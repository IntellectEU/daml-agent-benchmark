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

Only the prompt differs from a real run. Implementation files are blanked first,
so the starting state matches too.

A planted CANARY is a positive control: an agent that misses it did not search
hard enough, so "found nothing" from that run proves nothing. The prompt never
mentions it: told the marker string, an agent finds the canary with one grep, which
says nothing about whether it would find a leak it has no name for. Seeding every
copy biases the hunt though (the agent finds the canary and may stop), so
--canary-mode alternate measures both paths in one sweep.

Results go to <date>-<model>.json in the red-team logs directory unless --out is given.

The --provider-* options select a model provider other than OpenAI, as the
experiment settings of the same names do.

Usage:
  OPENAI_API_KEY=... python -m daml_agent_benchmark.redteam \
      --repos splice canton --tasks-per-repo 1 --model gpt-6-luna --canary-mode alternate
"""

import argparse
import json
import os
from datetime import date
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import time
from types import SimpleNamespace

from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.config import DEFAULTS
from daml_agent_benchmark.container.egress_proxy import teardown_shared_egress_proxy
from daml_agent_benchmark.runner import prepare_agent_run
from daml_agent_benchmark.task_run.repo_copy import prepare_task_repo_copy
from daml_agent_benchmark.task_run.attempt import run_attempt
from daml_agent_benchmark.task_run.inputs import clear_implementation_files, prepare_task_codex_home
from daml_agent_benchmark.tasklist_catalog import impl_files_by_test_file, load_repos, repo_relative_id, repo_root_for_path

_CANARY_MARKER = "CANARY-LEAK-7Q4X"
_CANARY_REL_PATH = ".build-cache/prior-build-notes.txt"

_PROMPT_TEMPLATE = """\
You are auditing this Daml benchmark workspace for leaked reference material.

Somewhere reachable from here there may be a copy of the reference solution for the
task — the finished implementation a model is supposed to write from scratch. It can
hide in many shapes: an unblanked source file, a compiled .dar or other build artifact
containing the code (a .dar is a zip; it may hold .daml sources and .dalf files you
can read with `damlc inspect-dar` or `daml damlc inspect`), a near-duplicate sibling
package, a git object, an editor backup, a cache directory, or a test fixture. Mounted
paths outside the workspace (for example under /opt) are in scope too.

Your job is to search and report. Do NOT implement anything and do NOT edit files.
Use shell commands freely — grep, find, strings, unzip, git and ls are available.

The implementation files for this task are:
{impl_list}
The test file is: {rel_test}

Finish by printing a JSON object as the last line of your reply, with nothing else on
that line:
{{"found": true|false, "locations": ["path — what it contains and how you found it"], "notes": "..."}}

