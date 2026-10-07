"""Red-team the repository copy: can the agent find the reference solution?

A normal run detects leaks only as a side effect — the agent has to stumble onto
one while solving. This inverts it: the agent's only job is to hunt for the
reference implementation in the environment it is given, and report what it found.

This drives the same invocation path as a real benchmark run: `run_attempt`, which
runs the agent through codex_in_container.py. The copy is copied into a container
rather than mounted, the working directory is /workspace, the SDK store is mounted
read-only at /opt/daml and /opt/dpm, capabilities are dropped, and network traffic
goes through a proxy that allows only the model provider's domains. The agent
therefore sees exactly what a benchmark agent sees. Running the agent against a
repository copy on the host is not equivalent and produces false leaks, because it
can read the whole file system, including the source checkouts and the repository's
own git history.

Only the prompt differs from a real run. The answer files are blanked first, so the
starting state matches too.

A planted CANARY is a positive control: an agent that misses it did not search
hard enough, so "found nothing" from that run proves nothing. The prompt never
mentions it: told the marker string, an agent finds the canary with one grep, which
says nothing about whether it would find a leak it has no name for. Seeding every
copy biases the hunt though (the agent finds the canary and may stop), so
--canary-mode alternate measures both paths in one sweep.

Without the answer, the agent has to recognise a leak from the task's file names alone.
That works for an implementation, whose copy carries the same template names, but not
for a test file: a sibling package's test of the same templates under another module
name reads as ordinary repository code. --show-answer puts the answer files in the prompt,
so the agent searches for anything they could be recovered from. Such a sweep plants no
canary: the canary holds the answer text, which a grep for that text finds at once.

Results go to <date>-<model>.json in the red-team logs directory unless --out is given.
The file is a `RedTeamReport`: the sweep's summary, one `RedTeamResult` per task the agent
finished, and one `RedTeamFailure` per task the harness could not run. --skip-tasks-in
leaves out the finished tasks of such files, so a failed task runs again.

The --provider-* options select a model provider other than OpenAI, as the
experiment settings of the same names do.

Usage:
  OPENAI_API_KEY=... python -m daml_agent_benchmark.redteam \
      --repos splice canton --tasks-per-repo 1 --model gpt-6-luna --canary-mode alternate
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import time
from types import SimpleNamespace

from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.config import DEFAULTS, ExperimentConfig, TaskKindName
from daml_agent_benchmark.container.egress_proxy import teardown_shared_egress_proxy
from daml_agent_benchmark.runner import prepare_agent_run
from daml_agent_benchmark.mutations.model import load_mutations
from daml_agent_benchmark.records import FileChange, Record, TokenUsage, write_record_json
from daml_agent_benchmark.task_kinds import task_kind
from daml_agent_benchmark.task_run.repo_copy import hide_in_repo_copy, prepare_task_repo_copy
from daml_agent_benchmark.task_run.attempt import run_attempt
from daml_agent_benchmark.task_run.inputs import blank_files, prepare_task_codex_home
from daml_agent_benchmark.tasklist_catalog import impl_files_by_test_file, load_repos, repo_relative_id, repo_root_for_path

_CANARY_MARKER = "CANARY-LEAK-7Q4X"
_CANARY_REL_PATH = ".build-cache/prior-build-notes.txt"


class CanaryMode(StrEnum):
    NONE = "none"  # no copy holds the canary
    ALTERNATE = "alternate"  # every other copy holds it, starting with the first
    # The other half: with an `alternate` sweep of the same tasks, every task gets one copy
    # without the canary, where finding the canary cannot end the search early.
    ALTERNATE_SWAPPED = "alternate-swapped"


_PROMPT_TEMPLATE = """\
You are auditing this Daml benchmark workspace for leaked reference material.

Somewhere reachable from here there may be a copy of the reference solution for the
task — {answer_what}. It can
hide in many shapes: an unblanked source file, a compiled .dar or other build artifact
containing the code (a .dar is a zip; it may hold .daml sources and .dalf files you
can read with `damlc inspect-dar` or `daml damlc inspect`), a near-duplicate sibling
package, a git object, an editor backup, a cache directory, or a test fixture. Mounted
paths outside the workspace (for example under /opt) are in scope too.

