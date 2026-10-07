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
- the mutation catalogue lists the tasks that have mutations, and one task's mutations with
  their validation, when a report has one, and the files they patch, when they are checked out
- `/` and `/assets/...` serve the built page when a `dist/` directory exists
- the start-up build is skipped when `dist/` exists, and refused plainly when npm is missing
"""

import json
import socket
from dataclasses import asdict, replace
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
from daml_agent_benchmark.locations import configure, locations, task_file_name
from daml_agent_benchmark.server.mutation_catalogue import short_names

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
        workspace_audit=WorkspaceAudit.from_changes([], [f"daml/{name}.daml"], ["daml/Test.daml"]),
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
        answer_file_snapshots=[],
        attempts=attempt,
    )


@pytest.fixture
def logs_dir(tmp_path):
    """A logs directory holding one run of three tasks: one passed its tests, one failed them, one is still queued."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(json.dumps({"codex_model": "gpt-6-luna", "max_task_runtime_seconds": 600, "task_kind": "implementation"}), encoding="utf-8")
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
        return cost_breakdown("gpt-6-luna", input_tokens, output_tokens, cached).total

    per_request = price(200_000, 1_000, 150_000) + price(200_000, 2_000, 190_000)

    run_dir = tmp_path / "run-2"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(json.dumps({"codex_model": "gpt-6-luna", "max_task_runtime_seconds": 600, "task_kind": "implementation"}), encoding="utf-8")
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


CATALOGUE_REPOS_YAML = """\
example-fetched-repo:
  url: null
  commit: null
  license: null
  build_tool: daml
example-unfetched-repo:
  url: null
  commit: null
  license: null
  build_tool: daml
"""

CATALOGUE_TASKS_YAML = """\
- repo: example-fetched-repo
  test: daml/Test/Order.daml
  impl: [daml/Shop/Order.daml]
- repo: example-fetched-repo
  test: daml/Test/Plain.daml
  impl: [daml/Shop/Plain.daml]
- repo: example-unfetched-repo
  test: daml/Test/Order.daml
  impl: [daml/Shop/Order.daml]
"""

ORDER_PATCH = """\
diff --git a/daml/Shop/Order.daml b/daml/Shop/Order.daml
--- a/daml/Shop/Order.daml
+++ b/daml/Shop/Order.daml
@@ -2,3 +2,3 @@ template Order
   with
-    ensure qty > 0
+    ensure qty >= 0
   where
"""


def _mutation_file(task_id: str, ids: list[str]) -> str:
    entries = "".join(
        f"  - id: {mid}\n    source: {'real-bug' if mid.startswith('fix') else 'llm'}\n    kind: validation\n"
        f"    reason: The check lets an empty order through.\n    patch: |\n"
        + "".join(f"      {line}\n" for line in ORDER_PATCH.splitlines())
        for mid in ids
    )
    return f"task: {task_id}\nmutations:\n{entries}"


