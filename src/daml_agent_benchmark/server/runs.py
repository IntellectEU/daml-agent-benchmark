"""What the dashboard shows about benchmark runs.

Runs live under `locations.logs_dir`, archived ones under its `z_archive` directory. A run
is read through `daml_agent_benchmark.records`, so this file names fields rather than
walking dictionaries. The matrix over all runs is cached per run directory, keyed by
the files it was built from.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from daml_agent_benchmark.pricing import (
    TOKEN_USAGE_EVENT_TYPES,
    RequestTokenUsage,
    attempt_events,
    request_token_usages,
    requests_cost,
)
from daml_agent_benchmark.server.event_costs import event_costs
from daml_agent_benchmark.records import (
    LiveState,
    Grade,
    RunResult,
    Severity,
    TaskFlag,
    TaskResult,
    TokenUsage,
    read_task_result,
    resolve_repo_relative,
    task_file_name,
    task_live_events_path,
)
from daml_agent_benchmark.locations import locations

ARCHIVE_DIR_NAME = "z_archive"
MATRIX_CACHE_FILENAME = "matrix_overview.json"
MATRIX_CACHE_SCHEMA_VERSION = 10
# A task whose live snapshot has not moved for this long is not running any more.
LIVE_STALE_SECONDS = int(os.getenv("AGENT_LIVE_STALE_SECONDS", "1800"))

_PARSER_ERROR_RE = re.compile(r"source:\s*parser", re.IGNORECASE)


# --- Finding the runs on disk ---------------------------------------------------------


def logs_dir() -> Path:
    """The directory the runs are written to, read from `locations` at call time."""
    return locations.logs_dir


def archive_dir() -> Path:
    """The directory archived runs are moved to."""
    return logs_dir() / ARCHIVE_DIR_NAME


def ensure_dirs() -> None:
    """Make sure the run directories exist, so a fresh checkout can be browsed."""
    logs_dir().mkdir(parents=True, exist_ok=True)
    archive_dir().mkdir(parents=True, exist_ok=True)


def _has_data(run_dir: Path) -> bool:
    """Whether a directory holds a run at all, rather than being left over from one."""
    return (run_dir / "run.json").exists() or any(
        any((run_dir / name).glob("*.json")) for name in ("tasks", "tasks_live")
    )


def _created_at(run_dir: Path) -> float:
    """When the run started, for ordering, from its record.

    A directory without a run record sorts last. The matrix skips it.
    """
    record = run_dir / "run.json"
    if not record.exists():
        return 0.0
    return datetime.fromisoformat(json.loads(record.read_text(encoding="utf-8"))["created_at_utc"]).timestamp()


@dataclass(frozen=True)
class RunDir:
    """A run on disk: its id, where it is, and whether it has been archived."""

    run_id: str
    path: Path
    archived: bool


def iter_run_dirs(*, include_archived: bool) -> list[RunDir]:
    """Every run on disk, newest first, by when it started rather than when it was written."""
    ensure_dirs()
    found = [
        RunDir(child.name, child, False)
        for child in logs_dir().iterdir()
        if child.is_dir() and child.name != ARCHIVE_DIR_NAME and _has_data(child)
    ]
    if include_archived:
        found += [RunDir(c.name, c, True) for c in archive_dir().iterdir() if c.is_dir() and _has_data(c)]
    return sorted(found, key=lambda run: _created_at(run.path), reverse=True)


def find_run_dir(run_id: str) -> RunDir | None:
    """One run by id, wherever it lives; None when no directory has that id."""
    ensure_dirs()
    for archived, base in ((False, logs_dir()), (True, archive_dir())):
        candidate = base / run_id
        if candidate.is_dir():
            return RunDir(run_id, candidate, archived)
    return None


# --- What the run was configured with -------------------------------------------------


@dataclass(frozen=True)
class RunConfig:
    """The bits of a run's config the dashboard reads."""

    billed_by_api_key: bool
    stale_seconds: int
    model: str  # the model the run's agent used, for pricing a task still running

    @classmethod
    def load(cls, run_dir: Path) -> RunConfig:
        """What the page needs from the run's config, which the runner writes at the start of every run.

        A run that authenticated with a subscription had no tokens billed, so its
        recorded cost is notional and the page withholds it.
        """
        config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
        timeout = config["max_task_runtime_seconds"]
        # A task is stale once it has been silent for about twice its own timeout.
        stale = int(max(180, min(3600, timeout * 2))) if timeout else LIVE_STALE_SECONDS
        return cls(
            billed_by_api_key=config["codex_auth_mode"] != "chatgpt",
            stale_seconds=stale,
            model=config["codex_model"],
        )


