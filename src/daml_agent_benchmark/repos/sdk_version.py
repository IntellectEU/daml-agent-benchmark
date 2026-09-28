"""Shared Daml package version discovery.

The Daml package config names this `sdk-version`. The resolved value stands in
for the Daml language and compiler generation. Callers decide what to do when
the version is unknown: the benchmark fails fast, other callers may fall back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class _DamlSdkVersionResolution:
    """Result of resolving a Daml package's SDK/compiler version.

    `version` is the concrete resolved value, or None when unknown. `raw_version`
    is the value read from daml.yaml before template resolution, when one was
    present. `error` is diagnostic text for callers that need to raise/log.
    """

    version: str | None
    raw_version: str | None
    daml_yaml_path: Path | None
    error: str | None = None


def find_daml_yaml_root(start_path: str | Path) -> Path:
    """Return the nearest ancestor directory containing `daml.yaml`.

    Accepts either a file or directory path.
    """

    current_dir = Path(start_path)
    if current_dir.is_file():
        current_dir = current_dir.parent
    current_dir = current_dir.resolve()

    while True:
        if (current_dir / "daml.yaml").exists():
            return current_dir
        parent_dir = current_dir.parent
        if parent_dir == current_dir:
            raise FileNotFoundError(f"Could not find daml.yaml in any parent directory of {start_path}.")
        current_dir = parent_dir


def find_daml_build_root(start_path: str | Path) -> Path:
    """Return the Daml build root for a package file or directory.

    Multi-package projects build from the nearest ancestor containing
    `multi-package.yaml`; single-package projects build from the nearest
    `daml.yaml` directory.
    """

    current_dir = Path(start_path)
    if current_dir.is_file():
        current_dir = current_dir.parent
    search_dir = current_dir.resolve()

    while True:
        if (search_dir / "multi-package.yaml").exists():
            return search_dir
        parent_dir = search_dir.parent
        if parent_dir == search_dir:
            return find_daml_yaml_root(start_path)
        search_dir = parent_dir


def _extract_raw_version(daml_config: object) -> str | None:
    """Extract the version field from parsed daml.yaml content.

    Prefer top-level `sdk-version`. If absent, support Canton-style package
    configs that pin components under `override-components.damlc.version`.
    """

    if not isinstance(daml_config, dict):
        return None
    sdk_version = daml_config.get("sdk-version")
    if sdk_version:
        return str(sdk_version)
    override_components = daml_config.get("override-components")
    if not isinstance(override_components, dict):
        return None
    damlc = override_components.get("damlc")
    if not isinstance(damlc, dict):
        return None
    version = damlc.get("version")
    return str(version) if version else None


_TEMPLATE_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")


def templated_version_variable(raw_version: str) -> str | None:
    """Return the environment variable name referenced by a templated version.

    Supports `$VAR`, `${VAR}`, and the SDK repo's `__VERSION__` placeholder. A
    literal version returns None.
    """

    if raw_version == "__VERSION__":
        return "DAML_VERSION"
    match = _TEMPLATE_RE.match(raw_version)
    return match.group(1) if match else None


def _resolve_env_version(search_roots: list[Path], var_name: str) -> str | None:
    """Find a numeric-looking variable assignment in `.envrc` or `Makefile`.

    This intentionally does not execute shell or make code. It only recognizes
    direct assignments such as `DAML_VERSION=3.4.11` or `DAML_VERSION := 3.4.11`.
    """

    for root in search_roots:
        for filename in (".envrc", "Makefile"):
            path = root / filename
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8")
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                match = re.search(rf"{re.escape(var_name)}\s*[:=]?=?\s*[\"']?([0-9][^\"'\s]*)", stripped)
                if match:
                    return match.group(1)
    return None


def _candidate_env_roots(daml_yaml_root: Path, build_root: Path | None, repo_root: Path | None) -> list[Path]:
    """Return the ordered, de-duplicated roots to inspect for version variables."""

    roots: list[Path] = []
    for root in (build_root, daml_yaml_root, repo_root):
        if root is not None and root not in roots:
            roots.append(root)
    return roots


def resolve_daml_sdk_version(
    start_path: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> _DamlSdkVersionResolution:
    """Resolve a Daml package SDK/compiler version.

    Reads the nearest `daml.yaml`, extracts either `sdk-version` or
    `override-components.damlc.version`, and resolves simple templated values
    from nearby `.envrc`/`Makefile` files. Unknown versions are represented by
    `_DamlSdkVersionResolution(version=None)` rather than exceptions so callers
    can apply their own policy.
    """

    try:
        try:
            build_root = find_daml_build_root(start_path)
        except FileNotFoundError:
            build_root = None
        try:
            daml_yaml_root = find_daml_yaml_root(start_path)
        except FileNotFoundError as exc:
            return _DamlSdkVersionResolution(None, None, None, str(exc))

        daml_yaml_path = daml_yaml_root / "daml.yaml"
        with daml_yaml_path.open("r", encoding="utf-8") as f:
            raw_version = _extract_raw_version(yaml.safe_load(f))
        if raw_version is None:
            return _DamlSdkVersionResolution(None, None, daml_yaml_path, "'sdk-version' not found in daml.yaml")

        var_name = templated_version_variable(raw_version)
        if var_name is None:
            return _DamlSdkVersionResolution(raw_version, raw_version, daml_yaml_path)

        repo_root_path = Path(repo_root).resolve() if repo_root is not None else None
        version = _resolve_env_version(_candidate_env_roots(daml_yaml_root, build_root, repo_root_path), var_name)
        if version is None:
            return _DamlSdkVersionResolution(None, raw_version, daml_yaml_path, f"Variable {var_name} not resolved")
        return _DamlSdkVersionResolution(version, raw_version, daml_yaml_path)
    except (OSError, yaml.YAMLError, TypeError) as exc:
        return _DamlSdkVersionResolution(None, None, None, str(exc))
