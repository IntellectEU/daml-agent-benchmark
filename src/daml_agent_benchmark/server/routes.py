"""The routes behind the run dashboard.

The dashboard shows every run as a row of a matrix with one cell per task, and shows one
task in full on request. Runs can be archived and deleted from it. The router mounts under
`/api/agent`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from daml_agent_benchmark.server import runs
from daml_agent_benchmark.tasklist_catalog import package_task_ids

router = APIRouter(prefix="/api/agent")


class BulkArchiveRequest(BaseModel):
    """Which runs to archive, and why."""

    run_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class BulkDeleteRequest(BaseModel):
    """Which runs to delete."""

    run_ids: list[str] = Field(default_factory=list)


def normalize_run_id(run_id: str) -> str:
    """The run id as a directory name. Refuses an empty id and one that leaves its directory."""
    value = str(run_id or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="run_id is required")
    if "/" in value or "\\" in value or ".." in value:
        raise HTTPException(status_code=400, detail=f"Invalid run_id: {run_id}")
    return value


def write_archive_meta(run_dir: Path, reason: str) -> None:
    """Record next to an archived run why and when it was archived."""
    payload = {"reason": reason.strip(), "archived_at": datetime.now(timezone.utc).isoformat()}
    (run_dir / "archive_meta.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def color_from_string(text: str) -> str:
    """A pastel colour that is always the same for the same text, as a hex triplet."""
    if not text:
        return "#d3d3d3"
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    r, g, b = (int(digest[i : i + 2], 16) for i in (0, 2, 4))
    return f"#{(r + 255) // 2:02x}{(g + 255) // 2:02x}{(b + 255) // 2:02x}"


def _repository_of(task_id: str) -> str:
    """The repository a task belongs to: the first segment of its id."""
    return task_id.split("/", 1)[0]


@router.get("/matrix")
def get_matrix(include_archived: bool = False) -> dict[str, Any]:
    """Every run as a row of cells, one per task, over the union of the runs' tasks.

    Tasks of the same repository share a header colour. `package_task_ids` names the tasks
    that ship with the package, so a page showing runs over added tasklists can also show
    what a user of the package alone would see.
    """
    task_ids: set[str] = set()
    items: list[dict[str, Any]] = []
    for run_dir in runs.iter_run_dirs(include_archived=include_archived):
        row = runs.matrix_row(run_dir)
        if row is None:
            continue
        task_ids.update(row["task_ids"])
        items.append(row["item"])

    sorted_task_ids = sorted(task_ids)
    return {
        "task_ids": sorted_task_ids,
        "task_header_colors": {task_id: color_from_string(_repository_of(task_id)) for task_id in sorted_task_ids},
        "task_flags": runs.flag_labels(),
        "package_task_ids": sorted(package_task_ids()),
        "items": items,
    }


@router.get("/runs/{run_id}/tasks/detail")
def get_task_detail(run_id: str, task_id: str) -> dict[str, Any]:
    """One task of one run in full, with its sidecars."""
    task_id = str(task_id or "").strip()
    if not task_id or "\\" in task_id or ".." in task_id:
        raise HTTPException(status_code=400, detail=f"Invalid task_id: {task_id!r}")
    run_dir = runs.find_run_dir(normalize_run_id(run_id))
    if run_dir is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    detail = runs.task_detail(run_dir, task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Task not found for run_id={run_id}: {task_id}")
    return detail


@router.post("/runs/archive")
def archive_runs(body: BulkArchiveRequest) -> dict[str, Any]:
    """Move runs into the archive directory. A run that is already there is skipped."""
    run_ids = [normalize_run_id(rid) for rid in body.run_ids if str(rid).strip()]
    if not run_ids:
        raise HTTPException(status_code=400, detail="No run_ids provided")

    runs.ensure_dirs()
    archived: list[str] = []
    skipped: list[str] = []
    for run_id in run_ids:
        active_dir = runs.logs_dir() / run_id
        if not active_dir.is_dir():
            if (runs.archive_dir() / run_id).exists():
                skipped.append(run_id)
                continue
            raise HTTPException(status_code=404, detail=f"Run not found in active logs: {run_id}")

        target = runs.archive_dir() / run_id
        idx = 1
        while target.exists():
            target = runs.archive_dir() / f"{run_id}__archived_{idx}"
            idx += 1

        shutil.move(str(active_dir), str(target))
        write_archive_meta(target, reason=body.reason)
        archived.append(target.name)

    return {"archived": archived, "skipped": skipped, "count": len(archived)}


@router.post("/runs/delete")
def delete_runs(body: BulkDeleteRequest) -> dict[str, Any]:
    """Delete runs, active or archived."""
    run_ids = [normalize_run_id(rid) for rid in body.run_ids if str(rid).strip()]
    if not run_ids:
        raise HTTPException(status_code=400, detail="No run_ids provided")

    deleted: list[str] = []
    missing: list[str] = []
    for run_id in run_ids:
        found = False
        for base in (runs.logs_dir(), runs.archive_dir()):
            run_dir = base / run_id
            if run_dir.is_dir():
                shutil.rmtree(run_dir)
                found = True
        (deleted if found else missing).append(run_id)

    return {"deleted": deleted, "missing": missing, "count": len(deleted)}
