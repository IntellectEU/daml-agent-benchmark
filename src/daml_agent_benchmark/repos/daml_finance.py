"""daml-finance-specific copy dependency restoration."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from daml_agent_benchmark.repos.registry import RepoHandler, register


_TUTORIAL_ALIAS_RE = re.compile(r"^(?P<source>\S+)\s+(?P<alias>\.lib/\S+\.dar)\s*$")


def _declared_dar_dependencies(daml_yaml: Path) -> list[str]:
    """Return `.dar` dependency paths declared in a package config."""
    data = yaml.safe_load(daml_yaml.read_text(encoding="utf-8")) or {}
    deps: list[str] = []
    for key in ("dependencies", "data-dependencies"):
        values = data.get(key) or []
        if isinstance(values, list):
            deps.extend(str(value) for value in values if isinstance(value, str) and value.endswith(".dar"))
    return deps


def _tutorial_alias_mapping(repo_root: Path) -> dict[str, str]:
    """Return tutorial `.lib` aliases mapped to the real DAR filenames they expect."""
    mapping: dict[str, str] = {}
    for config in (repo_root / "docs" / "code-samples").rglob("*.conf"):
        for line in config.read_text(encoding="utf-8").splitlines():
            match = _TUTORIAL_ALIAS_RE.match(line.strip())
            if not match:
                continue
            source_name = Path(match.group("source")).name
            alias_name = Path(match.group("alias")).name
            mapping[alias_name] = source_name
    return mapping


def _find_matching_dar(repo_root: Path, alias_name: str, alias_mapping: dict[str, str]) -> Path | None:
    """Find a built DAR in the repo that can satisfy one tutorial alias."""
    source_name = alias_mapping.get(alias_name, alias_name)
    alias_stem = Path(alias_name).stem
    source_stem = Path(source_name).stem
    candidates = sorted(
        path
        for path in repo_root.rglob("*.dar")
        if path.name == source_name
        or path.stem == source_stem
        or path.stem.startswith(f"{source_stem}-")
        or path.stem.startswith(f"{alias_stem}-")
    )
    preferred = [path for path in candidates if "/package/main/" in path.as_posix()]
    if preferred:
        return min(preferred, key=lambda path: len(path.as_posix()))
    if candidates:
        return min(candidates, key=lambda path: len(path.as_posix()))
    return None


def _restore_deps_for_daml_finance(repo_root: Path, repo_copy: Path, target_files: list[Path]) -> None:
    """Populate missing `.lib/*.dar` tutorial aliases inside daml-finance repository copies."""
    envrc = repo_copy / ".envrc"
    if envrc.exists():
        envrc.unlink()
    alias_mapping = _tutorial_alias_mapping(repo_root)

    for target in target_files:
        target_rel = target.resolve().relative_to(repo_root.resolve())
        package_root = (repo_copy / target_rel).parent
        while package_root != repo_copy and not (package_root / "daml.yaml").exists():
            package_root = package_root.parent
        daml_yaml = package_root / "daml.yaml"
        if not daml_yaml.exists():
            continue
        for dependency in _declared_dar_dependencies(daml_yaml):
            dep_path = package_root / dependency
            if dep_path.exists():
                continue
            if not dependency.startswith(".lib/"):
                continue
            source_dar = _find_matching_dar(repo_root, dep_path.name, alias_mapping)
            if source_dar is None:
                continue
            dep_path.parent.mkdir(parents=True, exist_ok=True)
            dep_path.write_bytes(source_dar.read_bytes())


def _build_lib_dependencies(impl_file: str, build_root: Path, command_prefix: list[str]) -> None:
    """Run `make` once to populate `.lib`, the dependency directory the packages build against."""
    from daml_agent_benchmark.tasklist_catalog import repo_root_for_path

    lib_dir = Path(repo_root_for_path(impl_file)) / ".lib"
    if lib_dir.exists():
        return None
    print("'.lib' directory for daml-finance not found; running 'make' to build dependencies, this may take long.")
    make = subprocess.run(command_prefix + ["make"], capture_output=True, text=True, cwd=build_root, check=False)
    if make.returncode != 0:
        print(f"--- 'make' failed ---\n{make.stderr}")
        raise subprocess.CalledProcessError(make.returncode, make.args, make.stdout, make.stderr)
    print("`make` completed successfully.")
    return None


register(RepoHandler(name="daml-finance", restore_deps=_restore_deps_for_daml_finance, pre_build=_build_lib_dependencies))