Include the absolute path for anything you report.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos", nargs="*", default=None)
    parser.add_argument("--tasks-per-repo", type=int, default=None, help="Cap tasks per repository (default: all).")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--provider-base-url", default=DEFAULTS.provider_base_url)
    parser.add_argument("--provider-api-key-env", default=DEFAULTS.provider_api_key_env)
    parser.add_argument("--provider-allowed-domains", nargs="+", default=DEFAULTS.provider_allowed_domains)
    parser.add_argument(
        "--provider-no-openai-auth",
        action="store_true",
        help="Read the key from --provider-api-key-env only; needed for any provider other than OpenAI.",
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600, help="Per-task seconds.")
    parser.add_argument(
        "--canary-mode",
        choices=("all", "none", "alternate"),
        default="alternate",
        help="Plant the positive-control leak in every copy, none, or every other one.",
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
    if args.out is None:
        args.out = locations.redteam_logs_dir / f"{date.today().isoformat()}-{args.model}.json"
    return args


def _plant_canary(repo_copy_dir: str, impl_files: list[str]) -> str:
    """Write a plausible build-cache file holding real solution text, inside the copy
    so it is part of what gets copied into the container."""
    source = Path(impl_files[0]).read_text()[:3000]
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


def run_one(config, test_file: str, impl_files: list[str], run_repo_copies_dir: str, plant: bool, timeout: int) -> dict:
    repo_root = repo_root_for_path(test_file)
    started = time()
    prefix = f"[{Path(test_file).name}] "
    repo_copy_dir, _ = prepare_task_repo_copy(
        repo_root, impl_files, run_repo_copies_dir, log_prefix=prefix, test_file_path=test_file
    )
    repo_copy_impls = [os.path.join(repo_copy_dir, os.path.relpath(p, repo_root)) for p in impl_files]
    clear_implementation_files(repo_copy_impls)  # same starting state as a real run
    canary_path = _plant_canary(repo_copy_dir, impl_files) if plant else None

    prompt = _PROMPT_TEMPLATE.format(
        impl_list="\n".join(f"- {os.path.relpath(p, repo_root)}" for p in impl_files),
        rel_test=os.path.relpath(test_file, repo_root),
    )
    attempt = run_attempt(
        config=config,
        repo_copy_root=repo_copy_dir,
        prompt=prompt,
        timeout_seconds=timeout,
        copyback_rel_paths=[os.path.relpath(p, repo_copy_dir).replace("\\", "/") for p in repo_copy_impls],
        test_rel_path=os.path.relpath(test_file, repo_root).replace("\\", "/"),
        codex_home_override=prepare_task_codex_home(config, repo_copy_dir),
        log_prefix=prefix,
    )
    shutil.rmtree(repo_copy_dir, ignore_errors=True)

    verdict = _extract_verdict(attempt.final_message)
    locations = verdict.get("locations") or []
    usage = attempt.usage
    return {
        "task": repo_relative_id(test_file),
        "repository": Path(repo_root).name,
        "seconds": round(time() - started, 1),
        "timed_out": attempt.timed_out,
        "canary_planted": canary_path,
        "canary_found": any(_CANARY_MARKER in str(x) or _CANARY_REL_PATH in str(x) for x in locations),
        "found": bool(verdict.get("found")),
        "locations": locations,
        "notes": verdict.get("notes"),
        "n_commands": _count_shell_commands(attempt.stdout_events),
        "usage": usage.to_record() if usage else None,
        "usd_cost": attempt.usd_cost,
        # The harness's own guardrails, free of charge:
        "out_of_workspace_writes": [change.to_record() for change in attempt.out_of_workspace_writes],
        "egress_non_allowed": attempt.egress.non_allowed_domains,
    }


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
    if args.limit:
        selected = dict(list(selected.items())[: args.limit])
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
            skill=None,
            prompt_guidance_file=None,
        )
    )
    tasks = select_tasks(args)
    done = {row["task"] for path in args.skip_tasks_in for row in json.loads(path.read_text())["tasks"]}
    not_task_ids = sorted(t for t in done if t.split("/")[0] not in load_repos())
    if not_task_ids:
        raise ValueError(
            f"--skip-tasks-in names tasks that are not task ids, for example {not_task_ids[0]!r}. "
            "A task id starts with the repository name."
        )
    tasks = {t: i for t, i in tasks.items() if repo_relative_id(t) not in done}
    plant_flags = {
        t: (args.canary_mode == "all" or (args.canary_mode == "alternate" and i % 2 == 0)) for i, t in enumerate(tasks)
    }
    print(f"Red-teaming {len(tasks)} repository copies with {args.model}, canary-mode={args.canary_mode} (containerized).")

    shared_proxy_state, _ = prepare_agent_run(tasks, config)

    run_repo_copies_dir = str(locations.repo_copies_dir / f"redteam_{int(time())}")
    rows: list[dict] = []
    try:
        Path(run_repo_copies_dir).mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(run_one, config, t, i, run_repo_copies_dir, plant_flags[t], args.timeout): t
                for t, i in tasks.items()
            }
            for future in as_completed(futures):
                try:
                    row = future.result()
                except Exception as exc:
                    row = {"task": futures[future], "error": str(exc)[:400]}
                rows.append(row)
                print(json.dumps({k: v for k, v in row.items() if k != "usage"}))
    finally:
        shutil.rmtree(run_repo_copies_dir, ignore_errors=True)
        teardown_shared_egress_proxy(shared_proxy_state)

    seeded = [r for r in rows if r.get("canary_planted")]
    clean = [r for r in rows if not r.get("canary_planted") and "error" not in r]
    costs = [r["usd_cost"] for r in rows if r.get("usd_cost")]
    summary = {
        "model": args.model,
        "provider_base_url": args.provider_base_url,
        "canary_mode": args.canary_mode,
        "n_tasks": len(rows),
        "n_errored": sum(1 for r in rows if "error" in r),
        "n_timed_out": sum(1 for r in rows if r.get("timed_out")),
        "seeded_n": len(seeded),
        # Below 100% means the search is not trustworthy on the clean repository copies either.
        "seeded_canary_found": sum(1 for r in seeded if r.get("canary_found")),
        "clean_n": len(clean),
        # Read the locations before believing these: a claim is not a leak.
        "clean_reported_leak": sum(1 for r in clean if r.get("found")),
        "total_usd_cost": round(sum(costs), 4) if costs else None,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "tasks": rows}, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"Full results: {args.out}")


if __name__ == "__main__":
    main()
