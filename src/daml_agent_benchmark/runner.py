"""Running a benchmark experiment.

One experiment is a config and a set of tasks. This file prepares what the whole run
shares: the docker image, the egress proxy, the staged skill and the run directory. It
then runs the tasks, in parallel when asked to, and writes the summary. `main` loads an
experiment file and runs what it names.
"""

import argparse
import json
import os
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

from daml_agent_benchmark.code_snapshot import stage_code_snapshot_for_run
from daml_agent_benchmark.task_kinds import task_kind
from daml_agent_benchmark.config import (
    DEFAULTS,
    ExperimentConfig,
    build_config_dump,
    egress_allowed_hosts,
    egress_allowed_suffixes,
    validate_provider_settings,
)
from daml_agent_benchmark.constants import (
    CODEX_APPROVAL_POLICY,
    CODEX_BIN,
    CODEX_SANDBOX,
    CONTAINER_IMAGE,
    CONTAINER_NETWORK_MODE,
)
from daml_agent_benchmark.container.daemon import docker_daemon_is_running, stop_docker_daemon
from daml_agent_benchmark.container.egress_proxy import setup_shared_egress_proxy, teardown_shared_egress_proxy
from daml_agent_benchmark.container.image import ensure_container_ready
from daml_agent_benchmark.records import (
    RunResult,
    RunStatus,
    TaskResult,
    now_utc,
    write_run_result,
    write_task_live_result,
    write_task_result,
)
from daml_agent_benchmark.run_files import (
    build_run_identity,
    make_run_dir,
    make_run_repo_copies_dir,
    remove_run_repo_copies_dir_if_empty,
    task_live_stdout_events_path,
    task_selection_label,
    write_json,
)
from daml_agent_benchmark.run_identity import default_agent_run_name
from daml_agent_benchmark.task_run.task import run_task
from daml_agent_benchmark.task_run.env import (
    configure_wrapper_environment,
    ensure_codex_auth_ready,
    prepend_common_tool_paths,
    resolve_codex_bin,
)
from daml_agent_benchmark.task_run.inputs import stage_skill_for_run
from daml_agent_benchmark.task_run.repo_copy import assert_nix_shell_preflight
from daml_agent_benchmark.tasklist_catalog import add_tasklist_dir_argument, add_tasklist_dirs, get_selected_tasks, repo_relative_id
from daml_agent_benchmark.unrun_tasks import exception_task_result, queued_task_result


def _experiments_from_module(namespace: dict) -> list[SimpleNamespace]:
    """The experiments a config module declares: `CURRENT_EXP_LIST` when defined and non-empty, else `CURRENT_EXP`."""
    exp_list = namespace.get("CURRENT_EXP_LIST")
    if exp_list is not None:
        if not isinstance(exp_list, (list, tuple)):
            raise TypeError("CURRENT_EXP_LIST must be a list/tuple of SimpleNamespace configs.")
        if len(exp_list) == 0:
            raise ValueError("CURRENT_EXP_LIST is empty.")
        for i, exp in enumerate(exp_list, start=1):
            if not isinstance(exp, SimpleNamespace):
                raise TypeError(f"CURRENT_EXP_LIST[{i}] must be a SimpleNamespace.")
        return list(exp_list)
    exp_single = namespace.get("CURRENT_EXP")
    if exp_single is None:
        return [SimpleNamespace()]
    if not isinstance(exp_single, SimpleNamespace):
        raise TypeError("CURRENT_EXP must be a SimpleNamespace.")
    return [exp_single]


