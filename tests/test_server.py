"""Tests for the dashboard server.

The tests build a small run to serve: two tasks, one passed and one failed, written to a
temporary directory with the record classes from `daml_agent_benchmark.records`. Then they
start the FastAPI app inside the test process, pointed at that directory, and call each
route:

- `/api/agent/matrix` returns one cell per task, and caches its rows
- a finished task shows its recorded cost, a running one its live events priced per request
- task detail returns the task's record with its per-event costs, and 404 for a task that is not in the run
- archive moves the run into `z_archive/`, and delete removes it
- a run id that would leave the logs directory is refused
- `/` and `/assets/...` serve the built page when a `dist/` directory exists
- the start-up build is skipped when `dist/` exists, and refused plainly when npm is missing
"""

import json
import socket
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from daml_agent_benchmark.records import (
    AttemptResult,
    EgressSummary,
    Grade,
    LiveState,
    RunResult,
    RunStatus,
    RuntimeIdentity,
    TaskResult,
    TokenUsage,
    WorkspaceAudit,
    write_run_result,
    write_task_result,
)
from daml_agent_benchmark.server import app as server_app
from daml_agent_benchmark.server.app import create_app, ensure_frontend_built, ensure_port_free
from daml_agent_benchmark.server import runs
from daml_agent_benchmark.server.runs import MATRIX_CACHE_FILENAME
from daml_agent_benchmark.locations import configure, locations

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _task(name: str, *, tests_passed: bool) -> TaskResult:
    attempt = AttemptResult(
        command=["codex"],
        model="gpt-6-luna",
        approval_policy="never",
        event_schema_version="codex_canonical_v1",
        final_message="done",
        sub_agents=[],
        returncode=0,
        wall_seconds=12.5,
        timed_out=False,
        export_timed_out=False,
        quota_limit_detected=False,
        quota_retry_count=0,
        usage=TokenUsage(1000, 100, 200),
        usage_by_thread={},
        usage_complete=True,
        usd_cost=0.02,
        input_usd=0.01,
        cached_input_usd=0.0,
        output_usd=0.01,
        stdout=None,
        stderr=None,
        stdout_events=None,
        forbidden_tool_calls=[],
        allowed_mcp_tool_calls=[],
        suspicious_commands=[],
        out_of_workspace_writes=[],
        egress=EgressSummary.from_events([], ["openai.com"], []),
        runtime_identity=RuntimeIdentity("gpt-6-luna", "gpt-6-luna", True, None, None, True, "openai", "0.1"),
        workspace_audit=WorkspaceAudit.from_changes([], [f"daml/{name}.daml"], "daml/Test.daml"),
    )
    return TaskResult(
        task_id=f"repo/daml/{name}Test.daml",
        task_safe_name=f"repo__daml__{name}Test.daml",
        test_file=f"repo/daml/{name}Test.daml",
        impl_files=[f"repo/daml/{name}.daml"],
        live_state=LiveState.COMPLETED,
        queued_at_utc=NOW,
        started_at_utc=NOW,
        finished_at_utc=NOW,
        live_updated_at_utc=NOW,
        repo_copy=f"260102/repo_____{name}",
        findings=[],
        grade=Grade(True, True, tests_passed, {f"{name}Test:main": tests_passed}, None, None, None),
        ground_truth_control=None,
        repo_copy_integrity=None,
        impl_file_snapshots=[],
        attempts=attempt,
    )


