"""Check a task's mutations against its ground-truth test file.

For each mutation in `tasklist/mutations/<task file name>.yaml` (the task id with each `/`
replaced by `__`), the mutant is built and the ground-truth test file run on it, in the eval
container. The outcome says whether the
mutation is usable: it applies, the mutant compiles, and its target script fails. A real-bug
mutation may have no target script, when no ground-truth script is known to catch it; it is then
killed when any script fails. Every script that passed on the ground truth and fails on the
mutant is recorded, since a mutation often breaks more than its target.

    python -m daml_agent_benchmark.mutations.validate <task id> [--only <mutation id> ...]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from daml_agent_benchmark.config import ExperimentConfig
from daml_agent_benchmark.container.daemon import docker_daemon_is_running, stop_docker_daemon
from daml_agent_benchmark.container.image import ensure_container_ready
from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.mutations.model import (
    Mutation,
    MutationValidation,
    ValidationOutcome,
    apply_patch,
    load_mutations,
    validation_report_path,
)
from daml_agent_benchmark.task_run.mutant_grading import MutantRun, grade_timed, run_mutants
from daml_agent_benchmark.task_run.repo_copy import prepare_task_repo_copy
from daml_agent_benchmark.tasklist_catalog import get_selected_tasks, repo_root_for_path


def _script_key(test_rel_path: str, script: str, results: dict[str, bool]) -> str:
    """The grading key of a script of the ground-truth test file: `<path>:<script>`, where
    `<path>` is relative to the test's package and so a suffix of the repository path."""
    matches = [
        key for key in results
        if key.endswith(f":{script}") and test_rel_path.endswith(key.rsplit(":", 1)[0])
    ]
    if len(matches) != 1:
        raise KeyError(f"script {script!r} of {test_rel_path} matches {matches} among the ground truth's results")
    return matches[0]


def _patch_problem(mutation: Mutation, impl_rel: list[str], originals: dict[str, bytes]) -> MutationValidation | None:
    """The verdict on a mutation whose mutant cannot be built from its patch, or None when it can."""
    stray = [p for p in mutation.patched_paths() if p not in impl_rel]
    if stray:
        outcome, detail = ValidationOutcome.NOT_IMPL_ONLY, f"patch changes {stray}"
    else:
        patched, apply_output = apply_patch(mutation.patch, originals)
        if patched is not None:
            return None
        outcome, detail = ValidationOutcome.DOES_NOT_APPLY, apply_output
    return MutationValidation(
        id=mutation.id,
        source=mutation.source,
        kind=mutation.kind,
        target_script=mutation.target_script,
        outcome=outcome,
        outcome_detail=detail,
        newly_failing={},
        seconds=0.0,
    )


def _verdict(run: MutantRun, passing: set[str], target_key: str | None) -> MutationValidation:
    """Whether the mutation is usable, from what its mutant did under the ground-truth tests."""
    failing: dict[str, str] = {}
    if not run.grade.compile_passed:
        outcome, detail = ValidationOutcome.DOES_NOT_COMPILE, run.grade.syntax_error or run.grade.compile_error
    elif not run.grade.test_results:
        # The package built, but the test file did not compile against it, so no script ran.
        outcome, detail = ValidationOutcome.DOES_NOT_COMPILE, run.grade.tests_error
    else:
        failing = {
            key: run.reported_failures.get(key, "")
            for key in sorted(passing)
            if not run.grade.test_results.get(key, False)
        }
        # Without a target, the mutant is killed when any script that passed on the ground truth fails.
        killed = bool(failing) if target_key is None else target_key in failing
        outcome, detail = (ValidationOutcome.KILLED if killed else ValidationOutcome.SURVIVED), None
    return MutationValidation(
        id=run.mutation.id,
        source=run.mutation.source,
        kind=run.mutation.kind,
        target_script=run.mutation.target_script,
        outcome=outcome,
        outcome_detail=detail,
        newly_failing=failing,
        seconds=run.seconds,
    )


