"""
Splice-specific repository-copy preparation helpers.

The `splice` repository (and its vendored `canton` submodule) is a Scala project. Its
Daml packages are normally built by SBT, the Scala build tool, which compiles them in
dependency order and places each `.dar` where the sibling packages expect it.
The benchmark builds generated code in isolated repository copies with the plain Daml build
tool, so SBT never runs.

This module provides hooks that do by hand what SBT would have done: resolve
cross-package data-dependencies, compile the missing sibling `.dar` files, and
patch package-local references so the copy builds on its own.

It also points the copy at an SDK that can be installed. The daml.yaml files at the pinned
commit name an SDK snapshot version that has no release tag of its own. `daml install`
finds releases by tag, so it cannot install that version. The same SDK is published under
another tag, and the copy's daml.yaml files are rewritten to name that tag.
"""

from __future__ import annotations

import re
from pathlib import Path

from daml_agent_benchmark.repos.build import build_daml_package
from daml_agent_benchmark.repos.sdk_version import resolve_daml_sdk_version
from daml_agent_benchmark.repos.registry import RepoHandler, register
from daml_agent_benchmark.repos.common import (
    append_build_option,
    append_data_dependency,
    find_package_root_for_target,
    get_package_name_from_yaml,
    ledger_common_dars_root,
    list_resource_managed_dars,
    resolve_missing_data_dependencies,
    resource_managed_dep_relpath,
    restore_resource_managed_dars,
)
from daml_agent_benchmark.repos.repo_copy import _command_prefix_for_repo_copy

# The SDK version that splice's daml.yaml files name at the pinned commit.
SPLICE_DAML_SDK_VERSION = "3.3.0-snapshot.20250502.13767.0.v2fc6c7e2"
# The release tag under which that SDK is published. Its SDK reports the version above.
SPLICE_DAML_RELEASE_TAG = "3.3.0-snapshot.20250507.0"
_SPLICE_SDK_VERSION_LINE = re.compile(rf"^sdk-version:\s*{re.escape(SPLICE_DAML_SDK_VERSION)}\s*$", re.MULTILINE)


def _splice_sdk_version(test_file: str) -> str | None:
    """The release tag for tasks whose package names splice's snapshot version, else None."""
    if resolve_daml_sdk_version(test_file).version == SPLICE_DAML_SDK_VERSION:
        return SPLICE_DAML_RELEASE_TAG
    return None


def _use_installable_sdk(repo_copy: Path) -> None:
    """Rewrite every daml.yaml in the copy that names the snapshot version to name its release tag.

    Sibling packages that the copy builds need the tag too, so every package is rewritten,
    not only the target's.
    """
    for daml_yaml in repo_copy.rglob("daml.yaml"):
        text = daml_yaml.read_text(encoding="utf-8")
        rewritten = _SPLICE_SDK_VERSION_LINE.sub(f"sdk-version: {SPLICE_DAML_RELEASE_TAG}", text)
        if rewritten != text:
            daml_yaml.write_text(rewritten, encoding="utf-8")


def _build_splice_resource_managed_package(repo_copy: Path, package_root: Path, dar_name: str) -> Path | None:
    """Build and return the DAR expected in Splice managed resources."""
    dist_dar = package_root / ".daml" / "dist" / dar_name
    if not dist_dar.exists():
        res = build_daml_package(package_root, command_prefix=_command_prefix_for_repo_copy(repo_copy))
        if res.returncode != 0:
            raise RuntimeError(f"Splice resource managed build failed in {package_root}:\n{res.stderr or res.stdout}")
    return dist_dar if dist_dar.exists() else None


def _build_splice_dependency_package(repo_copy: Path, package_root: Path) -> None:
    """Build one sibling Splice package to satisfy a missing data dependency."""
    res = build_daml_package(package_root, command_prefix=_command_prefix_for_repo_copy(repo_copy))
    if res.returncode != 0:
        raise RuntimeError(f"Splice dependency build failed in {package_root}:\n{res.stderr or res.stdout}")


