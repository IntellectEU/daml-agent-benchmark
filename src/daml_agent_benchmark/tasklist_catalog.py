"""The benchmark's tasks and the repositories that they come from.

Both are declared in YAML files in a task-list directory: `tasks.yaml` and `repos.yaml`.
The package ships one such directory with the tasks from openly licensed repositories.
More directories come from the runner's and the fetcher's `--tasklist-dir` option, or from
`configure()`. A task names one test file and the implementation files the agent must
regenerate for it, relative to its repository. Repositories are checked out side by side
under the sources root.

A run selects a subset of the tasks, by id or by rule.

Paths leave this module as absolute strings, because the harness and its run records
work with absolute file paths throughout. The copy of a repository lives at
<copy dir>/<run>/<repo><marker><suffix>, and the path helpers here understand both
layouts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import sys
from dataclasses import dataclass
from functools import cache
from collections import Counter
from pathlib import Path

import yaml

from daml_agent_benchmark.config import ExperimentConfig
from daml_agent_benchmark.constants import REPO_COPY_DIR_SPLITTER
from daml_agent_benchmark.locations import PACKAGE_TASKLIST_DIR, configure, locations
from daml_agent_benchmark.repos.registry import handler_for
from daml_agent_benchmark.repos.sdk_version import resolve_daml_sdk_version


@dataclass(frozen=True)
class Repo:
    name: str
    url: str | None
    commit: str | None
    license: str | None
    build_tool: str

    @property
    def root(self) -> Path:
        return locations.sources_root / self.name


@dataclass(frozen=True)
class Task:
    repo: str
    test_file: str
    impl_files: tuple[str, ...]

    @property
    def test_path(self) -> str:
        return str(locations.sources_root / self.repo / self.test_file)

    @property
    def impl_paths(self) -> list[str]:
        return [str(locations.sources_root / self.repo / f) for f in self.impl_files]


def _tasklist_files(name: str) -> list[Path]:
    return [d / name for d in locations.tasklist_dirs if (d / name).exists()]


def clear_caches() -> None:
    """Forget loaded task lists, after the locations changed."""
    load_repos.cache_clear()
    load_tasks.cache_clear()
    impl_files_by_test_file.cache_clear()


def add_tasklist_dir_argument(parser: argparse.ArgumentParser) -> None:
    """Add the `--tasklist-dir` option, through which the runner and the fetcher take extra task-list directories."""
    parser.add_argument(
        "--tasklist-dir",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="a task-list directory to read after the package's own; repeat for several",
    )


def add_tasklist_dirs(paths: list[Path]) -> None:
    """Add task-list directories after the configured ones, and load each one's `handlers.py`.

    Each directory must hold a `repos.yaml`, a `tasks.yaml` or both. It also becomes an
    extra code directory, so a run's code snapshot includes its task lists and handlers.
    """
    dirs = [Path(path).expanduser().resolve() for path in paths]
    for d in dirs:
        if not d.is_dir():
            raise SystemExit(f"task-list directory {d} does not exist")
        if not (d / "repos.yaml").is_file() and not (d / "tasks.yaml").is_file():
            raise SystemExit(f"task-list directory {d} holds neither a repos.yaml nor a tasks.yaml")
        if d in {c.resolve() for c in locations.tasklist_dirs}:
            raise SystemExit(f"task-list directory {d} is already configured")
    if not dirs:
        return
    configure(
        tasklist_dirs=(*locations.tasklist_dirs, *dirs),
        extra_code_dirs=(*locations.extra_code_dirs, *dirs),
    )
    for d in dirs:
        handlers = d / "handlers.py"
        if handlers.is_file():
            _import_handlers(handlers)


def _import_handlers(path: Path) -> None:
    """Import a task-list directory's `handlers.py` under a module name unique to its directory."""
    digest = hashlib.sha256(str(path.parent).encode()).hexdigest()[:12]
    name = f"daml_agent_benchmark_tasklist_handlers_{digest}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise


@cache
def load_repos() -> dict[str, Repo]:
    repos: dict[str, Repo] = {}
    for path in _tasklist_files("repos.yaml"):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        for name, r in raw.items():
            if name in repos:
                raise KeyError(f"repository {name!r} is declared twice, in {path.parent} and earlier")
            repos[name] = Repo(name=name, url=r["url"], commit=r["commit"], license=r["license"], build_tool=r["build_tool"])
    return repos


@cache
def load_tasks() -> tuple[Task, ...]:
    repos = load_repos()
    tasks: list[Task] = []
    seen: set[tuple[str, str]] = set()
    for path in _tasklist_files("tasks.yaml"):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        for entry in raw:
            if entry["repo"] not in repos:
                raise KeyError(f"task {entry['test']} names repository {entry['repo']!r}, which no repos.yaml declares")
            key = (entry["repo"], entry["test"])
            if key in seen:
                raise KeyError(f"task {entry['repo']}/{entry['test']} is declared twice")
            seen.add(key)
            impl = entry["impl"]
            # A stray word after `impl:` turns the list below it into one string, and
            # iterating a string yields its characters, so this is checked here.
            if not isinstance(impl, list) or not all(isinstance(f, str) for f in impl):
                raise TypeError(f"task {entry['repo']}/{entry['test']}: `impl` must be a list of paths, got {impl!r}")
            tasks.append(
                Task(
                    repo=entry["repo"],
                    test_file=entry["test"],
                    impl_files=tuple(impl),
                )
            )
    return tuple(tasks)