# --- One task, as a cell of the matrix -------------------------------------------------

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_USAGE_LIMIT = "usage_limit"
STATUS_TESTS_FAILED = "tests_failed"
STATUS_BUILD_FAILED = "build_failed"
STATUS_PARSE_FAILED = "parse_failed"
STATUS_INFRA_FAILED = "infra_failed"
STATUS_SECURITY_VIOLATION = "security_violation"
STATUS_OTHER_ERROR = "other_error"


def _is_stale(task: TaskResult, *, stale_seconds: int) -> bool:
    """A task whose live snapshot stopped moving without the attempt ever ending."""
    if task.finished_at_utc is not None or task.attempts is not None:
        return False
    updated = task.live_updated_at_utc or task.started_at_utc
    if updated is None:
        return False
    return (datetime.now(timezone.utc) - updated).total_seconds() >= stale_seconds


def _task_status(task: TaskResult, *, stale_seconds: int) -> str:
    """The one word the matrix shows for a task."""
    if task.live_state is LiveState.QUEUED:
        # Waiting for a worker. Its snapshot is written once and never moves, so the
        # staleness rule below would read it as an error.
        return STATUS_QUEUED
    if task.finished_at_utc is None and task.attempts is None:
        return STATUS_OTHER_ERROR if _is_stale(task, stale_seconds=stale_seconds) else STATUS_RUNNING
    if any(f.flag.severity is Severity.SECURITY for f in task.findings):
        return STATUS_SECURITY_VIOLATION
    if task.attempts is not None and task.attempts.quota_limit_detected:
        return STATUS_USAGE_LIMIT
    if any(f.flag.severity is Severity.INFRA for f in task.findings):
        return STATUS_INFRA_FAILED
    grade = task.grade
    if grade is None:
        return STATUS_OTHER_ERROR
    if grade.tests_passed:
        return STATUS_SUCCESS
    if grade.compile_passed:
        return STATUS_TESTS_FAILED
    if not grade.syntax_passed and _PARSER_ERROR_RE.search(grade.syntax_error or ""):
        return STATUS_PARSE_FAILED
    return STATUS_BUILD_FAILED


def _scripts_in_test_file(grade: Grade, test_file: str) -> int:
    """How many of a grade's scripts belong to the task's own test file.

    A run reports every script in every module it compiled, so a grade can name scripts
    that live in an implementation file the agent rewrote. Only the test file's own count
    says how much of the task was exercised.
    """
    name = Path(test_file).name
    return sum(1 for key in grade.test_results if Path(key.split(":")[0]).name == name)


_LINE_NO_RE = re.compile(rb'^\{"line_no": (\d+),')
_USAGE_MARKERS = tuple(f'"{event_type}"'.encode() for event_type in TOKEN_USAGE_EVENT_TYPES)


def _request_token_usages(path: Path) -> list[RequestTokenUsage]:
    """The requests a running task's live event stream reports.

    Only the lines that mention a usage event are parsed in full. Every other line gives
    just its line number, which is enough to find where a restarted stream begins. The
    streams run to megabytes per task, so this keeps the matrix fast. A last line
    without its newline is still being written and is left for the next read.
    """
    records: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                break
            match = _LINE_NO_RE.match(line)
            if match is not None and not any(marker in line for marker in _USAGE_MARKERS):
                records.append({"line_no": int(match.group(1)), "event": {}})
            elif line.strip():
                records.append(json.loads(line))
    return request_token_usages(attempt_events(records))


def _task_cell(task: TaskResult, *, config: RunConfig, run_dir: Path) -> dict[str, Any]:
    """One task as the matrix shows it: a status, its test tally and what it cost.

    A finished task shows the cost the runner recorded from its events, in total and per
    kind of token. A task still running is priced the same way from the events it has
    streamed so far, and marked as a lower bound.
    """
    grade = task.grade
    attempt = task.attempts
    tests_succeeded = grade.tests_passed_count() if grade else 0
    tests_total = grade.tests_total() if grade else 0
    control = task.ground_truth_control
    if not tests_total and control is not None:
        # Nothing ran, so the grade has no denominator: a build that failed reports no
        # scripts at all. The control ran the same file with its implementation intact, so
        # its scripts in that file say how much the task would have exercised.
        tests_total = _scripts_in_test_file(control.grade, task.test_file)

    status = _task_status(task, stale_seconds=config.stale_seconds)
    usage = attempt.usage if attempt else None
    costs: dict[str, float | None] = {"input_cost": None, "cached_input_cost": None, "output_cost": None}
    usd_cost = None
    lower_bound = bool(attempt and not attempt.usage_complete)
    if attempt is not None and config.billed_by_api_key:
        usd_cost = attempt.usd_cost
        costs = {
            "input_cost": attempt.input_usd,
            "cached_input_cost": attempt.cached_input_usd,
            "output_cost": attempt.output_usd,
        }
    elif status == STATUS_RUNNING and config.billed_by_api_key:
        live_path = task_live_events_path(run_dir, task.task_safe_name)
        requests = _request_token_usages(live_path) if live_path.exists() else []
        if requests:
            usage = sum(
                (TokenUsage(r.input_tokens, r.output_tokens, r.cached_input_tokens) for r in requests), TokenUsage.ZERO
            )
            cost = requests_cost(config.model, requests)
            if cost is not None:
                usd_cost = cost.total
                costs = {"input_cost": cost.input, "cached_input_cost": cost.cached_input, "output_cost": cost.output}
            lower_bound = True
    return {
        "task_id": task.task_id,
        "status_code": status,
        "timed_out": bool(attempt and attempt.timed_out),
        "tests_succeeded": tests_succeeded,
        "tests_total": tests_total,
        "usd_cost": usd_cost,
        "usd_cost_is_lower_bound": lower_bound,
        "input_tokens": usage.input_tokens if usage else None,
        "cached_input_tokens": usage.cached_input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        **costs,
    }