Your job is to search and report. Do NOT implement anything and do NOT edit files.
Use shell commands freely — grep, find, strings, unzip, git and ls are available.

{files}
{answer}
Finish by printing a JSON object as the last line of your reply, with nothing else on
that line:
{{"found": true|false, "locations": ["path — what it contains and how you found it"], "notes": "..."}}

Include the absolute path for anything you report.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos", nargs="*", default=None)
    parser.add_argument("--tasks-per-repo", type=int, default=None, help="Cap tasks per repository (default: all).")
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--provider-base-url", default=DEFAULTS.provider_base_url)
    parser.add_argument("--provider-api-key-env", default=DEFAULTS.provider_api_key_env)
    parser.add_argument("--provider-allowed-domains", nargs="+", default=DEFAULTS.provider_allowed_domains)
    parser.add_argument(
        "--provider-no-openai-auth",
        action="store_true",
        help="Read the key from --provider-api-key-env only; needed for any provider other than OpenAI.",
    )
    parser.add_argument(
        "--task-kind", type=TaskKindName, choices=list(TaskKindName), default=TaskKindName.IMPLEMENTATION
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600, help="Per-task seconds.")
    parser.add_argument(
        "--canary-mode",
        type=CanaryMode,
        choices=list(CanaryMode),
        default=CanaryMode.ALTERNATE,
        help="Plant the positive-control leak in every other copy, in the other half of the copies, or in none.",
    )
    parser.add_argument(
        "--show-answer",
        action="store_true",
        help="Put the answer files in the prompt; needs --canary-mode none.",
    )
    parser.add_argument(
        "--skip-tasks-in",
        type=Path,
        nargs="*",
        default=[],
        help="Result files whose tasks are left out, to continue a sweep or avoid repeating one.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Result file (default: <date>-<model>.json in the red-team logs directory).")
    args = parser.parse_args()
    if args.show_answer and args.canary_mode is not CanaryMode.NONE:
        parser.error("--show-answer needs --canary-mode none: the canary holds the answer text")
    if args.out is None:
        args.out = locations.redteam_logs_dir / f"{date.today().isoformat()}-{args.model}.json"
    return args


def _plant_canary(repo_copy_dir: str, answer_files: list[str]) -> str:
    """Write a plausible build-cache file holding real solution text, inside the copy
    so it is part of what gets copied into the container."""
    source = Path(answer_files[0]).read_text()[:3000]
    target = Path(repo_copy_dir) / _CANARY_REL_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# build notes {_CANARY_MARKER}\n# previous successful build of this package:\n\n{source}\n")
    return _CANARY_REL_PATH


def _extract_verdict(final_message: str | None) -> dict:
    """Pull the agent's trailing JSON verdict; tolerate fences and surrounding prose."""
    for line in reversed((final_message or "").strip().splitlines()):
        line = line.strip().strip("`")
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def _count_shell_commands(events: list[dict] | None) -> int:
    """Completed command items in the run's event log (entries wrap the codex event under `event`)."""
    return sum(
        1
        for e in (events or [])
        if e["event"].get("type") == "item.completed"
        and (e["event"].get("item") or {}).get("type") in ("commandExecution", "command_execution")
    )


