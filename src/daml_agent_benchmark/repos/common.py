"""Shared helpers for the per-repository fix-ups a task's copy needs.

These functions only ever edit files inside the copy. They exist for the small
compatibility patches several repositories need alike; each repository still owns the
decision about which of its files to patch.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Callable

import yaml


def set_daml_sdk_version(daml_yaml: Path, sdk_version: str) -> None:
    """Patch one copied daml.yaml so standalone `daml build` knows the SDK."""
    text = daml_yaml.read_text(encoding="utf-8")
    if re.search(r"^sdk-version:", text, flags=re.MULTILINE):
        text = re.sub(r"^sdk-version:\s*.*$", f"sdk-version: {sdk_version}", text, flags=re.MULTILINE)
    else:
        text = f"sdk-version: {sdk_version}\n{text}"

    text = re.sub(r"^(\s*version:\s*)\$DAML_VERSION\s*$", rf"\g<1>{sdk_version}", text, flags=re.MULTILINE)
    daml_yaml.write_text(text, encoding="utf-8")


def get_package_name_from_yaml(daml_yaml_path: Path) -> str | None:
    """Return the package name declared in a `daml.yaml` file."""
    if not daml_yaml_path.exists():
        return None
    for line in daml_yaml_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


def canton_package_build_options(package_root: Path) -> list[str]:
    """Return Canton package build options normally supplied by its SBT/DPM path."""
    options = ["--ghc-option=-DDAML_NUCK"]
    package_name = get_package_name_from_yaml(package_root / "daml.yaml")
    if package_name in {"model-tests", "semantic-tests", "benchtool-tests"}:
        options.append("-Wno-upgrade-interfaces")
    if package_name == "benchtool-tests":
        options.append("--target=2.dev")
    return options


def append_build_option(daml_yaml: Path, option: str) -> None:
    """Add one build option to a copied `daml.yaml` file if missing."""
    text = daml_yaml.read_text(encoding="utf-8")
    if option in text:
        return

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "build-options:":
            lines.insert(i + 1, f"- {option}")
            daml_yaml.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return

    lines.extend(["build-options:", f"- {option}"])
    daml_yaml.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_data_dependency(daml_yaml: Path, dependency: str) -> None:
    """Add one data dependency to a copied `daml.yaml` file if missing."""
    text = daml_yaml.read_text(encoding="utf-8")
    if dependency in text:
        return

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "data-dependencies:":
            lines.insert(i + 1, f"- {dependency}")
            daml_yaml.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return

    lines.extend(["data-dependencies:", f"- {dependency}"])
    daml_yaml.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ledger_common_dars_root(path: Path) -> Path | None:
    """Return the enclosing `ledger-common-dars` root for `path`, if any."""
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name != "ledger-common-dars":
            continue
        if len(candidate.parts) >= 3 and candidate.parts[-3:] == ("community", "ledger", "ledger-common-dars"):
            return candidate
    return None


def _resource_managed_dependency_dir(ledger_common_dars: Path) -> Path:
    """Return the managed DAR directory for one `ledger-common-dars` tree."""
    return ledger_common_dars / "scala-2.13" / "resource_managed" / "main"


def resource_managed_dep_relpath(package_root: Path, dep_name: str) -> str:
    """Return the package-relative path to one managed DAR dependency."""
    dep_path = _resource_managed_dependency_dir(ledger_common_dars_root(package_root) or package_root) / dep_name
    return os.path.relpath(dep_path, package_root)


def list_resource_managed_dars(daml_yaml: Path) -> list[str]:
    """Return managed DAR filenames referenced by one `daml.yaml` file."""
    deps: list[str] = []
    for line in daml_yaml.read_text(encoding="utf-8").splitlines():
        dep = line.strip()
        if not dep.startswith("- "):
            continue
        dep = dep[2:].strip()
        if "resource_managed/main/" in dep and dep.endswith(".dar"):
            deps.append(Path(dep).name)
    return deps


def list_resource_managed_dependencies(daml_yaml: Path) -> list[tuple[str, str]]:
    """Return `(dependency_path, dar_name)` pairs for managed-resource DARs."""
    deps: list[tuple[str, str]] = []
    for line in daml_yaml.read_text(encoding="utf-8").splitlines():
        dep = line.strip()
        if not dep.startswith("- "):
            continue
        dep = dep[2:].strip()
        if "resource_managed/main/" in dep and dep.endswith(".dar"):
            deps.append((dep, Path(dep).name))
    return deps


def find_package_root_for_target(repo_root: Path, repo_copy: Path, target: Path) -> Path | None:
    """Return the copied package root for one target file in the copy."""
    target_in_repo_copy = repo_copy / target.resolve().relative_to(repo_root.resolve())
    package_root = target_in_repo_copy.parent
    while package_root != repo_copy and not (package_root / "daml.yaml").exists():
        package_root = package_root.parent
    daml_yaml = package_root / "daml.yaml"
    return package_root if daml_yaml.exists() else None


def _splice_style_package_roots(ledger_common_dars: Path) -> dict[str, Path]:
    """Map package names to package roots under `src/main/daml`."""
    package_roots: dict[str, Path] = {}
    for daml_yaml in (ledger_common_dars / "src" / "main" / "daml").rglob("daml.yaml"):
        name = get_package_name_from_yaml(daml_yaml)
        if name:
            package_roots[name] = daml_yaml.parent
    return package_roots


def repo_package_roots(repo_root: Path) -> dict[str, Path]:
    """Map package names to package roots for all `daml.yaml` files under a repo."""
    package_roots: dict[str, Path] = {}
    for daml_yaml in repo_root.rglob("daml.yaml"):
        name = get_package_name_from_yaml(daml_yaml)
        if name:
            package_roots[name] = daml_yaml.parent
    return package_roots


def source_package_for_dar(package_roots: dict[str, Path], dar_name: str) -> Path | None:
    """Return the source package root that should build `dar_name`."""
    stem = dar_name.removesuffix(".dar")
    if stem in package_roots:
        return package_roots[stem]
    matching_names = [name for name in package_roots if stem == name or stem.startswith(f"{name}-")]
    if not matching_names:
        return None
    return package_roots[max(matching_names, key=len)]


def _create_current_symlink(package_dir: Path) -> None:
    """Create `-current.dar` aliases for built DARs in one package."""
    dist_dir = package_dir / ".daml" / "dist"
    if not dist_dir.exists():
        return
    for filename in os.listdir(dist_dir):
        if filename.endswith(".dar") and "-current.dar" not in filename:
            pkg_name = filename.rsplit("-", 1)[0]
            symlink_name = f"{pkg_name}-current.dar"
            symlink_path = dist_dir / symlink_name
            if not symlink_path.exists():
                try:
                    os.symlink(filename, symlink_path)
                except OSError:
                    shutil.copy2(dist_dir / filename, symlink_path)


def resolve_missing_data_dependencies(
    daml_yaml_path: Path,
    build_package: Callable[[Path], None],
    seen: set[Path] | None = None,
) -> None:
    """Build sibling packages needed by missing `.daml/dist` dependencies."""
    if seen is None:
        seen = set()
    abs_root = daml_yaml_path.parent.resolve()
    if abs_root in seen:
        return
    seen.add(abs_root)
    with daml_yaml_path.open("r", encoding="utf-8") as f:
        daml_config = yaml.safe_load(f)
    if not isinstance(daml_config, dict):
        return
    deps = daml_config.get("data-dependencies") or []
    if not isinstance(deps, list):
        return
    for dep in deps:
        if not isinstance(dep, str) or (daml_yaml_path.parent / dep).exists() or ".daml/dist/" not in dep:
            continue
        rel_sibling_path = dep.split(".daml/dist/")[0]
        sibling_dir = (daml_yaml_path.parent / rel_sibling_path).resolve()
        sibling_yaml = sibling_dir / "daml.yaml"
        if not sibling_yaml.exists():
            continue
        resolve_missing_data_dependencies(sibling_yaml, build_package, seen)
        build_package(sibling_dir)
        _create_current_symlink(sibling_dir)


def restore_resource_managed_dars(
    repo_root: Path,
    repo_copy: Path,
    target_files: list[Path],
    required_dars: list[str],
    build_resource_managed_package: Callable[[Path, str], Path | None],
) -> None:
    """Populate missing `resource_managed/main/*.dar` files in the copy."""
    ledger_common_dars = ledger_common_dars_root(repo_copy / target_files[0].resolve().relative_to(repo_root.resolve()))
    if ledger_common_dars is None or not required_dars:
        return

    package_roots = _splice_style_package_roots(ledger_common_dars)
    dep_dir = _resource_managed_dependency_dir(ledger_common_dars)
    dep_dir.mkdir(parents=True, exist_ok=True)
    target_package_roots = {
        root
        for target in target_files
        for root in [find_package_root_for_target(repo_root, repo_copy, target)]
        if root is not None
    }

    for dar_name in sorted(set(required_dars)):
        dest = dep_dir / dar_name
        if dest.exists():
            continue
        source_package = source_package_for_dar(package_roots, dar_name)
        if source_package is None or source_package in target_package_roots:
            continue
        built_dar = build_resource_managed_package(source_package, dar_name)
        if built_dar is not None:
            shutil.copy2(built_dar, dest)