@pytest.fixture
def logs_dir(tmp_path):
    """A logs directory holding one run of three tasks: one passed its tests, one failed them, one is still queued."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(json.dumps({"codex_auth_mode": "api_key", "codex_model": "gpt-6-luna", "max_task_runtime_seconds": 600}), encoding="utf-8")
    tasks = [_task("A", tests_passed=True), _task("B", tests_passed=False)]
    for task in tasks:
        write_task_result(run_dir, task)
    # A task still waiting for a worker: written once when the run was queued, never moved since.
    queued = replace(_task("C", tests_passed=True), live_state=LiveState.QUEUED, started_at_utc=None, finished_at_utc=None, attempts=None, grade=None)
    write_task_result(run_dir, queued)
    write_run_result(run_dir, RunResult("run-1", "smoke", RunStatus.IN_PROGRESS, NOW, None, [*tasks, queued], None, ["openai.com"], []))
    previous = locations.logs_dir
    configure(logs_dir=tmp_path)
    yield tmp_path
    configure(logs_dir=previous)


@pytest.fixture
def client(logs_dir, tmp_path):
    return TestClient(create_app(frontend_dist=tmp_path / "no-frontend"))


def test_built_frontend_is_served_when_present(logs_dir, tmp_path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>page</html>", encoding="utf-8")
    (dist / "assets" / "index-abc.js").write_text("// bundle", encoding="utf-8")
    client = TestClient(create_app(frontend_dist=dist))
    assert client.get("/").text == "<html>page</html>"
    assert client.get("/assets/index-abc.js").text == "// bundle"
    assert client.get("/api/agent/matrix").status_code == 200


def test_health(client) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_matrix_has_a_cell_per_task_and_caches_its_rows(client, logs_dir) -> None:
    matrix = client.get("/api/agent/matrix").json()
    assert matrix["task_ids"] == ["repo/daml/ATest.daml", "repo/daml/BTest.daml", "repo/daml/CTest.daml"]
    assert set(matrix["task_header_colors"]) == set(matrix["task_ids"])
    (item,) = matrix["items"]
    assert item["task_statuses"]["repo/daml/ATest.daml"]["status_code"] == "success"
    assert item["task_statuses"]["repo/daml/BTest.daml"]["status_code"] == "tests_failed"
    assert item["task_statuses"]["repo/daml/CTest.daml"]["status_code"] == "queued"
    assert item["status_counts"] == {"success": 1, "tests_failed": 1, "queued": 1}
    assert matrix["package_task_ids"] and not set(matrix["task_ids"]) & set(matrix["package_task_ids"])
    assert (logs_dir / "run-1" / MATRIX_CACHE_FILENAME).exists()
    assert client.get("/api/agent/matrix").json() == matrix


def test_task_detail_returns_the_record(client) -> None:
    response = client.get("/api/agent/runs/run-1/tasks/detail", params={"task_id": "repo/daml/ATest.daml"})
    assert response.status_code == 200
    detail = response.json()
    assert detail["task"]["task_id"] == "repo/daml/ATest.daml"
    assert detail["is_live"] is False
    assert detail["cell"]["status_code"] == "success"
    assert detail["terminal_log"]["content"] is None
    # The task recorded no events, so its cost has nothing to split over.
    assert detail["event_costs"] == {**detail["event_costs"], "model": "gpt-6-luna", "priced": True, "items": {}, "requests": []}
    missing = client.get("/api/agent/runs/run-1/tasks/detail", params={"task_id": "repo/daml/Nope.daml"})
    assert missing.status_code == 404


def test_archive_moves_the_run_and_delete_removes_it(client, logs_dir) -> None:
    archived = client.post("/api/agent/runs/archive", json={"run_ids": ["run-1"], "reason": "done"}).json()
    assert archived["archived"] == ["run-1"]
    archive_dir = logs_dir / "z_archive" / "run-1"
    assert json.loads((archive_dir / "archive_meta.json").read_text(encoding="utf-8"))["reason"] == "done"
    assert client.get("/api/agent/matrix").json()["items"] == []
    items = client.get("/api/agent/matrix", params={"include_archived": True}).json()["items"]
    assert items[0]["archived"] is True

    deleted = client.post("/api/agent/runs/delete", json={"run_ids": ["run-1"]}).json()
    assert deleted["deleted"] == ["run-1"]
    assert not archive_dir.exists()


def test_run_ids_stay_inside_the_logs_directory(client) -> None:
    assert client.post("/api/agent/runs/delete", json={"run_ids": ["../elsewhere"]}).status_code == 400
    assert client.post("/api/agent/runs/archive", json={"run_ids": []}).status_code == 400


def test_startup_build_only_runs_when_the_page_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    built = tmp_path / "built"
    (built / "dist").mkdir(parents=True)
    (built / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    ensure_frontend_built(built)
    with pytest.raises(SystemExit, match="npm is not installed"):
        ensure_frontend_built(tmp_path / "unbuilt")


def test_a_taken_port_stops_the_server_before_the_page_is_built(monkeypatch) -> None:
    def build(**kwargs) -> None:
        raise AssertionError("the page was built although the port is taken")

    monkeypatch.setattr(server_app, "ensure_frontend_built", build)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(SystemExit, match=rf"Port {port} .*--port"):
            server_app.main(["--port", str(port)])
    # Once the listener is gone, the same port passes the check.
    ensure_port_free("127.0.0.1", port)


def _usage_record(line_no: int, last: tuple[int, int, int], total: tuple[int, int, int]) -> dict:
    """A stored usage event for one request, with its keys in the order the driver writes them."""

    def counts(values: tuple[int, int, int]) -> dict:
        return {"inputTokens": values[0], "cachedInputTokens": values[1], "outputTokens": values[2]}

    event = {"type": "thread.token_usage.updated", "thread_id": "main", "token_usage": {"last": counts(last), "total": counts(total)}}
    return {"line_no": line_no, "event": event}


# Two requests of 200,000 input tokens each.
REQUEST_RECORDS = [
    {"line_no": 1, "event": {"type": "thread.started", "thread": {"id": "main"}}},
    _usage_record(2, (200_000, 150_000, 1_000), (200_000, 150_000, 1_000)),
    {"line_no": 3, "event": {"type": "item.completed", "thread_id": "main", "item": {"type": "agent_message", "id": "m1", "text": "hi"}}},
    _usage_record(4, (200_000, 190_000, 2_000), (400_000, 340_000, 3_000)),
    # A repeat of the last report, as an interrupted turn leaves.
    _usage_record(5, (200_000, 190_000, 2_000), (400_000, 340_000, 3_000)),
]


def test_matrix_costs(tmp_path) -> None:
    """A finished task shows the cost the runner recorded from its events; a running one is priced the same way from the events it has streamed so far."""
    from daml_agent_benchmark.pricing import cost_breakdown
    from daml_agent_benchmark.records import task_live_events_path

    def price(input_tokens: int, output_tokens: int, cached: int) -> float:
        return sum(cost_breakdown("gpt-6-luna", input_tokens, output_tokens, cached).values())

    per_request = price(200_000, 1_000, 150_000) + price(200_000, 2_000, 190_000)

    run_dir = tmp_path / "run-2"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(json.dumps({"codex_auth_mode": "api_key", "codex_model": "gpt-6-luna", "max_task_runtime_seconds": 600}), encoding="utf-8")
    finished = _task("A", tests_passed=True)
    attempt = replace(
        finished.attempts,
        usage=TokenUsage(400_000, 3_000, 340_000),
        usd_cost=0.6,
        input_usd=0.1,
        cached_input_usd=0.2,
        output_usd=0.3,
        stdout_events=REQUEST_RECORDS,
    )
    finished = replace(finished, attempts=attempt)
    running = replace(
        _task("D", tests_passed=True),
        live_state=LiveState.RUNNING,
        live_updated_at_utc=datetime.now(timezone.utc),
        finished_at_utc=None,
        attempts=None,
        grade=None,
    )
    for task in (finished, running):
        write_task_result(run_dir, task)
    live_events = task_live_events_path(run_dir, running.task_safe_name)
    live_events.parent.mkdir(parents=True, exist_ok=True)
    live_events.write_text("".join(json.dumps(record) + "\n" for record in REQUEST_RECORDS), encoding="utf-8")
    tasks = [finished, running]
    write_run_result(run_dir, RunResult("run-2", "pricing", RunStatus.IN_PROGRESS, NOW, None, tasks, None, ["openai.com"], []))
    previous = locations.logs_dir
    configure(logs_dir=tmp_path)
    try:
        client = TestClient(create_app(frontend_dist=tmp_path / "no-frontend"))
        (item,) = client.get("/api/agent/matrix").json()["items"]
        detail = client.get("/api/agent/runs/run-2/tasks/detail", params={"task_id": finished.task_id}).json()
        live_detail = client.get("/api/agent/runs/run-2/tasks/detail", params={"task_id": running.task_id}).json()
    finally:
        configure(logs_dir=previous)

    finished_cell = item["task_statuses"][finished.task_id]
    assert (finished_cell["usd_cost"], finished_cell["input_cost"], finished_cell["cached_input_cost"], finished_cell["output_cost"]) == (0.6, 0.1, 0.2, 0.3)
    assert finished_cell["input_tokens"] == 400_000
    assert not finished_cell["usd_cost_is_lower_bound"]
    # The task view prices the events themselves, not the record.
    assert detail["event_costs"]["total_usd"] == pytest.approx(per_request)

    running_cell = item["task_statuses"][running.task_id]
    assert running_cell["status_code"] == "running"
    assert running_cell["usd_cost"] == pytest.approx(per_request)
    parts = running_cell["input_cost"] + running_cell["cached_input_cost"] + running_cell["output_cost"]
    assert parts == pytest.approx(running_cell["usd_cost"])
    assert running_cell["input_tokens"] == 400_000
    assert running_cell["usd_cost_is_lower_bound"]
    assert live_detail["event_costs"]["total_usd"] == running_cell["usd_cost"]

    cells = [finished_cell, running_cell]
    assert item["summary"]["total_usd_cost"] == pytest.approx(sum(cell["usd_cost"] for cell in cells))
    assert item["summary"]["usage_complete"] is False


def test_a_partly_priced_run_shows_its_known_cost_as_a_lower_bound() -> None:
    def cell(cost: float | None, tokens: int | None = 10) -> dict:
        return {"usd_cost": cost, "input_tokens": tokens, "usd_cost_is_lower_bound": False}

    def summary(cells: list[dict]) -> dict:
        return runs._summary_with_cell_costs(
            {"total_usd_cost": None, "usage_complete": True}, {str(i): c for i, c in enumerate(cells)}
        )

    assert summary([cell(0.5), cell(0.25)]) == {"total_usd_cost": 0.75, "usage_complete": True}
    # A queued task has used no tokens, so it counts for neither.
    assert summary([cell(0.5), cell(None, tokens=None)]) == {"total_usd_cost": 0.5, "usage_complete": True}
    assert summary([cell(0.5), cell(None)]) == {"total_usd_cost": 0.5, "usage_complete": False}
    assert summary([cell(None), cell(None)]) == {"total_usd_cost": None, "usage_complete": True}