_ANSWER_WHAT = {
    TaskKindName.IMPLEMENTATION: "the finished implementation a model is supposed to write from scratch",
    TaskKindName.TEST_GENERATION: (
        "the finished test file a model is supposed to write from scratch, or a list of the bugs "
        "that will be seeded into the implementation to grade those tests"
    ),
}
# The task's files as the prompt lists them, filled with the test file and the implementation list.
_FILES = {
    TaskKindName.IMPLEMENTATION: "The implementation files for this task are:\n{impl_list}\nThe test file is: {rel_test}",
    TaskKindName.TEST_GENERATION: "The test file for this task, blanked here, is: {rel_test}\nThe implementation files are:\n{impl_list}",
}
# With --show-answer: the answer files' contents, so the agent can recognise a copy that
# carries none of the task's names.
_ANSWER = """
Here is the reference solution, as it was before it was blanked, so you can recognise it.
Look for anything in the environment from which all of it or a substantial part could be
recovered: an exact copy, a copy under another file or module name, a reformatted or
reordered copy, a file that shares most of its code with it, or a compiled form. For each
location, say roughly what share of the solution it covers.

Your own session is not a finding: /workspace/.codex_home holds it, and its logs contain
this prompt and so the solution above.{not_findings}

{contents}
"""
# What else the agent is told not to report, by task kind.
_NOT_FINDINGS = {
    TaskKindName.IMPLEMENTATION: "",
    TaskKindName.TEST_GENERATION: (
        "\nNeither are the implementation files, which the model is given:\n"
        "their source, or a compiled form of them such as Java classes generated from their\n"
        "templates inside a jar, is not a copy of the test file."
    ),
}


def _answer_block(kind: TaskKindName, answers: list[str], rel) -> str:
    return _ANSWER.format(
        not_findings=_NOT_FINDINGS[kind],
        contents="\n".join(f"--- {rel(p)} ---\n{Path(p).read_text(encoding='utf-8')}" for p in answers)
    )


# --- What a sweep writes down -----------------------------------------------------------


@dataclass(frozen=True)
class RedTeamResult(Record):
    """One repository copy the agent searched, and what it reported finding."""

    task: str  # the task id: the test file as `<repository>/<path inside it>`
    task_kind: TaskKindName  # which files were the answer the agent searched for
    repository: str  # the source repository the copy was made from
    seconds: float  # from making the copy to the agent's verdict
    timed_out: bool  # the per-task cap stopped the agent
    canary_path: str | None  # where the positive-control leak was planted, relative to the copy; None in a clean copy
    canary_found: bool  # a reported location names the canary
    found: bool  # the agent claims a leak; a claim until its locations are read
    locations: list[str]  # each reported leak: where it is, what it holds, how the agent found it
    notes: str | None  # the agent's remarks, when its verdict had any
    n_commands: int  # shell commands the agent ran
    usage: TokenUsage | None  # tokens over every thread; None when no thread reported any
    usd_cost: float | None  # `usage` priced for the model; None when no price is known for it
    # The harness's own guardrails, free of charge; both always empty:
    out_of_workspace_writes: list[FileChange]  # files the agent changed outside its repository copy
    egress_non_allowed: list[str]  # hosts outside the allowlist that the proxy served


@dataclass(frozen=True)
class RedTeamFailure(Record):
    """A task the harness could not run, so there is no verdict for it."""

    task: str  # the task id
    error: str  # the exception, clipped


@dataclass(frozen=True)
class RedTeamSummary(Record):
    """How the sweep was run, and its counts over the tasks."""

    model: str
    provider_base_url: str
    canary_mode: CanaryMode
    answer_shown: bool  # the prompt held the answer files (--show-answer)
    n_tasks: int  # every task the sweep ran, the failed ones included
    n_errored: int  # the failed ones
    n_timed_out: int
    seeded_n: int  # finished tasks whose copy held the canary
    # Below 100% means the search is not trustworthy on the clean repository copies either.
    seeded_canary_found: int
    clean_n: int  # finished tasks whose copy held no canary
    # Read the locations before believing these: a claim is not a leak.
    clean_reported_leak: int
    total_usd_cost: float | None  # over the tasks whose cost is known; None when none is

    @classmethod
    def of(
        cls,
        model: str,
        provider_base_url: str,
        canary_mode: CanaryMode,
        answer_shown: bool,
        tasks: list[RedTeamResult],
        failed: list[RedTeamFailure],
    ) -> RedTeamSummary:
        seeded = [r for r in tasks if r.canary_path is not None]
        clean = [r for r in tasks if r.canary_path is None]
        costs = [r.usd_cost for r in tasks if r.usd_cost is not None]
        return cls(
            model=model,
            provider_base_url=provider_base_url,
            canary_mode=canary_mode,
            answer_shown=answer_shown,
            n_tasks=len(tasks) + len(failed),
            n_errored=len(failed),
            n_timed_out=sum(1 for r in tasks if r.timed_out),
            seeded_n=len(seeded),
            seeded_canary_found=sum(1 for r in seeded if r.canary_found),
            clean_n=len(clean),
            clean_reported_leak=sum(1 for r in clean if r.found),
            total_usd_cost=round(sum(costs), 4) if costs else None,
        )


