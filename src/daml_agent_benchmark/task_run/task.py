"""Running one task, start to finish.

`run_task` is the order the steps happen in. Each step is a call into the file that owns
it: the copy from `repo_copy.py`, the checks on it from `ground_truth.py`, what the agent
is handed from `inputs.py`, the attempt from `attempt.py`, and the grade from
`ground_truth.py` again.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from daml_agent_benchmark.config import ExperimentConfig, secret_env_names
from daml_agent_benchmark.records import (
    LiveState,
    TaskResult,
    copies_relative,
    now_utc,
    repo_relative,
    task_live_record_path,
    write_task_live_result,
)
from daml_agent_benchmark.run_files import (
    build_impl_file_snapshots,
    read_impl_original_snapshots,
    task_live_stdout_events_path,
    task_safe_name,
)
from daml_agent_benchmark.task_run.attempt import attempt_with_quota_retries, redacted
from daml_agent_benchmark.task_run.ground_truth import grade_in_environment, verify_copy
from daml_agent_benchmark.task_run.inputs import (
    build_codex_prompt,
    clear_implementation_files,
    load_prompt_guidance,
    stage_skill_into_copy,
    stage_task_docs,
)
from daml_agent_benchmark.task_run.outcome import classify_attempt
from daml_agent_benchmark.task_run.repo_copy import prepare_task_repo_copy
from daml_agent_benchmark.tasklist_catalog import repo_relative_id, repo_root_for_path


def _queued_at(run_dir: Path | None, safe_name: str) -> datetime | None:
    """When the runner queued this task, from the snapshot it wrote before starting it."""
    if run_dir is None:
        return None
    snapshot = task_live_record_path(run_dir, safe_name)
    if not snapshot.exists():
        return None
    return TaskResult.from_record(json.loads(snapshot.read_text(encoding="utf-8"))).queued_at_utc


def run_task(
    config: ExperimentConfig,
    test_file_path: str,
    impl_files: list[str],
    run_repo_copies_dir: str,
    run_dir: Path | None = None,
) -> TaskResult:
    """One task, from its repository copy to its record.

    1. Copy the source repository, so the agent works in a tree of its own
    2. Verify the copy: it must not hold the answer, and the task must build and pass here
    3. Blank the implementation files, so the agent writes them from scratch
    4. Stage what the agent is handed: the prompt, and any skill and documentation
    5. Let it attempt the task, retrying while the provider rate-limits the run
    6. Classify what it did, and grade what it wrote
    7. Delete the copy

    The record exists from the first line and is narrowed as each step answers something,
    so the live snapshot the dashboard polls and the record written at the end are the same
    object at different moments. Step 2 can reject the copy, and step 7 happens on the way
    out either way: what survives the copy is in the record.
    """
    started_at = now_utc()
    task_id_rel = repo_relative_id(test_file_path)
    safe_name = task_safe_name(test_file_path)
    task = TaskResult(
        task_id=task_id_rel,
        task_safe_name=safe_name,
        test_file=repo_relative(test_file_path),
        impl_files=[repo_relative(f) for f in impl_files],
        live_state=LiveState.RUNNING,
        queued_at_utc=_queued_at(run_dir, safe_name),
        started_at_utc=started_at,
        finished_at_utc=None,
        live_updated_at_utc=started_at,
        repo_copy=None,
        findings=[],
        grade=None,
        ground_truth_control=None,
        repo_copy_integrity=None,
        impl_file_snapshots=[],
        attempts=None,
    )

    # A "running" live snapshot so the UI can show this task is in progress
    live_stdout_events_path: Path | None = None
    if run_dir:
        live_stdout_events_path = task_live_stdout_events_path(run_dir, test_file_path)
        live_stdout_events_path.parent.mkdir(parents=True, exist_ok=True)
        write_task_live_result(run_dir, task)

    # 1. The task's own copy of the repository, with paths remapped
    repo_root = repo_root_for_path(test_file_path)
    task_prefix = f"[{Path(test_file_path).name}] "
    repo_copy_dir, pruned_dars = prepare_task_repo_copy(
        repo_root, impl_files, run_repo_copies_dir, log_prefix=task_prefix, test_file_path=test_file_path
    )
    task = replace(task, repo_copy=copies_relative(repo_copy_dir))

    relative_test_path = os.path.relpath(test_file_path, repo_root)
    repo_copy_test_file = os.path.join(repo_copy_dir, relative_test_path)
    repo_copy_impl_files = [os.path.join(repo_copy_dir, os.path.relpath(p, repo_root)) for p in impl_files]
    original_impl_snapshots = read_impl_original_snapshots(impl_files)

    # 2. Whether this copy can measure an agent at all: it must not hold the answer, and
    # the task must build and pass its own tests here. Neither can be asked once it is blanked.
    integrity, control, rejection = verify_copy(
        config,
        task_id=task_id_rel,
        repo_copy_dir=repo_copy_dir,
        repo_copy_test_file=repo_copy_test_file,
        repo_copy_impl_files=repo_copy_impl_files,
        pruned_archives=pruned_dars,
        log_prefix=task_prefix,
    )
    if rejection:
        print(f"{task_prefix}{rejection.detail}", flush=True)
        failure = replace(
            task, findings=[rejection], repo_copy_integrity=integrity, ground_truth_control=control
        ).completed()
        if run_dir:
            write_task_live_result(run_dir, failure)
        shutil.rmtree(repo_copy_dir, ignore_errors=True)
        return failure

    # 3. Always clear impl files so every task is from-scratch generation, not patching existing code.
    clear_implementation_files(repo_copy_impl_files)

    # 4. What the agent is handed: the prompt, and whatever else the experiment stages.
    docs_skill_content = load_prompt_guidance(config) if config.prompt_guidance_file else None
    prompt = build_codex_prompt(
        repo_copy_test_file,
        repo_copy_impl_files,
        repo_copy_dir,
        docs_skill_content=docs_skill_content,
    )

    task_log_path = None
    if run_dir:
        task_log_path = run_dir / "full_terminal_output_split_per_task" / f"{safe_name}.log"

    if config.skill:
        stage_skill_into_copy(config, run_dir, repo_copy_dir)

    if config.task_docs_dir:
        staged_docs = stage_task_docs(config, test_file_path, repo_copy_dir)
        print(f"{task_prefix}docs staged: {', '.join(staged_docs)}", flush=True)

    # 5. The agent's attempt, retried while the provider is rate-limiting the run.
    attempt = attempt_with_quota_retries(
        config,
        task_id=task_id_rel,
        repo_copy_root=repo_copy_dir,
        repo_copy_impl_files=repo_copy_impl_files,
        prompt=prompt,
        test_rel_path=relative_test_path.replace("\\", "/"),
        log_prefix=task_prefix,
        task_log_path=task_log_path,
        live_stdout_events_path=live_stdout_events_path,
    )

    # 6. What the attempt did, and what its files do.
    findings = classify_attempt(attempt, integrity)
    for finding in findings:
        print(f"{task_prefix}{finding.flag.severity.value} finding {finding.flag.value}: {finding.detail}", flush=True)

    # Every task is graded, including one with a security or infra finding: the record
    # then shows what the files on the host actually did, which is what a debugging
    # reader wants. The finding marks the result as untrusted and keeps it out of the
    # pass rates.
    result = replace(
        task,
        findings=findings,
        grade=grade_in_environment(config, repo_copy_test_file, repo_copy_impl_files),
        ground_truth_control=control,
        repo_copy_integrity=integrity,
        impl_file_snapshots=build_impl_file_snapshots(
            impl_files=impl_files,
            repo_copy_impl_files=repo_copy_impl_files,
            repo_copy_root=repo_copy_dir,
            original_snapshots=original_impl_snapshots,
        ),
        attempts=redacted(attempt, [os.environ[name] for name in secret_env_names(config)]),
    ).completed()

    if run_dir:
        write_task_live_result(run_dir, result)

    # 7. The copy has served its purpose; what survives it is in the record.
    shutil.rmtree(repo_copy_dir, ignore_errors=True)
    return result
