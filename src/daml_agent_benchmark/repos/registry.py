"""Per-repository behaviour of the harness.

Most repositories build from a plain copy of their source tree. Some need help: a
dependency DAR fetched or built, a symlink that the copy dropped put back, an extra build
flag, a subtree kept out of the copy because it contains the answer. Each such need
is a field of a RepoHandler, and a repository has at most one handler, registered under
its checkout name. A repository with no handler gets the defaults.

The handlers of the repositories shipped with the package live next to this module and
register themselves when loaded; extra code with more repositories registers its own by
calling `register` before the harness runs.
"""

from __future__ import annotations

import importlib
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

RestoreDeps = Callable[[Path, Path, list[Path]], None]
BuildArgs = Callable[[str, Path], list[str]]
PreBuild = Callable[[str, Path, list[str]], subprocess.CompletedProcess[str] | None]
PreTest = Callable[[str, Path, list[str]], None]
ScopeRoot = Callable[[str], Path | None]
SdkVersion = Callable[[str], str | None]


@dataclass(frozen=True)
class RepoHandler:
    name: str
    # Copies or builds the dependencies that a copy of the repository needs in order to build:
    # (repository root, repository copy's root, target files). Must never place a compiled form of a
    # target file into the copy.
    restore_deps: RestoreDeps | None = None
    # The handler's restore_deps accepts deps_cache=, a directory in which it stages downloads
    # and generated DARs instead of writing them into the repository checkout.
    restore_deps_takes_deps_cache: bool = False
    # Copy every non-target package's .daml/dist across before restore_deps runs.
    restore_dist_dirs: bool = True
    # Run restore_deps when preparing the copy the agent works from, not only for
    # repository copies that other callers prepare. Checked per repository: the handler must
    # copy no compiled artifact of a target package, and the repository's tasks must still
    # pass the ground-truth and empty-implementation validation with it active.
    restore_in_repo_copy: bool = False
    # Environment the task and eval containers get on top of the copy's .envrc exports.
    container_env: Mapping[str, str] = field(default_factory=dict)
    # Extra command-line arguments for the build: (implementation file, build root).
    build_args: BuildArgs | None = None
    # Runs before the build: (implementation file, build root, command prefix). Returns a
    # failed process to report a compile failure, None to continue. Raises for infrastructure
    # failures.
    pre_build: PreBuild | None = None
    # Runs before the tests: (test file, test package root, command prefix).
    pre_test: PreTest | None = None
    # Scope the agent's copy to the target package: maps a target file to the directory
    # that becomes the copy's whole content (None keeps the full tree for that file).
    # Only for repositories whose task packages are self-contained; scoping breaks
    # packages that data-depend on siblings.
    repo_copy_scope_root: ScopeRoot | None = None
    # Subtrees, relative to the repository root, that are left out of every copy of this
    # repository: solution leaks and vendored bulk.
    excluded_subpaths: tuple[str, ...] = ()
    # The SDK version that a task builds with, for repositories whose checkout does not
    # name an installable one: (test file in the checkout) -> version. None falls back to the
    # version that the package's daml.yaml declares. The copy's daml.yaml must name the same
    # version, so the handler's restore_deps writes it there when the checkout differs.
    sdk_version: SdkVersion | None = None
    # The repository's own `repo_*.sh` script in the SDK store bootstrap installs its SDK.
    # The generic install step then leaves the repository's tasks out.
    sdk_installed_by_repo_script: bool = False
    # Environment for the SDK store bootstrap container, for the repository's `repo_*.sh` script.
    sdk_store_env: Mapping[str, str] = field(default_factory=dict)


_HANDLERS: dict[str, RepoHandler] = {}
_BUILTIN_HANDLER_MODULES = ("account_hierarchy", "canton", "daml", "daml_finance", "ex_models", "splice")
_builtins_loaded = False


def register(handler: RepoHandler) -> None:
    if handler.name in _HANDLERS:
        raise ValueError(f"a handler for repository {handler.name!r} is already registered")
    _HANDLERS[handler.name] = handler


def load_builtin_handlers() -> None:
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    for module in _BUILTIN_HANDLER_MODULES:
        importlib.import_module(f"daml_agent_benchmark.repos.{module}")


def sdk_store_env() -> dict[str, str]:
    """The environment that every registered handler gives the SDK store bootstrap."""
    load_builtin_handlers()
    env: dict[str, str] = {}
    for handler in _HANDLERS.values():
        env.update(handler.sdk_store_env)
    return env


def handler_for(repo_name: str) -> RepoHandler:
    """The repository's registered handler, or the defaults when it registered none.

    Unknown names get the defaults too: callers may prepare repository copies of repositories
    outside the task list, and a plain copy is the right treatment there.
    Whether a name is a declared benchmark repository is checked where it matters, when
    the task list is loaded and when a package is routed to its build tool.
    """
    load_builtin_handlers()
    return _HANDLERS.get(repo_name) or RepoHandler(name=repo_name)
