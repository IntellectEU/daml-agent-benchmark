"""The runner's own bookkeeping, with everything around the tasks faked.

Task selection, the container and proxy set-up, Docker and the task run itself are
faked. The tests check two things:

- a run writes one record per task and a `run.json` that says completed and lists them, in both execution modes
- the runner stops Docker at the end only when Docker was off before the run
- a run that fails before its tasks start still tears down the egress proxy and stops Docker
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from daml_agent_benchmark import runner
from daml_agent_benchmark.config import DEFAULTS
from daml_agent_benchmark.records import Grade, LiveState, TaskResult, now_utc

TASKS = {"/sources/repo/daml/ATest.daml": ["/sources/repo/daml/A.daml"], "/sources/repo/daml/BTest.daml": ["/sources/repo/daml/B.daml"]}


def _queued(test_file: str, impl_files: list[str]) -> TaskResult:
    name = Path(test_file).stem
    return TaskResult(
        task_id=f"repo/daml/{name}.daml",
        task_safe_name=f"repo__daml__{name}.daml",
        test_file=f"repo/daml/{name}.daml",
        impl_files=[f"repo/daml/{Path(f).name}" for f in impl_files],
        live_state=LiveState.QUEUED,
        queued_at_utc=now_utc(),
        started_at_utc=None,
        finished_at_utc=None,
        live_updated_at_utc=now_utc(),
        repo_copy=None,
        findings=[],
        grade=None,
        ground_truth_control=None,
        repo_copy_integrity=None,
        answer_file_snapshots=[],
        attempts=None,
    )


def _completed(config, test_file, impl_files, run_repo_copies_dir, run_dir=None) -> TaskResult:
    return replace(
        _queued(test_file, impl_files),
        live_state=LiveState.COMPLETED,
        started_at_utc=now_utc(),
        finished_at_utc=now_utc(),
        grade=Grade(True, True, True, {"main": True}, None, None, None),
    )


def _fake_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, docker_was_running: bool = True) -> tuple[Path, list[str]]:
    """Fake everything around the tasks. Returns the run directory and a list that records Docker stops."""
    run_dir = tmp_path / "run"
    docker_stops: list[str] = []
    for name, value in {
        "get_selected_tasks": lambda config: dict(TASKS),
        "prepare_agent_run": lambda tasks, config: ({}, "codex"),
        "assert_nix_shell_preflight": lambda tasks: None,
        "build_run_identity": lambda config: ("run-test", "test"),
        "make_run_dir": lambda run_id: (run_dir.mkdir(), run_dir)[1],
        "make_run_repo_copies_dir": lambda: tmp_path / "copies",
        "stage_code_snapshot_for_run": lambda *args: {},
        "build_config_dump": lambda config: {},
        "repo_relative_id": lambda path: Path(path).name,
        "task_live_stdout_events_path": lambda run_dir, test_file: run_dir / "tasks_live" / f"{Path(test_file).stem}.events.jsonl",
        "queued_task_result": _queued,
        "run_task": _completed,
        "teardown_shared_egress_proxy": lambda state: [],
        "docker_daemon_is_running": lambda: docker_was_running,
        "stop_docker_daemon": lambda: docker_stops.append("stop"),
        "remove_run_repo_copies_dir_if_empty": lambda path: None,
    }.items():
        monkeypatch.setattr(runner, name, value)
    return run_dir, docker_stops


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
def test_run_ends_with_a_completed_record(tmp_path, monkeypatch, parallel) -> None:
    run_dir, _ = _fake_run(tmp_path, monkeypatch)

    runner._run_config(replace(DEFAULTS, run_tasks_in_parallel=parallel, max_parallel_tasks=2))

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "completed"
    assert run["finished_at_utc"] is not None
    assert len(run["tasks"]) == 2
    assert len(list((run_dir / "tasks").glob("*.json"))) == 2


@pytest.mark.parametrize("docker_was_running", [True, False], ids=["already-running", "started-by-run"])
def test_docker_is_stopped_only_when_the_run_started_it(tmp_path, monkeypatch, docker_was_running) -> None:
    _, docker_stops = _fake_run(tmp_path, monkeypatch, docker_was_running=docker_was_running)

    runner._run_config(DEFAULTS)

    assert docker_stops == ([] if docker_was_running else ["stop"])


def test_a_failure_before_the_tasks_still_cleans_up(tmp_path, monkeypatch) -> None:
    _, docker_stops = _fake_run(tmp_path, monkeypatch, docker_was_running=False)
    teardowns: list[object] = []
    monkeypatch.setattr(runner, "teardown_shared_egress_proxy", lambda state: (teardowns.append(state), [])[1])

    def fail_staging(config, run_dir):
        raise RuntimeError("skill staging failed")

    monkeypatch.setattr(runner, "stage_skill_for_run", fail_staging)

    with pytest.raises(RuntimeError, match="skill staging failed"):
        runner._run_config(replace(DEFAULTS, skill="some-skill"))

    assert teardowns == [{}]
    assert docker_stops == ["stop"]
