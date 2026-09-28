"""account-hierarchy-specific copy dependency restoration.

The repo's packages all depend on lib-finance (a git submodule, materialized as
plain files at the pinned commit) via lib-finance/model/.daml/dist/finlib-2.0.0.dar,
and on each other via .daml/dist DARs. Only the lib-finance DARs are safe to
restore: every other package's dist DAR embeds the account-hierarchy-core-model
dalfs — i.e. a compiled copy of the solution the agent is asked to write.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from daml_agent_benchmark.repos.registry import RepoHandler, register

_LIB_FINANCE_SUBMODULE_COMMIT = "0b21a0723db01f63f7d6b16ed59ea8d37030219f"
_LIB_FINANCE_URL = "https://github.com/digital-asset/lib-finance"


def _materialize_lib_finance(repo_root: Path) -> None:
    """Fetch the lib-finance submodule content if the checkout doesn't have it.

    The content is cloned at the pinned submodule commit, and the clone's .git is
    removed, because a source checkout must not contain a live git directory.
    """
    lib_finance = repo_root / "lib-finance"
    if (lib_finance / "model").is_dir():
        return
    subprocess.run(
        ["git", "clone", _LIB_FINANCE_URL, str(lib_finance)],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "checkout", _LIB_FINANCE_SUBMODULE_COMMIT],
        cwd=lib_finance, capture_output=True, text=True, check=True,
    )
    shutil.rmtree(lib_finance / ".git")


def _restore_deps_for_account_hierarchy(repo_root: Path, repo_copy: Path, target_files: list[Path]) -> None:
    """Make an account-hierarchy task's repository copy buildable without exposing the solution.

    Background: every package in this repo depends on lib-finance, an external library
    that lives in the repo as a git submodule (its files are materialized in-tree at the
    pinned commit). Packages consume each other as compiled .daml/dist DAR files — and a
    compiled DAR bundles the code of everything it depends on, which is what makes most
    of them dangerous here: any DAR of a package that depends on core-model contains a
    compiled (decompilable) copy of core-model, i.e. of the very code the agent is asked
    to write. Only the lib-finance DARs are safe, because lib-finance depends on nothing
    in this repo.

    Three steps:
    1. Provide the lib-finance DAR the packages depend on: build it from the submodule
       sources if it doesn't exist yet, then place it in the copy.
    2. Remove every other DAR that the generic dist restore brought in (core-model/test,
       examples, core-trigger, ...) — per the background above, they all embed the
       solution.
    3. Give the implementation package a "package database" (the compiler's index of a
       package's dependencies). The eval's first stage lints the implementation files on
       their own, and the linter — unlike a full build — cannot create this index itself,
       so without it the lint fails to resolve the lib-finance imports even on perfectly
       good code. The SDK has no command that only creates the index, so we run a full
       build and then keep only the index: the DAR it produced and the `.hie` interface
       files both carry the package's own code (the `.hie` files embed the module source
       verbatim), while the index only describes dependencies (lib-finance + the standard
       library), so nothing solution-shaped stays behind.
    """
    repo_root = Path(repo_root)
    repo_copy = Path(repo_copy)

    # Step 1: the lib-finance dependency DAR.
    _materialize_lib_finance(repo_root)
    finlib_model = repo_root / "lib-finance" / "model"
    finlib_dar = finlib_model / ".daml" / "dist" / "finlib-2.0.0.dar"
    if not finlib_dar.exists() and finlib_model.is_dir():
        subprocess.run(["daml", "build"], cwd=finlib_model, capture_output=True, text=True, check=True)
    if finlib_dar.exists():
        dest = repo_copy / "lib-finance" / "model" / ".daml" / "dist" / finlib_dar.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(finlib_dar, dest)

    # Step 2: remove the solution-embedding DARs.
    lib_finance_root = (repo_copy / "lib-finance").resolve()
    for dist_dir in repo_copy.rglob(".daml/dist"):
        if not dist_dir.resolve().is_relative_to(lib_finance_root):
            shutil.rmtree(dist_dir, ignore_errors=True)

    # Step 3: package database for the linter (build, then keep only the index — see docstring).
    impl_package_root = repo_copy / "core-model" / "main"
    subprocess.run(["daml", "build"], cwd=impl_package_root, capture_output=True, text=True, check=True)
    build_dir = impl_package_root / ".daml"
    for entry in build_dir.iterdir():
        if entry.name != "package-database":
            shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink()


register(RepoHandler(name="account-hierarchy", restore_deps=_restore_deps_for_account_hierarchy, restore_in_repo_copy=True))
