"""Where a run writes, and what it names things.

One directory per run under the logs root, one file per task inside it, plus the live
snapshots the dashboard reads while a run is still going. Writes go through a temporary
file and a rename, so a reader never sees half a record.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from daml_agent_benchmark.config import ExperimentConfig
from daml_agent_benchmark.records import AnswerFileSnapshot, now_utc, repo_relative, task_live_events_path
from daml_agent_benchmark.run_identity import build_uuid_run_identity, default_agent_run_name
from daml_agent_benchmark.locations import locations, task_file_name
from daml_agent_benchmark.tasklist_catalog import repo_relative_id


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_compact() -> str:
    return datetime.now().strftime("%y%m%d-%H%M%S")


def default_run_name(config: ExperimentConfig) -> str:
    return default_agent_run_name(config.codex_model, task_selection_label(config))


def build_run_identity(config: ExperimentConfig) -> tuple[str, str]:
    return build_uuid_run_identity(config.run_name, default_run_name(config))


def task_safe_name(test_file_path: str | Path) -> str:
    """The task id as a flat string safe for use as a filename or dict key."""
    return task_file_name(repo_relative_id(test_file_path))


def write_json_atomic(path: Path, payload: dict | list) -> None:
    """Write JSON via a temp file + rename so readers never see a half-written file.
    Used for the ground-truth control cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)


def load_json_dict(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def safe_read_text(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return None


def read_answer_original_snapshots(answer_files: list[str]) -> dict[str, str | None]:
    """The answer files as the source repository has them, read before the task blanks them.

    This is the ground truth half of `build_answer_file_snapshots`, taken while it is still
    on disk. A file that cannot be read is recorded as None rather than as empty.
    """
    snapshots: dict[str, str | None] = {}
    for answer_file in answer_files:
        snapshots[str(answer_file)] = safe_read_text(answer_file)
    return snapshots


def build_answer_file_snapshots(
    answer_files: list[str],
    repo_copy_answer_files: list[str],
    repo_copy_root: str,
    original_snapshots: dict[str, str | None],
) -> list[AnswerFileSnapshot]:
    """Each answer file as it was before the task blanked it, next to what the agent wrote.

    The repository copy is deleted when the run ends, so this is the only place the agent's
    work survives. Without it a run keeps a grade and nothing to read behind it, and no way
    to see why a task failed.

    `original` is read from the host repository before the task blanks the file, because by
    the time this runs the copy holds only what the agent left there.
    """
    captured_at_utc = now_utc()
    root = Path(repo_copy_root).resolve()
    snapshots: list[AnswerFileSnapshot] = []
    for answer_file, copy_path in zip(answer_files, repo_copy_answer_files, strict=True):
        snapshots.append(
            AnswerFileSnapshot(
                answer_file=repo_relative(answer_file),
                path_in_copy=Path(copy_path).resolve().relative_to(root).as_posix(),
                original=original_snapshots.get(str(answer_file)),
                generated=safe_read_text(copy_path),
                captured_at_utc=captured_at_utc,
            )
        )
    return snapshots


def task_live_stdout_events_path(run_dir: Path, test_file_path: str | Path) -> Path:
    """Where a task's events are streamed as they arrive, for a task named by its test file."""
    return task_live_events_path(run_dir, task_safe_name(test_file_path))


def make_run_dir(run_id: str) -> Path:
    run_dir = locations.logs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def make_run_repo_copies_dir() -> Path:
    repo_copy_root = locations.repo_copies_dir
    repo_copy_root.mkdir(parents=True, exist_ok=True)
    run_repo_copies_dir = repo_copy_root / _timestamp_compact()
    run_repo_copies_dir.mkdir(parents=True, exist_ok=True)
    return run_repo_copies_dir


def remove_run_repo_copies_dir_if_empty(run_repo_copies_dir: Path) -> None:
    try:
        run_repo_copies_dir.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        # Keep non-empty repository copies for post-mortem debugging.
        return


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def task_selection_label(config: ExperimentConfig) -> str:
    tasks = config.tasks
    if isinstance(tasks, list) and tasks:
        return f"manual_{len(tasks)}"
    return config.task_set