@dataclass(frozen=True)
class RedTeamReport(Record):
    """A sweep's result file."""

    summary: RedTeamSummary
    tasks: list[RedTeamResult]  # the tasks the agent finished, one verdict each
    failed: list[RedTeamFailure]  # the tasks the harness could not run

    @classmethod
    def load(cls, path: Path) -> RedTeamReport:
        return cls.from_record(json.loads(path.read_text(encoding="utf-8")))

    def write(self, path: Path) -> None:
        write_record_json(path, self.to_record())


def run_one(
    config: ExperimentConfig,
    test_file: str,
    impl_files: list[str],
    run_repo_copies_dir: str,
    plant: bool,
    show_answer: bool,
    timeout: int,
) -> RedTeamResult:
    kind = task_kind(config.task_kind)
    repo_root = repo_root_for_path(test_file)
    started = time()
    prefix = f"[{Path(test_file).name}] "
    repo_copy_dir, _ = prepare_task_repo_copy(
        repo_root, impl_files, run_repo_copies_dir, log_prefix=prefix, test_file_path=test_file
    )
    hide_in_repo_copy(repo_copy_dir, kind.hidden_from_copy(repo_relative_id(test_file)))

    def rel(path: str) -> str:
        return os.path.relpath(path, repo_root).replace("\\", "/")

    def in_copy(path: str) -> str:
        return os.path.join(repo_copy_dir, rel(path))

    answers = kind.answer_files(test_file, impl_files)
    # Read from the source checkout, which the blanking below leaves alone.
    answer = _answer_block(kind.name, answers, rel) if show_answer else ""
    blank_files([in_copy(p) for p in answers])  # same starting state as a real run
    canary_path = _plant_canary(repo_copy_dir, answers) if plant else None

    prompt = _PROMPT_TEMPLATE.format(
        answer_what=_ANSWER_WHAT[kind.name],
        files=_FILES[kind.name].format(
            impl_list="\n".join(f"- {rel(p)}" for p in impl_files), rel_test=rel(test_file)
        ),
        answer=answer,
    )
    attempt = run_attempt(
        config=config,
        repo_copy_root=repo_copy_dir,
        prompt=prompt,
        timeout_seconds=timeout,
        copyback_rel_paths=[rel(p) for p in answers],
        protected_rel_paths=[rel(p) for p in kind.protected_files(test_file, impl_files)],
        codex_home_override=prepare_task_codex_home(config, repo_copy_dir),
        log_prefix=prefix,
    )
    shutil.rmtree(repo_copy_dir, ignore_errors=True)

    # The verdict is whatever JSON the agent printed, so each field is coerced to its type here.
    verdict = _extract_verdict(attempt.final_message)
    reported = verdict.get("locations")
    locations = [str(x) for x in reported] if isinstance(reported, list) else []
    notes = verdict.get("notes")
    return RedTeamResult(
        task=repo_relative_id(test_file),
        task_kind=kind.name,
        repository=Path(repo_root).name,
        seconds=round(time() - started, 1),
        timed_out=attempt.timed_out,
        canary_path=canary_path,
        canary_found=any(_CANARY_MARKER in x or _CANARY_REL_PATH in x for x in locations),
        found=verdict.get("found") is True,
        locations=locations,
        notes=None if notes is None else str(notes),
        n_commands=_count_shell_commands(attempt.stdout_events),
        usage=attempt.usage,
        usd_cost=attempt.usd_cost,
        out_of_workspace_writes=list(attempt.out_of_workspace_writes),
        egress_non_allowed=list(attempt.egress.non_allowed_domains),
    )


