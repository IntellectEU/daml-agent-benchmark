"""`--tasklist-dir` adds a task-list directory, with its handlers, after the package's own."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from daml_agent_benchmark import fetch_sources, runner, tasklist_catalog
from daml_agent_benchmark.locations import PACKAGE_TASKLIST_DIR, configure, locations
from daml_agent_benchmark.repos import registry
from daml_agent_benchmark.repos.registry import handler_for
from daml_agent_benchmark.tasklist_catalog import add_tasklist_dirs, load_repos, load_tasks, package_task_ids

REPOS_YAML = """\
acme-settlement:
  url: null
  commit: null
  license: null
  build_tool: daml
"""

TASKS_YAML = """\
- repo: acme-settlement
  test: daml/Test/Settlement.daml
  impl:
    - daml/Acme/Settlement.daml
"""

HANDLERS_PY = """\
from daml_agent_benchmark.repos.registry import RepoHandler, register

register(RepoHandler(name="acme-settlement", excluded_subpaths=("dist/dars",)))
"""


@pytest.fixture(autouse=True)
def restore_state():
    """Put the task-list directories, the catalog caches, the handlers and the loaded modules back."""
    tasklist_dirs, extra_code_dirs = locations.tasklist_dirs, locations.extra_code_dirs
    handlers = dict(registry._HANDLERS)
    modules = set(sys.modules)
    yield
    configure(tasklist_dirs=tasklist_dirs, extra_code_dirs=extra_code_dirs)
    registry._HANDLERS.clear()
    registry._HANDLERS.update(handlers)
    for name in set(sys.modules) - modules:
        del sys.modules[name]


@pytest.fixture
def tasklist(tmp_path: Path) -> Path:
    d = tmp_path / "tasklist"
    d.mkdir()
    (d / "repos.yaml").write_text(REPOS_YAML, encoding="utf-8")
    (d / "tasks.yaml").write_text(TASKS_YAML, encoding="utf-8")
    (d / "handlers.py").write_text(HANDLERS_PY, encoding="utf-8")
    return d


def test_the_directory_adds_its_tasks_repositories_and_handlers(tasklist: Path) -> None:
    package_tasks = len(load_tasks())
    add_tasklist_dirs([tasklist])

    assert locations.tasklist_dirs == (PACKAGE_TASKLIST_DIR, tasklist.resolve())
    assert "acme-settlement" in load_repos()
    assert len(load_tasks()) == package_tasks + 1
    assert any(t.repo == "acme-settlement" and t.test_file == "daml/Test/Settlement.daml" for t in load_tasks())
    assert handler_for("acme-settlement").excluded_subpaths == ("dist/dars",)
    assert "acme-settlement/daml/Test/Settlement.daml" not in package_task_ids()


def test_two_directories_each_load_their_own_handlers(tmp_path: Path) -> None:
    dirs = []
    for name in ("one", "two"):
        d = tmp_path / name
        d.mkdir()
        (d / "repos.yaml").write_text(REPOS_YAML.replace("acme-settlement", f"acme-{name}"), encoding="utf-8")
        (d / "handlers.py").write_text(HANDLERS_PY.replace("acme-settlement", f"acme-{name}"), encoding="utf-8")
        dirs.append(d)
    add_tasklist_dirs(dirs)
    assert handler_for("acme-one").excluded_subpaths == ("dist/dars",)
    assert handler_for("acme-two").excluded_subpaths == ("dist/dars",)


def test_a_directory_without_task_lists_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=str(tmp_path)):
        add_tasklist_dirs([tmp_path])
    assert locations.tasklist_dirs == (PACKAGE_TASKLIST_DIR,)


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="does not exist"):
        add_tasklist_dirs([tmp_path / "missing"])


def test_a_failing_handlers_module_raises(tasklist: Path) -> None:
    (tasklist / "handlers.py").write_text("raise RuntimeError('broken handler')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="broken handler"):
        add_tasklist_dirs([tasklist])


def _record_calls(monkeypatch) -> list[list[Path]]:
    """Record the directories each command passes on, and add them as the real call would."""
    calls: list[list[Path]] = []

    def record(paths: list[Path]) -> None:
        calls.append(paths)
        tasklist_catalog.add_tasklist_dirs(paths)

    for module in (runner, fetch_sources):
        monkeypatch.setattr(module, "add_tasklist_dirs", record)
    return calls


def test_the_runner_takes_the_option_before_it_loads_the_experiments(tasklist: Path, tmp_path: Path, monkeypatch) -> None:
    calls = _record_calls(monkeypatch)
    config = tmp_path / "experiment.py"
    config.write_text("CURRENT_EXP = None\n", encoding="utf-8")
    seen: list[bool] = []
    monkeypatch.setattr(runner, "_experiments_from_module", lambda module: [])
    monkeypatch.setattr(runner, "run_experiments", lambda experiments, index: seen.append("acme-settlement" in load_repos()))
    runner.main(["--config", str(config), "--tasklist-dir", str(tasklist)])
    assert calls == [[tasklist]]
    assert seen == [True]


def test_the_fetcher_takes_the_repeated_option(tmp_path: Path, monkeypatch) -> None:
    calls = _record_calls(monkeypatch)
    dirs = []
    for name in ("one", "two"):
        d = tmp_path / name
        d.mkdir()
        (d / "repos.yaml").write_text(REPOS_YAML.replace("acme-settlement", f"acme-{name}"), encoding="utf-8")
        dirs.append(d)
    fetched: list[str] = []
    monkeypatch.setattr(fetch_sources, "fetch_all", lambda selected, dest: fetched.extend(selected) or {"ready": [], "skipped": [], "failed": []})
    args = ["--sources-dir", str(tmp_path / "sources"), "--tasklist-dir", str(dirs[0]), "--tasklist-dir", str(dirs[1])]
    assert fetch_sources.main(args) == 0
    assert calls == [dirs]
    # Repositories without a URL are not fetched, so only the package's are passed on.
    assert "acme-one" not in fetched and fetched