def _prepare_splice_package_configs(repo_root: Path, repo_copy: Path, target_files: list[Path]) -> list[str]:
    """Patch Splice package configs and collect required managed DARs."""
    required_dars: list[str] = []
    for target in target_files:
        package_root = find_package_root_for_target(repo_root, repo_copy, target)
        if package_root is None:
            continue
        daml_yaml = package_root / "daml.yaml"

        required_dars.extend(list_resource_managed_dars(daml_yaml))

        if ledger_common_dars_root(package_root) is None:
            continue

        package_name = get_package_name_from_yaml(daml_yaml)
        if package_name in {"model-tests", "semantic-tests"}:
            append_build_option(daml_yaml, "-Wno-upgrade-interfaces")

        if package_name == "model-tests":
            dep_name = "model-iface-tests-3.1.0.dar"
            append_data_dependency(daml_yaml, resource_managed_dep_relpath(package_root, dep_name))
            required_dars.append(dep_name)

    return sorted(set(required_dars))


def _create_dangling_current_symlink(package_root: Path) -> None:
    """Pre-create the `-current.dar` alias for a package that eval builds later.

    splice packages reference each other by a version-independent alias
    (`data-dependencies: ../<pkg>/.daml/dist/<name>-current.dar`) that splice's own
    build tooling maintains as a symlink to the latest versioned DAR; plain
    `daml build` only ever writes the versioned file. Target packages must never
    be pre-built from ground truth (the DAR would hand the agent the compiled
    solution), so at prep time there is no DAR at all — this creates the alias as
    a DANGLING symlink: it points at the versioned DAR filename that does not
    exist yet. That is intentional and harmless (nothing reads it during prep, and
    a dangling link carries no content to leak). The moment the eval's build stage
    compiles the (agent-written) target package and produces the versioned DAR,
    the alias starts resolving and dependent packages' builds find it.
    """
    import yaml

    daml_yaml = package_root / "daml.yaml"
    if not daml_yaml.exists():
        return
    config = yaml.safe_load(daml_yaml.read_text()) or {}
    name, version = config.get("name"), config.get("version")
    if not name or not version:
        return
    dist_dir = package_root / ".daml" / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    symlink_path = dist_dir / f"{name}-current.dar"
    if not symlink_path.exists() and not symlink_path.is_symlink():
        symlink_path.symlink_to(f"{name}-{version}.dar")


def _restore_deps_for_splice(repo_root: Path, repo_copy: Path, target_files: list[Path]) -> None:
    """
    Resolves data-dependencies for Splice and Canton packages.

    Because the evaluator wipes target implementation files, building all
    packages will fail if sibling packages try to resolve the target's `.dar` or vice-versa.
    This function pre-compiles any missing sibling packages that the target files
    depend on and correctly wires up the expected `-current.dar` symlinks or
    resource-managed directories before the evaluator runs its standalone build.

    Packages that contain a target file are NEVER pre-built: their compiled DAR
    would embed the ground-truth solution (the exact leak class caught with
    another repository, where an agent ran `strings` over the target's DAR). They get a
    dangling `-current.dar` symlink instead, satisfied by the eval-time build.

    The copy's daml.yaml files are pointed at the installable SDK first, since every build
    here needs it.
    """
    _use_installable_sdk(repo_copy)
    target_package_roots = {
        root.resolve()
        for target in target_files
        if (root := find_package_root_for_target(repo_root, repo_copy, target)) is not None
    }

    def build_or_skip_target(sibling_dir: Path) -> None:
        if sibling_dir.resolve() in target_package_roots:
            _create_dangling_current_symlink(sibling_dir)
            return
        _build_splice_dependency_package(repo_copy, sibling_dir)

    # Recursively resolve and build missing data-dependencies for target files
    for target in target_files:
        package_root = find_package_root_for_target(repo_root, repo_copy, target)
        if package_root is not None:
            resolve_missing_data_dependencies(package_root / "daml.yaml", build_or_skip_target)
            if package_root.resolve() in target_package_roots:
                _create_dangling_current_symlink(package_root)

    required_dars = _prepare_splice_package_configs(repo_root, repo_copy, target_files)
    restore_resource_managed_dars(
        repo_root,
        repo_copy,
        target_files,
        required_dars,
        lambda package_root, dar_name: _build_splice_resource_managed_package(repo_copy, package_root, dar_name),
    )


register(
    RepoHandler(
        name="splice",
        restore_deps=_restore_deps_for_splice,
        restore_in_repo_copy=True,
        # daml/dars ships released DARs including current versions of every target package,
        # compiled solutions; the rest is vendored canton and app code that only bloats the
        # copy.
        excluded_subpaths=("daml/dars", "canton", "apps", "apps-frontends", "cluster"),
        sdk_version=_splice_sdk_version,
    )
)