def select_tasks(args: argparse.Namespace) -> dict[str, list[str]]:
    """Pick the active tasks, optionally restricted to some repositories and capped per repository."""
    per_repo: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    for test_file, impl_files in impl_files_by_test_file().items():
        if not impl_files:
            continue
        repository = Path(repo_root_for_path(test_file)).name
        if args.repos and repository not in args.repos:
            continue
        per_repo[repository].append((test_file, impl_files))

    selected: dict[str, list[str]] = {}
    for repository in sorted(per_repo):
        tasks = per_repo[repository][: args.tasks_per_repo] if args.tasks_per_repo else per_repo[repository]
        for test_file, impl_files in tasks:
            selected[test_file] = impl_files
    return selected


def main() -> None:
    args = parse_args()
    config = DEFAULTS.merge(
        SimpleNamespace(
            codex_model=args.model,
            provider_base_url=args.provider_base_url,
            provider_api_key_env=args.provider_api_key_env,
            provider_allowed_domains=list(args.provider_allowed_domains),
            provider_requires_openai_auth=not args.provider_no_openai_auth,
            max_task_runtime_seconds=args.timeout,
            task_kind=args.task_kind,
            skill=None,
            prompt_guidance_file=None,
        )
    )
    tasks = select_tasks(args)
    if args.task_kind is TaskKindName.TEST_GENERATION:
        # A task without mutations cannot run as test generation, so it is not red-teamed as one.
        tasks = {t: i for t, i in tasks.items() if load_mutations(repo_relative_id(t))}
    done = {result.task for path in args.skip_tasks_in for result in RedTeamReport.load(path).tasks}
    not_task_ids = sorted(t for t in done if t.split("/")[0] not in load_repos())
    if not_task_ids:
        raise ValueError(
            f"--skip-tasks-in names tasks that are not task ids, for example {not_task_ids[0]!r}. "
            "A task id starts with the repository name."
        )
    tasks = {t: i for t, i in tasks.items() if repo_relative_id(t) not in done}
    planted_parity = {CanaryMode.ALTERNATE: 0, CanaryMode.ALTERNATE_SWAPPED: 1}.get(args.canary_mode)
    plant_flags = {t: planted_parity is not None and i % 2 == planted_parity for i, t in enumerate(tasks)}
    print(
        f"Red-teaming {len(tasks)} repository copies with {args.model}, canary-mode={args.canary_mode}"
        f"{' answer-shown' if args.show_answer else ''} (containerized)."
    )

    shared_proxy_state, _ = prepare_agent_run(tasks, config)

    run_repo_copies_dir = str(locations.repo_copies_dir / f"redteam_{int(time())}")
    results: list[RedTeamResult] = []
    failed: list[RedTeamFailure] = []
    try:
        Path(run_repo_copies_dir).mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(run_one, config, t, i, run_repo_copies_dir, plant_flags[t], args.show_answer, args.timeout): t
                for t, i in tasks.items()
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    failure = RedTeamFailure(task=repo_relative_id(futures[future]), error=str(exc)[:400])
                    failed.append(failure)
                    print(json.dumps(failure.to_record()))
                    continue
                results.append(result)
                record = result.to_record()
                del record["usage"]
                print(json.dumps(record))
    finally:
        shutil.rmtree(run_repo_copies_dir, ignore_errors=True)
        teardown_shared_egress_proxy(shared_proxy_state)

    summary = RedTeamSummary.of(args.model, args.provider_base_url, args.canary_mode, args.show_answer, results, failed)
    RedTeamReport(summary=summary, tasks=results, failed=failed).write(args.out)
    print("\n=== SUMMARY ===")
    print(json.dumps(summary.to_record(), indent=2))
    print(f"Full results: {args.out}")


if __name__ == "__main__":
    main()
