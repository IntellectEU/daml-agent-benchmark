"""The red-team result file: its summary over the rows, and its round trip."""

from dataclasses import replace

from daml_agent_benchmark.records import FileChange, TokenUsage
from daml_agent_benchmark.redteam import CanaryMode, RedTeamFailure, RedTeamReport, RedTeamResult, RedTeamSummary
from daml_agent_benchmark.config import TaskKindName
from daml_agent_benchmark.workspace_audit import ChangeKind

CLEAN = RedTeamResult(
    task="ex-models/shop/daml/Test.daml",
    task_kind=TaskKindName.IMPLEMENTATION,
    repository="ex-models",
    seconds=41.5,
    timed_out=False,
    canary_path=None,
    canary_found=False,
    found=False,
    locations=[],
    notes="nothing reachable holds the implementation",
    n_commands=9,
    usage=TokenUsage(input_tokens=1000, output_tokens=200, cached_input_tokens=600),
    usd_cost=0.0125,
    out_of_workspace_writes=[],
    egress_non_allowed=[],
)
SEEDED = replace(
    CLEAN,
    task="ex-models/chess/daml/Tests/Chess.daml",
    canary_path=".build-cache/prior-build-notes.txt",
    canary_found=True,
    found=True,
    locations=["/workspace/.build-cache/prior-build-notes.txt — the implementation"],
)
FAILED = RedTeamFailure(task="canton/community/common/src/main/daml/CantonExamples/Paint.daml", error="docker: no space left")


def _summary(tasks: list[RedTeamResult], failed: list[RedTeamFailure]) -> RedTeamSummary:
    return RedTeamSummary.of("gpt-6-luna", "https://api.openai.com/v1", CanaryMode.ALTERNATE, False, tasks, failed)


def test_summary_counts_seeded_clean_and_failed_tasks():
    tasks = [
        SEEDED,
        replace(SEEDED, canary_found=False, found=False, locations=[], usd_cost=None),  # missed the canary
        CLEAN,
        replace(CLEAN, found=True, locations=["/opt/daml/docs — generated API docs"], timed_out=True, usage=None),
    ]
    summary = _summary(tasks, [FAILED])

    assert summary == RedTeamSummary(
        model="gpt-6-luna",
        provider_base_url="https://api.openai.com/v1",
        canary_mode=CanaryMode.ALTERNATE,
        answer_shown=False,
        n_tasks=5,
        n_errored=1,
        n_timed_out=1,
        seeded_n=2,
        seeded_canary_found=1,
        clean_n=2,
        clean_reported_leak=1,
        total_usd_cost=0.0375,
    )


def test_summary_cost_is_unknown_when_no_task_cost_is():
    assert _summary([replace(CLEAN, usd_cost=None)], [FAILED]).total_usd_cost is None


def test_report_round_trips_through_its_record(tmp_path):
    tasks = [
        SEEDED,
        replace(
            CLEAN,
            task_kind=TaskKindName.TEST_GENERATION,
            notes=None,
            out_of_workspace_writes=[FileChange(path="/tmp/escape.txt", kind=ChangeKind.ADDED)],
        ),
    ]
    report = RedTeamReport(summary=_summary(tasks, [FAILED]), tasks=tasks, failed=[FAILED])

    assert RedTeamReport.from_record(report.to_record()) == report
    path = tmp_path / "sweep.json"
    report.write(path)
    assert RedTeamReport.load(path) == report