@cache
def package_task_ids() -> frozenset[str]:
    """The ids of the tasks that ship with the package, whatever tasklists `configure()` adds."""
    with open(PACKAGE_TASKLIST_DIR / "tasks.yaml", encoding="utf-8") as f:
        return frozenset(f"{entry['repo']}/{entry['test']}" for entry in yaml.safe_load(f))


@cache
def impl_files_by_test_file() -> dict[str, list[str]]:
    """Every task, as absolute test path to absolute implementation paths."""
    return {task.test_path: task.impl_paths for task in load_tasks()}


def repo_root_for_path(path: str | os.PathLike) -> str:
    """The repository checkout (or its copy) that contains the path.

    A checkout lives directly under the sources root; a copy lives two levels
    under the copies root (run directory, then the copy itself). Both checks are lexical,
    so a path inside the eval container, which mounts the copy at its host path, resolves
    the same way as on the host.
    """
    p = Path(path)
    for base, depth in ((locations.sources_root, 1), (locations.repo_copies_dir, 2)):
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        if len(rel.parts) >= depth:
            return str(base.joinpath(*rel.parts[:depth]))
    raise ValueError(f"{path} is neither under {locations.sources_root} nor under {locations.repo_copies_dir}")


def repo_name_for_path(path: str | os.PathLike, stripped: bool = False) -> str:
    """Name of the repository that contains the path. With `stripped`, a copy's suffix is removed."""
    name = os.path.basename(repo_root_for_path(path))
    return name.split(REPO_COPY_DIR_SPLITTER, 1)[0] if stripped else name


def path_relative_to_repo(path: str) -> str:
    """The path relative to the root of its repository, for a checkout or a copy."""
    return os.path.relpath(path, repo_root_for_path(path))


def repo_relative_id(path: str | os.PathLike) -> str:
    """The stable name of a file across machines: `<repository>/<path inside the repository>`.

    Task ids in run records are this, for the task's test file. Works for a checkout and
    for a copy, whose directory suffix is stripped.
    """
    return f"{repo_name_for_path(path, stripped=True)}/{path_relative_to_repo(str(path))}"


def task_sdk_version(test_file: str) -> str:
    """The Daml SDK version that the task builds with.

    The repository's handler decides it when it has an `sdk_version` hook. Otherwise it is
    the version that the test file's package declares in its daml.yaml.

    The resolver returns a result object because other callers tolerate an unknown
    version; the harness cannot (container setup and docs selection need one), so this
    raises instead. The repository root lets the resolver find the `.envrc` that defines a
    `${DAML_VERSION}` placeholder.
    """
    handler = handler_for(repo_name_for_path(test_file, stripped=True))
    if handler.sdk_version is not None and (version := handler.sdk_version(test_file)) is not None:
        return version
    resolution = resolve_daml_sdk_version(test_file, repo_root=repo_root_for_path(test_file))
    if resolution.version is None:
        raise ValueError(resolution.error or f"Could not determine Daml SDK version for {test_file}.")
    return resolution.version


def _resolve_task_key(task_id: str, available: dict[str, list[str]]) -> str | None:
    """The test file path behind a task id, `<repository>/<path to the test file>`, or None."""
    wanted = task_id.replace("\\", "/")
    for available_key in available:
        if repo_relative_id(available_key) == wanted:
            return available_key
    return None


def get_selected_tasks(config: ExperimentConfig) -> dict[str, list[str]]:
    """Which tasks a run covers, as a map from each test file to its implementation files.

    `config.tasks` names them outright. Otherwise `config.task_set` takes all of them or
    one per repository, and `config.tasks_per_repo` caps how many come from each.
    """
    available = {test_file: impl_files for test_file, impl_files in impl_files_by_test_file().items() if impl_files}
    filtered = available

    if config.tasks is not None:
        if not isinstance(config.tasks, list):
            raise ValueError("config.tasks must be a list of task ids or None")
        if not config.tasks:
            raise ValueError("config.tasks must contain at least one task id when provided")
        non_string = [item for item in config.tasks if not isinstance(item, str)]
        if non_string:
            raise ValueError("config.tasks must contain only string task ids")
        resolved_test_files: list[str] = []
        missing: list[str] = []
        for task_id in config.tasks:
            resolved = _resolve_task_key(task_id, available)
            if resolved is None:
                missing.append(task_id)
                continue
            resolved_test_files.append(resolved)
        if missing:
            raise ValueError(f"Unknown task id(s): {missing}")
        return {test_file: available[test_file] for test_file in resolved_test_files}

    if config.task_set == "one_per_repo":
        per_repo = 1
    elif config.task_set == "all":
        per_repo = config.tasks_per_repo
    else:
        raise ValueError(f"Unknown task_set: {config.task_set}")

    ordered = sorted(filtered.items(), key=lambda item: item[0])
    if per_repo is None:
        return dict(ordered)

    selected: dict[str, list[str]] = {}
    taken: Counter[str] = Counter()
    for test_file, impl_files in ordered:
        repo_name = Path(repo_root_for_path(test_file)).name
        if taken[repo_name] >= per_repo:
            continue
        selected[test_file] = impl_files
        taken[repo_name] += 1
    return selected
