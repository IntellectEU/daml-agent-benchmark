"""What a run's code snapshot includes.

- a loaded module under an extra code directory is included and marked as extra code
- a loaded module outside the package and the extra code is left out
- the built-in repository handlers are included even when nothing imported them yet
- the package's docker/ files and the container wrapper are included, and every path is relative
- every task list's files are included, the extra code's under the extra code
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from daml_agent_benchmark.code_snapshot import stage_code_snapshot_for_run
from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.repos import registry


def _write_module(parent: Path, package: str, module: str) -> None:
    package_dir = parent / package
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / f"{module}.py").write_text("VALUE = 1\n", encoding="utf-8")


def _snapshot_files(run_dir: Path) -> dict[str, str]:
    stage_code_snapshot_for_run(run_dir)
    manifest = json.loads((run_dir / "artifacts" / "code_snapshot" / "manifest.json").read_text(encoding="utf-8"))
    return {entry["path"]: entry["root"] for entry in manifest["files"]}


def test_loaded_extra_code_module_is_included_and_outside_module_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra_parent = tmp_path / "extra_src"
    outside_parent = tmp_path / "outside_src"
    _write_module(extra_parent, "snapshot_test_extra", "handlers")
    _write_module(outside_parent, "snapshot_test_outside", "other")
    monkeypatch.syspath_prepend(str(extra_parent))
    monkeypatch.syspath_prepend(str(outside_parent))
    monkeypatch.setattr(locations, "extra_code_dirs", (extra_parent / "snapshot_test_extra",))
    for name in ("snapshot_test_extra", "snapshot_test_extra.handlers", "snapshot_test_outside", "snapshot_test_outside.other"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    __import__("snapshot_test_extra.handlers")
    __import__("snapshot_test_outside.other")

    files = _snapshot_files(tmp_path / "run")

    assert files["snapshot_test_extra/handlers.py"] == "extra"
    assert not any(path.startswith("snapshot_test_outside/") for path in files)
    assert (tmp_path / "run" / "artifacts" / "code_snapshot" / "files" / "snapshot_test_extra" / "handlers.py").is_file()


def test_builtin_handlers_are_included_without_being_imported_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "_HANDLERS", {})
    monkeypatch.setattr(registry, "_builtins_loaded", False)
    for module in registry._BUILTIN_HANDLER_MODULES:
        monkeypatch.delitem(sys.modules, f"daml_agent_benchmark.repos.{module}", raising=False)

    files = _snapshot_files(tmp_path / "run")

    for module in registry._BUILTIN_HANDLER_MODULES:
        assert files[f"daml_agent_benchmark/repos/{module}.py"] == "package"


def test_unimported_package_files_are_included_with_relative_paths(tmp_path: Path) -> None:
    files = _snapshot_files(tmp_path / "run")

    assert "daml_agent_benchmark/docker/codex_diff_wrapper.py" in files
    assert "daml_agent_benchmark/docker/sdk_store_bootstrap/bootstrap_sdk_store.sh" in files
    assert "daml_agent_benchmark/codex_in_container.py" in files
    assert not any(Path(path).is_absolute() or "__pycache__" in path for path in files)


def test_task_lists_are_included(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = tmp_path / "snapshot_test_tasks_extra"
    (extra / "tasklist").mkdir(parents=True)
    (extra / "tasklist" / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    (extra / "tasklist" / "__pycache__").mkdir()
    (extra / "tasklist" / "__pycache__" / "handlers.cpython-313.pyc").write_bytes(b"")
    monkeypatch.setattr(locations, "extra_code_dirs", (extra,))
    monkeypatch.setattr(locations, "tasklist_dirs", (*locations.tasklist_dirs, extra / "tasklist"))

    files = _snapshot_files(tmp_path / "run")

    assert files["daml_agent_benchmark/tasklist/tasks.yaml"] == "package"
    assert files["daml_agent_benchmark/tasklist/repos.yaml"] == "package"
    assert files["snapshot_test_tasks_extra/tasklist/tasks.yaml"] == "extra"
    assert not any("__pycache__" in path for path in files)