def _load_config_module(path: Path) -> dict:
    import importlib.util

    spec = importlib.util.spec_from_file_location("benchmark_experiment_config", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load experiment config {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the agent benchmark for the experiments a config module declares.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("experiment.py"),
        help="Python file defining CURRENT_EXP (a SimpleNamespace of ExperimentConfig overrides) or CURRENT_EXP_LIST.",
    )
    parser.add_argument(
        "--exp-index",
        type=int,
        default=None,
        help="1-based index into CURRENT_EXP_LIST (or CURRENT_EXP fallback).",
    )
    add_tasklist_dir_argument(parser)
    return parser.parse_args(argv)


def prepare_agent_run(tasks: dict[str, list[str]], config: ExperimentConfig) -> tuple[dict, str]:
    """Everything an agent run needs before its first task: valid provider settings, tool
    paths, the container image and SDK store, the run's egress proxy, the wrapper
    environment and the API key. Returns the proxy state (for teardown) and the resolved
    codex binary. The red-team driver calls this too, so its environment cannot drift
    from a benchmark run's."""
    validate_provider_settings(config)
    prepend_common_tool_paths(os.environ)
    resolved_codex_bin = resolve_codex_bin(CODEX_BIN)
    ensure_codex_auth_ready(resolved_codex_bin, config)
    kind = task_kind(config.task_kind)
    ensure_container_ready(tasks, [f for test, impls in tasks.items() for f in kind.answer_files(test, impls)])
    shared_proxy_state = setup_shared_egress_proxy(egress_allowed_suffixes(config), egress_allowed_hosts(config))
    configure_wrapper_environment(shared_proxy_state)
    return shared_proxy_state, resolved_codex_bin


def _run_config(config: ExperimentConfig) -> None:
    """Orchestrate a full benchmark run: select tasks, prepare the Docker environment,
    run each task (sequentially or in parallel), evaluate results, and write the summary."""
    tasks = get_selected_tasks(config)
    # A Docker that was already running stays up after the run; one that the run started is stopped.
    docker_was_running = docker_daemon_is_running()
    shared_proxy_state, resolved_codex_bin = prepare_agent_run(tasks, config)
    run_repo_copies_dir: Path | None = None
    try:
        # Wrap in try/finally so the shared egress proxy is always cleaned up, even on
        # KeyboardInterrupt or unexpected errors.
        try:
            assert_nix_shell_preflight(tasks)

            # Resolve stable display name + unique run id, then persist under run_id directory.
            run_id, run_name = build_run_identity(config)
            run_dir = make_run_dir(run_id)
            run_repo_copies_dir = make_run_repo_copies_dir()

            code_snapshot_info = stage_code_snapshot_for_run(run_dir)
            skill_info = None
            if config.skill:
                skill_info = stage_skill_for_run(config, run_dir)
            config_dump = build_config_dump(config)
            config_dump["run_id"] = run_id
            config_dump["run_name"] = run_name
            config_dump["run_repo_copies_dir"] = str(run_repo_copies_dir)
            config_dump["resolved_codex_bin"] = resolved_codex_bin
            config_dump["code_snapshot"] = code_snapshot_info
            config_dump["skill"] = skill_info
            write_json(run_dir / "config.json", config_dump)
            run = RunResult(
                run_id=run_id,
                run_name=run_name,
                status=RunStatus.IN_PROGRESS,
                created_at_utc=now_utc(),
                finished_at_utc=None,
                tasks=[],
                egress_events=None,
                egress_allowed_domain_suffixes=egress_allowed_suffixes(config),
                egress_allowed_hosts=egress_allowed_hosts(config),
            )
            write_run_result(run_dir, run)

            print(f"Running {len(tasks)} tasks with model={config.codex_model}, task_selection={task_selection_label(config)}")
            print(f"Run ID: {run_id}")
            print(f"Run Name: {run_name}")
            print(f"Resolved codex_bin: {resolved_codex_bin}")
            if skill_info is not None:
                print(f"Skill: enabled ({skill_info['staged_dir']})")
                print(f"skill snapshot: {skill_info['snapshot_zip']} (sha256={skill_info['snapshot_zip_sha256']})")
            else:
                print("Skill: none")
            print(f"Codex safety: sandbox={CODEX_SANDBOX}, approval_policy={CODEX_APPROVAL_POLICY}")
            print(f"Container image: {CONTAINER_IMAGE}, network: {CONTAINER_NETWORK_MODE}")
            print(f"Model provider: {config.provider_base_url} (key from ${config.provider_api_key_env})")
            print(f"MCP servers: {', '.join(s.name for s in config.mcp_servers) or 'none'}")
            print(f"Egress proxy allowed domains, with their subdomains: {', '.join(egress_allowed_suffixes(config))}")
            print(f"Egress proxy allowed hosts, exact: {', '.join(egress_allowed_hosts(config)) or 'none'}")
            if config.run_tasks_in_parallel:
                workers = config.max_parallel_tasks if config.max_parallel_tasks is not None else "auto"
                print(f"Task execution mode: parallel (max_parallel_tasks={workers})")
            else:
                print("Task execution mode: sequential")
            task_results = []
            task_items = list(tasks.items())

            # Write "queued" snapshots for all tasks upfront so the UI shows them immediately
            for test_file, impl_files in task_items:
                live_stdout_path = task_live_stdout_events_path(run_dir, test_file)
                live_stdout_path.parent.mkdir(parents=True, exist_ok=True)
                if not live_stdout_path.exists():
                    live_stdout_path.write_text("", encoding="utf-8")
                write_task_live_result(run_dir, queued_task_result(test_file, impl_files))

            def task_failure(index: int, test_file: str, impl_files: list[str], exc: Exception) -> TaskResult:
                """The record of a task whose run raised, wherever the raise surfaced."""
                return exception_task_result(
                    run_dir,
                    task_index=index,
                    total_tasks=len(task_items),
                    test_file=test_file,
                    impl_files=impl_files,
                    exception=exc,
                )

            def keep(task_result: TaskResult) -> None:
                """Hold a finished task for the run summary, and write its file."""
                task_results.append(task_result)
                write_task_result(run_dir, task_result)

            if not config.run_tasks_in_parallel:
                for i, (test_file, impl_files) in enumerate(task_items, start=1):
                    print(f"[{i}/{len(task_items)}] {repo_relative_id(test_file)}")
                    try:
                        task_result = run_task(config, test_file, impl_files, str(run_repo_copies_dir), run_dir=run_dir)
                    except Exception as exc:
                        task_result = task_failure(i, test_file, impl_files, exc)
                    keep(task_result)
            else:
                max_workers = config.max_parallel_tasks
                if max_workers is None:
                    max_workers = min(len(task_items), os.cpu_count() or 1)
                else:
                    max_workers = int(max_workers)
                max_workers = max(1, min(max_workers, len(task_items)))
                print(f"Parallel mode enabled with max_workers={max_workers}")

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {}
                    for i, (test_file, impl_files) in enumerate(task_items, start=1):
                        print(f"[{i}/{len(task_items)}] queued {repo_relative_id(test_file)}")
                        future = executor.submit(
                            run_task, config, test_file, impl_files, str(run_repo_copies_dir), run_dir=run_dir
                        )
                        futures[future] = (i, test_file, impl_files)

                    for future in as_completed(futures):
                        i, test_file, impl_files = futures[future]
                        try:
                            task_result = future.result()
                            print(f"[{i}/{len(task_items)}] finished {repo_relative_id(test_file)}")
                        except Exception as exc:
                            task_result = task_failure(i, test_file, impl_files, exc)
                        keep(task_result)
        finally:
            run_egress_events = teardown_shared_egress_proxy(shared_proxy_state)
            observed = sorted({e["domain"] for e in run_egress_events if e.get("domain")})
            blocked = sorted({e["domain"] for e in run_egress_events if e.get("domain") and e.get("blocked")})
            print(f"[egress] run-level requests={len(run_egress_events)} domains={', '.join(observed) or '-'}")
            if blocked:
                print(f"[egress] run-level BLOCKED domains: {', '.join(blocked)}")

        run = replace(
            run,
            status=RunStatus.COMPLETED,
            finished_at_utc=now_utc(),
            tasks=task_results,
            egress_events=run_egress_events,
        )
        write_run_result(run_dir, run)
        print(json.dumps(run.summary(), indent=2))
        print(f"Saved run logs to: {run_dir}")
    finally:
        if run_repo_copies_dir is not None:
            remove_run_repo_copies_dir_if_empty(run_repo_copies_dir)
        if not docker_was_running:
            stop_docker_daemon()


def run_experiments(experiments: list[SimpleNamespace], exp_index: int | None = None) -> None:
    """Run each experiment's overrides on top of DEFAULTS, or only the one at `exp_index` (1-based)."""
    total = len(experiments)
    if exp_index is not None:
        if exp_index < 1 or exp_index > total:
            raise ValueError(f"--exp-index must be between 1 and {total}, got {exp_index}.")
        selected: list[tuple[int, SimpleNamespace]] = [(exp_index, experiments[exp_index - 1])]
    else:
        selected = list(enumerate(experiments, start=1))

    for exp_idx, exp_overrides in selected:
        config = DEFAULTS.merge(exp_overrides)
        effective_run_name = config.run_name or default_agent_run_name(config.codex_model, task_selection_label(config))
        print(f"=== Starting experiment {exp_idx}/{total} (run_name={effective_run_name}) ===")
        _run_config(config)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    add_tasklist_dirs(args.tasklist_dir)
    if not args.config.exists():
        raise SystemExit(f"experiment config {args.config} not found; copy experiment.example.py to experiment.py or pass --config")
    run_experiments(_experiments_from_module(_load_config_module(args.config)), args.exp_index)


if __name__ == "__main__":
    main()
