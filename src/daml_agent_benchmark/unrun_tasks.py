"""Task records for tasks that never ran: queued, or failed before the harness could start them."""

from __future__ import annotations

import traceback
from dataclasses import replace
from pathlib import Path

from daml_agent_benchmark.records import (
    Finding,
    LiveState,
    TaskFlag,
    TaskResult,
    now_utc,
    repo_relative,
    write_task_live_result,
)
from daml_agent_benchmark.run_files import task_safe_name
from daml_agent_benchmark.tasklist_catalog import repo_relative_id


def queued_task_result(test_file_path: str, impl_files: list[str]) -> TaskResult:
    """The record of a task the runner has accepted but not started."""
    return TaskResult(
        task_id=repo_relative_id(test_file_path),
        task_safe_name=task_safe_name(test_file_path),
        test_file=repo_relative(test_file_path),
        impl_files=[repo_relative(f) for f in impl_files],
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
        impl_file_snapshots=[],
        attempts=None,
    )


def exception_task_result(
    run_dir: Path,
    *,
    task_index: int,
    total_tasks: int,
    test_file: str,
    impl_files: list[str],
    exception: Exception,
) -> TaskResult:
    """The record of a task whose run raised: an infra finding carrying the traceback, and a
    finished live snapshot so the dashboard does not show it running forever."""
    print(f"[{task_index}/{total_tasks}] exception {repo_relative_id(test_file)}: {exception}", flush=True)
    detail = f"Unhandled evaluator exception: {type(exception).__name__}: {exception}\n{traceback.format_exc()}"
    result = replace(
        queued_task_result(test_file, impl_files),
        started_at_utc=now_utc(),
        findings=[Finding(flag=TaskFlag.INFRA_FAILURE, detail=detail)],
    ).completed()
    write_task_live_result(run_dir, result)
    return result