def validate_task(task_id: str, only: list[str] | None = None) -> dict:
    """Validate the task's mutations, or only those named in `only`, and write the report.

    1. A copy of the task's repository is made, and the ground-truth test file is graded on
       it, unmutated. Every script must pass, or the task's tests say nothing about mutants.
    2. Each mutation's patch is checked: it must change only implementation files and apply
       to them. A mutation that fails gets its verdict here and is never built.
    3. Each remaining mutant is built from a clone of the copy, and the ground-truth test
       file is run on it. Its verdict compares the scripts against step 1.

    The report lists every mutation in the mutation file's order, and is written to the logs
    directory's `mutations/<task file name>.json`, replacing an earlier one; with `only`, it
    holds only those mutations.
    """
    mutations = [m for m in load_mutations(task_id) if only is None or m.id in only]

    config = ExperimentConfig(tasks=[task_id])
    tasks = get_selected_tasks(config)
    ((test_file, impl_files),) = tasks.items()
    repo_root = repo_root_for_path(test_file)
    test_rel = os.path.relpath(test_file, repo_root)
    impl_rel = [os.path.relpath(p, repo_root) for p in impl_files]

    ensure_container_ready(tasks, impl_files)
    # Unique per validation, so that validations running at the same time never share it.
    locations.repo_copies_dir.mkdir(parents=True, exist_ok=True)
    copies_dir = Path(tempfile.mkdtemp(prefix="mutations-", dir=locations.repo_copies_dir))
    copy_dir, _ = prepare_task_repo_copy(repo_root, impl_files, str(copies_dir), test_file_path=test_file)
    copy_test = os.path.join(copy_dir, test_rel)
    try:
        # 1. The ground truth, unmutated.
        copy_impls = [os.path.join(copy_dir, p) for p in impl_rel]
        ground_truth, ground_truth_seconds = grade_timed(copy_test, copy_impls, copy_impls)
        print(f"ground truth: tests_passed={ground_truth.tests_passed} in {ground_truth_seconds}s", flush=True)
        if not ground_truth.tests_passed:
            raise RuntimeError(f"the ground truth does not pass its own tests: {ground_truth.tests_error}")
        passing = {key for key, passed in ground_truth.test_results.items() if passed}

        # 2. The patches, before anything is built.
        originals = {rel: Path(copy_dir, rel).read_bytes() for rel in impl_rel}
        by_id = {}
        for mutation in mutations:
            problem = _patch_problem(mutation, impl_rel, originals)
            if problem is not None:
                by_id[mutation.id] = problem
                print(f"{problem.id}: {problem.outcome.value}: {problem.outcome_detail}", flush=True)

        # 3. The buildable mutants, each against the ground-truth scripts.
        buildable = [m for m in mutations if m.id not in by_id]
        for run in run_mutants(copy_dir, test_rel, impl_rel, buildable):
            target = run.mutation.target_script
            target_key = None if target is None else _script_key(test_rel, target, ground_truth.test_results)
            result = _verdict(run, passing, target_key)
            print(f"{result.id}: {result.outcome.value} ({result.seconds}s) newly_failing={sorted(result.newly_failing)}", flush=True)
            by_id[result.id] = result
        results = [by_id[m.id] for m in mutations]
    finally:
        shutil.rmtree(copy_dir, ignore_errors=True)
        shutil.rmtree(copies_dir, ignore_errors=True)

    report = {
        "task": task_id,
        "ground_truth_seconds": ground_truth_seconds,
        "ground_truth_scripts": sorted(ground_truth.test_results),
        "mutations": [asdict(r) for r in results],
    }
    out = validation_report_path(task_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"report: {out}")
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("task_id")
    parser.add_argument("--only", nargs="+", help="validate only these mutation ids")
    args = parser.parse_args(argv)
    docker_was_running = docker_daemon_is_running()
    try:
        validate_task(args.task_id, args.only)
    finally:
        if not docker_was_running:
            stop_docker_daemon()


if __name__ == "__main__":
    main()