@pytest.fixture
def catalogue_client(tmp_path):
    """A task list of three tasks: `example-fetched-repo`'s order task has two mutations and a
    validation report that lists one of them, its plain task has none, and
    `example-unfetched-repo`'s task has mutations but no checkout and no report."""
    tasklist = tmp_path / "tasklist"
    (tasklist / "mutations").mkdir(parents=True)
    (tasklist / "repos.yaml").write_text(CATALOGUE_REPOS_YAML, encoding="utf-8")
    (tasklist / "tasks.yaml").write_text(CATALOGUE_TASKS_YAML, encoding="utf-8")
    for task_id, ids in (("example-fetched-repo/daml/Test/Order.daml", ["qty-allow-zero", "fix-qty-check"]), ("example-unfetched-repo/daml/Test/Order.daml", ["qty-allow-zero"])):
        (tasklist / "mutations" / f"{task_file_name(task_id)}.yaml").write_text(_mutation_file(task_id, ids), encoding="utf-8")
    sources = tmp_path / "sources"
    (sources / "example-fetched-repo" / "daml" / "Shop").mkdir(parents=True)
    (sources / "example-fetched-repo" / "daml" / "Shop" / "Order.daml").write_text("template Order\n  with\n    ensure qty > 0\n  where\n", encoding="utf-8")
    report = {
        "task": "example-fetched-repo/daml/Test/Order.daml",
        "ground_truth_seconds": 3.0,
        "ground_truth_scripts": ["daml/Test/Order.daml:testZero", "daml/Test/Order.daml:testOne", "daml/Test/Utils.daml:setup"],
        "mutations": [
            {"id": "qty-allow-zero", "source": "llm", "kind": "validation", "target_script": "testZero", "outcome": "killed",
             "outcome_detail": None, "newly_failing": {"daml/Test/Order.daml:testZero": "expected failure"}, "seconds": 2.0},
        ],
    }
    logs = tmp_path / "logs"
    (logs / "mutations").mkdir(parents=True)
    (logs / "mutations" / "example-fetched-repo__daml__Test__Order.daml.json").write_text(json.dumps(report), encoding="utf-8")
    saved = asdict(locations)
    configure(tasklist_dirs=(tasklist,), extra_code_dirs=(), sources_root=sources, logs_dir=logs)
    yield TestClient(create_app(frontend_dist=tmp_path / "no-frontend"))
    configure(**saved)


def test_the_catalogue_lists_the_tasks_that_have_mutations(catalogue_client) -> None:
    tasks = catalogue_client.get("/api/agent/mutations").json()["tasks"]
    assert [(t["task_id"], t["file_name"], t["short_name"]) for t in tasks] == [
        ("example-fetched-repo/daml/Test/Order.daml", "example-fetched-repo__daml__Test__Order.daml", "Order"),
        ("example-unfetched-repo/daml/Test/Order.daml", "example-unfetched-repo__daml__Test__Order.daml", "Order"),
    ]
    assert tasks[0]["mutations"] == [
        {"id": "qty-allow-zero", "kind": "validation", "source": "llm"},
        {"id": "fix-qty-check", "kind": "validation", "source": "real-bug"},
    ]


def test_a_tasks_mutations_come_with_their_validation_and_patched_files(catalogue_client) -> None:
    detail = catalogue_client.get("/api/agent/mutations/example-fetched-repo__daml__Test__Order.daml").json()
    # The test file's own scripts, without the helper its module imports.
    assert detail["scripts"] == 2
    validated, unvalidated = detail["mutations"]
    assert validated["patch"] == ORDER_PATCH
    assert validated["validation"] == {"outcome": "killed", "outcome_detail": None, "newly_failing": ["daml/Test/Order.daml:testZero"]}
    assert unvalidated["validation"] is None
    assert detail["files"] == {"daml/Shop/Order.daml": "template Order\n  with\n    ensure qty > 0\n  where\n"}


def test_a_task_without_a_report_or_a_checkout_shows_its_patches_alone(catalogue_client) -> None:
    detail = catalogue_client.get("/api/agent/mutations/example-unfetched-repo__daml__Test__Order.daml").json()
    assert detail["scripts"] is None
    assert [m["validation"] for m in detail["mutations"]] == [None]
    assert detail["files"] == {}
    assert catalogue_client.get("/api/agent/mutations/example-fetched-repo__daml__Test__Plain.daml").status_code == 404


def test_short_names_grow_until_they_are_unique_within_a_repository() -> None:
    names = short_names([
        "lib/daml/Shop/Order/Test.daml",
        "lib/daml/Bank/Order/Test.daml",
        "lib/src/Account/Model.daml",
        "other/daml/Shop/Order/Test.daml",
    ])
    assert names == {
        "lib/daml/Shop/Order/Test.daml": "Shop / Order",
        "lib/daml/Bank/Order/Test.daml": "Bank / Order",
        "lib/src/Account/Model.daml": "Account / Model",
        "other/daml/Shop/Order/Test.daml": "Shop / Order",
    }
    assert short_names(["lib/x/Core/Order/Test.daml", "lib/y/Core/Order/Test.daml"]) == {
        "lib/x/Core/Order/Test.daml": "x / Core / Order",
        "lib/y/Core/Order/Test.daml": "y / Core / Order",
    }
