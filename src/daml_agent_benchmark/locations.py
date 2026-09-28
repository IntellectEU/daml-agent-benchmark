"""Where the benchmark reads and writes on this machine.

Everything location-specific hangs off one `locations` object: the root that logs and
repository copies go under, the directory for red-team results, the directory that
holds the source repositories, and the task-list directories that declare tasks and
repositories. The defaults suit a checkout of this repository: the root is the
repository, sources live in `sources/`, and the package's own task list is the only
one. `configure()` overrides them from code, before tasks are loaded or a run starts:
an `experiment.py` can call it to point at sources kept elsewhere. Extra code, code
outside the package that adds its own repositories and tasks, calls it to add its task
list and handlers' paths. It also adds its own source directory to `extra_code_dirs`,
so a run's code snapshot includes the extra code's modules that were loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from daml_agent_benchmark.constants import PACKAGE_DIR, REPO_COPIES_DIR_NAME

PACKAGE_TASKLIST_DIR = PACKAGE_DIR / "tasklist"
_DEFAULT_ROOT = PACKAGE_DIR.parents[1]


@dataclass
class Locations:
    root: Path
    sources_root: Path
    tasklist_dirs: tuple[Path, ...]
    logs_dir: Path
    redteam_logs_dir: Path
    repo_copies_dir: Path
    # Extra `repo_*.sh` scripts the SDK store bootstrap runs, for repositories outside the package.
    sdk_bootstrap_extra_dir: Path | None = None
    # Source directories of extra code that adds repositories and tasks. A run's code snapshot includes their loaded modules.
    extra_code_dirs: tuple[Path, ...] = ()


locations = Locations(
    root=_DEFAULT_ROOT,
    sources_root=_DEFAULT_ROOT / "sources",
    tasklist_dirs=(PACKAGE_TASKLIST_DIR,),
    logs_dir=_DEFAULT_ROOT / "logs",
    redteam_logs_dir=_DEFAULT_ROOT / "redteam_logs",
    repo_copies_dir=_DEFAULT_ROOT / REPO_COPIES_DIR_NAME,
)


def configure(**overrides: object) -> Locations:
    """Change locations in place. Call before tasks are loaded or a run starts."""
    for name, value in overrides.items():
        if not hasattr(locations, name):
            raise AttributeError(f"unknown location {name!r}")
        setattr(locations, name, value)
    from daml_agent_benchmark import tasklist_catalog

    tasklist_catalog.clear_caches()
    return locations
