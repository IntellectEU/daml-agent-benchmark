"""Every task names files that exist, on any machine that has the repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.tasklist_catalog import load_tasks


@pytest.mark.parametrize("task", load_tasks(), ids=lambda task: f"{task.repo}/{task.test_file}")
def test_task_files_exist(task) -> None:
    if not (locations.sources_root / task.repo).is_dir():
        pytest.skip("source repository not present on this machine")
    missing = [p for p in [task.test_path, *task.impl_paths] if not Path(p).is_file()]
    assert missing == [], f"files named by the tasklist do not exist: {missing}"