# --- One run, as a row of the matrix ---------------------------------------------------


def run_summary(run: RunResult, *, config: RunConfig) -> dict[str, Any]:
    """The run's own numbers, with the cost withheld when nothing was billed for them."""
    summary = run.summary()
    if not config.billed_by_api_key:
        # A subscription run's tokens are not billed, so a cost would be made up.
        summary["total_usd_cost"] = None
    return summary


def _summary_with_cell_costs(summary: dict[str, Any], cells: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The run's cost as the sum of what its cells show.

    The cells include what the tasks still running have cost so far, which the recorded
    total does not. Only tasks that used tokens count. The sum is a lower bound while any
    task is running, or when some tasks have no price. It stays None when none has one.
    """
    costs = [cell["usd_cost"] for cell in cells.values() if cell["input_tokens"] is not None]
    known = [cost for cost in costs if cost is not None]
    if known:
        summary["total_usd_cost"] = sum(known)
        if len(known) < len(costs) or any(cell["usd_cost_is_lower_bound"] for cell in cells.values()):
            summary["usage_complete"] = False
    return summary


def _matrix_item(run_dir: RunDir, run: RunResult, *, config: RunConfig) -> dict[str, Any]:
    """One run as a column of the matrix: its summary, a cell per task, and the tallies."""
    cells = {task.task_id: _task_cell(task, config=config, run_dir=run_dir.path) for task in run.tasks}
    counts: dict[str, int] = {}
    for cell in cells.values():
        counts[cell["status_code"]] = counts.get(cell["status_code"], 0) + 1
    return {
        "run_id": run_dir.run_id,
        "archived": run_dir.archived,
        "summary": _summary_with_cell_costs(run_summary(run, config=config), cells),
        "task_statuses": cells,
        "status_counts": counts,
    }


def load_run(run_dir: Path, *, tasks: bool = True) -> RunResult | None:
    """The run, or None when its directory holds no run record.

    A directory with task files but no run record was written by a benchmark that recorded
    runs differently. It is named on stdout rather than passed over in silence.
    """
    if not (run_dir / "run.json").exists():
        print(f"[agent] {run_dir.name}: no run.json; skipping", flush=True)
        return None
    return RunResult.load(run_dir, tasks=tasks)


# --- The matrix cache ------------------------------------------------------------------


def _source_signature(run_dir: Path) -> dict[str, Any]:
    """What the matrix row was built from: each record file's size and modification time."""
    files = [run_dir / "run.json", run_dir / "config.json"]
    for name, pattern in (("tasks", "*.json"), ("tasks_live", "*.json"), ("tasks_live", "*.stdout.jsonl")):
        files.extend(sorted((run_dir / name).glob(pattern)))
    entries = [(str(p.relative_to(run_dir)), p.stat()) for p in files if p.exists()]
    return {
        "count": len(entries),
        "total_size": sum(stat.st_size for _, stat in entries),
        "max_mtime_ns": max((stat.st_mtime_ns for _, stat in entries), default=0),
    }


def matrix_row(run_dir: RunDir) -> dict[str, Any] | None:
    """One run's matrix row, from the cache next to the run when it is still current.

    The signature covers the record files, not this code, so a change to what a row
    contains has to raise `MATRIX_CACHE_SCHEMA_VERSION`. Otherwise every run keeps serving
    the row it was built with.
    """
    cache_path = run_dir.path / MATRIX_CACHE_FILENAME
    signature = _source_signature(run_dir.path)
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached["schema_version"] == MATRIX_CACHE_SCHEMA_VERSION and cached["source_signature"] == signature:
            cached["item"]["archived"] = run_dir.archived
            return cached

    run = load_run(run_dir.path)
    if run is None:
        return None
    payload = {
        "schema_version": MATRIX_CACHE_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_signature": signature,
        "task_ids": sorted(task.task_id for task in run.tasks),
        "item": _matrix_item(run_dir, run, config=RunConfig.load(run_dir.path)),
    }
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(cache_path)
    return payload


# --- One task in full ------------------------------------------------------------------


def _find_task(run_dir: Path, task_id: str) -> tuple[Path, TaskResult] | None:
    """A task's record, final if it has one, else its live snapshot."""
    safe_name = task_file_name(task_id)
    for folder in ("tasks", "tasks_live"):
        path = run_dir / folder / f"{safe_name}.json"
        if path.exists():
            return path, read_task_result(path, output=folder == "tasks")
    return None


def _live_events(run_dir: Path, task: TaskResult) -> list[dict[str, Any]]:
    """The events a running task has produced so far, from the file the driver streams into.

    A task that has finished carries them on its attempt instead.
    """
    path = task_live_events_path(run_dir, task.task_safe_name)
    if task.attempts is not None or not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _impl_file_views(task: TaskResult) -> list[dict[str, Any]]:
    """Each implementation file before and after the agent wrote it.

    The snapshot holds both. When its original text is missing, the file on this machine
    stands in, since that is what the copy was made from, and the view says which of the
    two it is showing.
    """
    views = []
    for snapshot in task.impl_file_snapshots:
        original = snapshot.original
        source = "snapshot"
        if original is None:
            path = resolve_repo_relative(snapshot.impl_file)
            original = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
            source = "current_source" if original is not None else "missing"
        views.append(
            {
                "impl_file": snapshot.impl_file,
                "path_in_copy": snapshot.path_in_copy,
                "original_text": original,
                "generated_text": snapshot.generated,
                "original_source": source,
                "generated_source": "snapshot" if snapshot.generated is not None else "missing",
            }
        )
    return views


def _terminal_log(run_dir: Path, task: TaskResult, *, max_chars: int = 120_000) -> dict[str, Any]:
    """What the run printed for this task, middle-truncated when it is long."""
    path = run_dir / "full_terminal_output_split_per_task" / f"{task.task_safe_name}.log"
    if not path.exists():
        return {"path": None, "content": None, "truncated": False, "total_chars": 0}
    raw = path.read_text(encoding="utf-8", errors="replace")
    if len(raw) <= max_chars:
        return {"path": str(path), "content": raw, "truncated": False, "total_chars": len(raw)}
    half = max_chars // 2
    content = f"{raw[:half]}\n\n... [truncated {len(raw) - max_chars} chars] ...\n\n{raw[-half:]}"
    return {"path": str(path), "content": content, "truncated": True, "total_chars": len(raw)}


def _event_costs(run_dir: Path, task: TaskResult, live_events: list[dict[str, Any]]) -> dict[str, Any]:
    """What each event of the task cost, split from its requests.

    A finished task is priced for the model its attempt used, a running one for the model
    its run is configured with. A run billed to a subscription gets token figures only, as
    in the matrix.
    """
    config = RunConfig.load(run_dir)
    attempt = task.attempts
    events = (attempt.stdout_events or []) if attempt is not None else live_events
    model = attempt.model if attempt is not None else config.model
    return event_costs(events, model if config.billed_by_api_key else None).to_record()


def task_detail(run_dir: RunDir, task_id: str) -> dict[str, Any] | None:
    """Everything the task view shows, including the sidecars; None when there is no such task.

    The whole record goes out as written, so the page can show a field this module does not
    know about yet. The task's matrix cell goes with it, so the page shows the same status
    as the matrix while it polls.
    """
    found = _find_task(run_dir.path, task_id)
    if found is None:
        return None
    path, task = found
    live_events = _live_events(run_dir.path, task)
    return {
        "run_id": run_dir.run_id,
        "task_id": task.task_id,
        "task_file_path": str(path),
        "is_live": path.parent.name == "tasks_live",
        "task": task.to_record(),
        "cell": _task_cell(task, config=RunConfig.load(run_dir.path), run_dir=run_dir.path),
        "live_events": live_events,
        "event_costs": _event_costs(run_dir.path, task, live_events),
        "impl_file_views": _impl_file_views(task),
        "terminal_log": _terminal_log(run_dir.path, task),
    }


def flag_labels() -> dict[str, str]:
    """Every finding flag with the severity it carries.

    The page hardcodes a colour for each of the three severities, not for each flag, and
    looks a flag's severity up in this map. A flag added to `TaskFlag` later therefore
    shows in the right colour without any change to the frontend.
    """
    return {flag.value: flag.severity.value for flag in TaskFlag}
