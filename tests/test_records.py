"""The records a run writes: round trips, sidecars, and the run-level numbers computed from tasks."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from daml_agent_benchmark.records import (
    AttemptResult,
    EgressSummary,
    Finding,
    Grade,
    LiveState,
    RunResult,
    RunStatus,
    RuntimeIdentity,
    TaskFlag,
    TaskResult,
    TokenUsage,
    WorkspaceAudit,
    read_task_result,
    task_record_path,
    write_run_result,
    write_task_result,
)

# One fixed instant for every timestamp in these fixtures, so a record's datetimes
# survive the trip through their ISO strings and back with nothing to compare against
# the clock. The value itself means nothing.
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _attempt(**overrides) -> AttemptResult:
    fields = dict(
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
        stdout="out\n",
        stderr="err\n",
        stdout_events=[{"event": {"type": "turn.completed"}}],
        forbidden_tool_calls=[],
        allowed_mcp_tool_calls=[],
        suspicious_commands=[],
        out_of_workspace_writes=[],
        egress=EgressSummary.from_events(
            [{"host": "api.openai.com", "domain": "api.openai.com", "blocked": False}], ["openai.com"], []
        ),
        runtime_identity=RuntimeIdentity("gpt-6-luna", "gpt-6-luna", True, None, None, True, "openai", "0.1"),
        workspace_audit=WorkspaceAudit.from_changes([], ["daml/Impl.daml"], "daml/Test.daml"),
    )
    fields.update(overrides)
    return AttemptResult(**fields)


def _task(name: str, *, tests_passed: bool = True, findings: list[Finding] | None = None, **overrides) -> TaskResult:
    fields = dict(
        task_id=f"repo/{name}",
        task_safe_name=f"repo__{name}",
        test_file=f"repo/daml/{name}Test.daml",
        impl_files=[f"repo/daml/{name}.daml"],
        live_state=LiveState.COMPLETED,
        queued_at_utc=NOW,
        started_at_utc=NOW,
        finished_at_utc=NOW,
        live_updated_at_utc=NOW,
        repo_copy=f"260917/repo_____{name}",
        findings=findings or [],
        grade=Grade(True, True, tests_passed, {"Test:main": tests_passed}, None, None, None),
        ground_truth_control=None,
        repo_copy_integrity=None,
        impl_file_snapshots=[],
        attempts=_attempt(),
    )
    fields.update(overrides)
    return TaskResult(**fields)


def test_task_record_round_trips_with_enums_datetimes_and_nesting() -> None:
    task = _task("a", findings=[Finding(TaskFlag.BLOCKED_EGRESS, "tried pypi.org")])
    record = task.to_record()
    assert record["live_state"] == "completed"
    assert record["started_at_utc"] == "2026-01-02T03:04:05+00:00"
    assert record["findings"] == [{"flag": "blocked_egress", "detail": "tried pypi.org"}]
    assert record["attempts"]["usage"] == {"input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": 200}
    assert TaskResult.from_record(record) == task


def test_attempt_record_missing_a_field_is_refused() -> None:
    record = _attempt(usd_cost=0.6, input_usd=0.1, cached_input_usd=0.2, output_usd=0.3).to_record()
    assert AttemptResult.from_record(record).output_usd == 0.3
    del record["output_usd"]
    with pytest.raises(TypeError, match="output_usd"):
        AttemptResult.from_record(record)


def test_unknown_record_fields_are_refused() -> None:
    """A field this model does not know stops the read instead of being dropped.

    Silently ignoring it would hand back a record missing whatever that field held, and
    writing it out again would delete it from the file.
    """
    record = _task("a").to_record()
    record["written_by_a_newer_version"] = {}
    with pytest.raises(ValueError, match="unknown fields"):
        TaskResult.from_record(record)


def test_paths_never_enter_a_record() -> None:
    with pytest.raises(TypeError):
        replace(_task("a"), repo_copy=Path("/abs/copy")).to_record()


def test_output_goes_to_sidecars_and_comes_back_on_request(tmp_path) -> None:
    task = _task("a")
    path = write_task_result(tmp_path, task)
    assert path == task_record_path(tmp_path, "repo__a")
    record_text = path.read_text(encoding="utf-8")
    assert "out\\n" not in record_text and "turn.completed" not in record_text
    assert (tmp_path / "tasks" / "repo__a.stdout.txt").read_text(encoding="utf-8") == "out\n"
    assert (tmp_path / "tasks" / "repo__a.events.jsonl").read_text(encoding="utf-8").count("\n") == 1

    slim = read_task_result(path)
    assert slim.attempts.stdout is None and slim.attempts.stdout_events is None
    assert slim == task.without_output()
    assert read_task_result(path, output=True) == task


def test_findings_decide_trust_and_gradability() -> None:
    clean = _task("a")
    warned = _task("b", findings=[Finding(TaskFlag.SUSPICIOUS_COMMANDS, "env")])
    violated = _task("c", findings=[Finding(TaskFlag.FORBIDDEN_TOOL_CALLS, "web_search")])
    broken = _task("d", findings=[Finding(TaskFlag.INFRA_FAILURE, "boom")], grade=None, attempts=None)
    assert [t.untrusted() for t in (clean, warned, violated, broken)] == [False, False, True, True]
    assert [t.gradable() for t in (clean, warned, violated, broken)] == [True, True, False, False]
    assert violated.flags() == [TaskFlag.FORBIDDEN_TOOL_CALLS] and violated.has(TaskFlag.FORBIDDEN_TOOL_CALLS)


def test_run_numbers_are_computed_from_the_tasks() -> None:
    run = RunResult(
        run_id="r1",
        run_name="luna",
        status=RunStatus.COMPLETED,
        created_at_utc=NOW,
        finished_at_utc=NOW,
        tasks=[
            _task("a"),
            _task("b", tests_passed=False),
            _task("c", findings=[Finding(TaskFlag.FORBIDDEN_TOOL_CALLS, "web_search")]),  # passes, but untrusted
            _task("d", findings=[Finding(TaskFlag.INFRA_FAILURE, "boom")], grade=None, attempts=None),
        ],
        egress_events=[
            {"host": "api.openai.com", "domain": "api.openai.com", "blocked": False},
            {"host": "pypi.org", "domain": "pypi.org", "blocked": True},
        ],
        egress_allowed_domain_suffixes=["openai.com"],
        egress_allowed_hosts=[],
    )
    assert len(run.gradable()) == 2 and len(run.untrusted()) == 2
    assert (run.syntax_passed(), run.compile_passed(), run.tests_passed()) == (2, 2, 1)
    assert run.tests_pass_rate() == 0.5
    assert run.usage() == TokenUsage(3000, 300, 600)
    assert run.usage_complete() is True
    assert run.total_usd_cost() == pytest.approx(0.06)
    assert run.agent_wall_seconds() == pytest.approx(37.5)
    assert run.flagged() == {TaskFlag.FORBIDDEN_TOOL_CALLS: ["repo/c"], TaskFlag.INFRA_FAILURE: ["repo/d"]}
    # The proxy saw a blocked request no attempt attributed to itself.
    assert run.run_egress().blocked_domains == ["pypi.org"]
    assert run.unattributed_egress() == [{"host": "pypi.org", "domain": "pypi.org", "blocked": True}]
    summary = run.summary()
    assert summary["gradable_tasks"] == 2 and summary["flagged"] == {"forbidden_tool_calls": ["repo/c"], "infra_failure": ["repo/d"]}


def test_run_wall_time_leaves_out_gaps_with_no_task_in_flight() -> None:
    """A run merged from two sittings a day apart counts only the time its tasks were in flight."""
    hour = timedelta(hours=1)

    def span(name: str, start: datetime, end: datetime) -> TaskResult:
        return _task(name, queued_at_utc=start, started_at_utc=start, finished_at_utc=end)

    day_later = NOW + timedelta(days=1)
    tasks = [span("a", NOW, NOW + hour), span("b", NOW + hour / 2, NOW + 2 * hour), span("c", day_later, day_later + hour)]
    run = RunResult("r1", "merged", RunStatus.COMPLETED, NOW, day_later + hour, tasks, None, ["openai.com"], [])
    # Two overlapping tasks cover 2 h, and the third 1 h a day later: 3 h, not 25.
    assert run.wall_seconds() == pytest.approx(3 * 3600)
    # A run still going has no wall time yet.
    assert replace(run, finished_at_utc=None).wall_seconds() is None


def test_unknown_cost_makes_the_run_total_unknown_but_usage_stays_summed() -> None:
    run = RunResult(
        "r1", "luna", RunStatus.COMPLETED, NOW, NOW,
        [_task("a"), _task("b", findings=[Finding(TaskFlag.COST_UNKNOWN, "no price")], attempts=_attempt(usd_cost=None))],
        None, ["openai.com"], [],
    )
    assert run.total_usd_cost() is None
    assert run.usage() == TokenUsage(2000, 200, 400)


def test_run_record_lists_its_tasks_by_path_and_loads_them_back(tmp_path) -> None:
    tasks = [_task("a"), _task("b", tests_passed=False)]
    run = RunResult("r1", "luna", RunStatus.COMPLETED, NOW, NOW, tasks, [{"host": "h", "domain": "h", "blocked": False}], ["openai.com"], [])
    for task in tasks:
        write_task_result(tmp_path, task)
    (tmp_path / "egress_events.json").write_text('[{"host": "h", "domain": "h", "blocked": false}]', encoding="utf-8")
    write_run_result(tmp_path, run)

    record = (tmp_path / "run.json").read_text(encoding="utf-8")
    assert '"tasks/repo__a.json"' in record and "egress_events" not in record

    loaded = RunResult.load(tmp_path)
    assert loaded == replace(run, tasks=[t.without_output() for t in tasks])
    assert loaded.tests_passed() == 1
    assert RunResult.load(tmp_path, tasks=False).tasks == []
    assert RunResult.load(tmp_path, output=True).tasks[0].attempts.stdout == "out\n"
