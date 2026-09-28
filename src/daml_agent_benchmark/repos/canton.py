"""Canton-specific copy dependency and config fixes.

Canton normally builds Daml packages through SBT (the Scala build tool) and DPM. The copy mirrors
that path for DPM-configured packages instead of rewriting package versions for
legacy `daml build`.
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

from daml_agent_benchmark.repos.build import build_daml_package
from daml_agent_benchmark.repos.sdk_version import find_daml_yaml_root
from daml_agent_benchmark.repos.registry import RepoHandler, register
from daml_agent_benchmark.repos.common import (
    canton_package_build_options,
    find_package_root_for_target,
    list_resource_managed_dependencies,
    repo_package_roots,
    resolve_missing_data_dependencies,
    set_daml_sdk_version,
    source_package_for_dar,
)
from daml_agent_benchmark.repos.repo_copy import _command_prefix_for_repo_copy

_CANTON_DAML_RELEASE_TAG = "3.5.0-snapshot.20260326.1"
CANTON_DAML_SDK_VERSION = "3.5.0-snapshot.20260320.14626.0.v851f5585"
_CANTON_DPM_BOOTSTRAP_LOCK = threading.Lock()
_DAML_VERSION_RE = re.compile(r'val version:\s*String\s*=\s*"([^"]+)"')


def _canton_daml_version(repo_copy: Path) -> str | None:
    daml_versions = repo_copy / "project" / "project" / "DamlVersions.scala"
    if not daml_versions.exists():
        return None
    match = _DAML_VERSION_RE.search(daml_versions.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"Failed to read Canton Daml version from {daml_versions}")
    return match.group(1)


def _dpm_cache_root() -> Path:
    return Path.home() / ".dpm" / "cache"


def _component_root(component: str, version: str) -> Path:
    return _dpm_cache_root() / "components" / component / version


def _dpm_canton_sdk_available(version: str) -> bool:
    manifest = _dpm_cache_root() / "sdk" / "open-source" / f"{version}.yaml"
    required_paths = [
        manifest,
        _component_root("damlc", version) / "damlc-dist-dpm" / "damlc",
        _component_root("damlc", version) / "damlc-dist-dpm" / "resources",
        _component_root("daml-script", version) / "script-service.jar",
        _component_root("daml-script", version) / "daml-script-2.dev.dar",
        _component_root("codegen", version) / "binary.jar",
    ]
    return all(path.exists() for path in required_paths)


def _symlink_force(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    destination.symlink_to(source)


def _write_dpm_sdk_manifest(version: str) -> None:
    manifest = _dpm_cache_root() / "sdk" / "open-source" / f"{version}.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "apiVersion: digitalasset.com/v1\n"
        "kind: SdkManifest\n"
        "spec:\n"
        "  components:\n"
        "    codegen:\n"
        f"      version: {version}\n"
        "    daml-script:\n"
        f"      version: {version}\n"
        "    damlc:\n"
        f"      version: {version}\n"
        f"  version: {version}\n"
        "  edition: open-source\n",
        encoding="utf-8",
    )


def _import_classic_sdk_into_dpm_cache(classic_sdk: Path, version: str) -> None:
    _write_dpm_sdk_manifest(version)

    damlc_root = _component_root("damlc", version)
    damlc_dist = damlc_root / "damlc-dist-dpm"
    damlc_dist.mkdir(parents=True, exist_ok=True)
    _symlink_force(classic_sdk / "damlc" / "damlc", damlc_dist / "damlc")
    _symlink_force(classic_sdk / "damlc" / "resources", damlc_dist / "resources")
    _symlink_force(classic_sdk / "damlc" / "lib", damlc_dist / "lib")
    damlc_root.joinpath("component.yaml").write_text(
        "apiVersion: digitalasset.com/v1\n"
        "kind: Component\n"
        "spec:\n"
        "  commands:\n"
        "    - path: damlc-dist-dpm/damlc\n"
        "      name: damlc\n"
        "      desc: Compiler and IDE backend for the Daml programming language\n"
        "    - path: damlc-dist-dpm/damlc\n"
        "      name: build\n"
        "      desc: Build a Daml package or project\n"
        '      exec-args: ["build"]\n'
        "  exports:\n"
        "    damlc-binary:\n"
        "      conflict-strategy: fail\n"
        "      paths:\n"
        "        - damlc-dist-dpm/damlc\n",
        encoding="utf-8",
    )

    daml_script_root = _component_root("daml-script", version)
    daml_script_root.mkdir(parents=True, exist_ok=True)
    _symlink_force(classic_sdk / "daml-sdk" / "daml-sdk.jar", daml_script_root / "daml-script-binary_distribute.jar")
    _symlink_force(classic_sdk / "damlc" / "resources" / "script-service.jar", daml_script_root / "script-service.jar")
    for dar_name in ["daml-script-2.1.dar", "daml-script-2.2.dar", "daml-script-2.dev.dar", "daml-script-2.3-staging.dar"]:
        _symlink_force(classic_sdk / "daml-libs" / dar_name, daml_script_root / dar_name)
    daml_script_root.joinpath("component.yaml").write_text(
        "apiVersion: digitalasset.com/v1\n"
        "kind: Component\n"
        "spec:\n"
        "  jar-commands:\n"
        "    - path: daml-script-binary_distribute.jar\n"
        "      name: script\n"
        "      desc: Daml Script Binary\n"
        "  exports:\n"
        "    dars:\n"
        "      conflict-strategy: extend\n"
        "      paths:\n"
        "        - ./daml-script-2.1.dar\n"
        "        - ./daml-script-2.2.dar\n"
        "        - ./daml-script-2.dev.dar\n"
        "        - ./daml-script-2.3-staging.dar\n"
        "    script-service:\n"
        "      conflict-strategy: fail\n"
        "      paths:\n"
        "        - ./script-service.jar\n",
        encoding="utf-8",
    )

    codegen_root = _component_root("codegen", version)
    codegen_root.mkdir(parents=True, exist_ok=True)
    _symlink_force(classic_sdk / "daml-sdk" / "daml-sdk.jar", codegen_root / "binary.jar")
    codegen_root.joinpath("component.yaml").write_text(
        "apiVersion: digitalasset.com/v1\n"
        "kind: Component\n"
        "spec:\n"
        "  jar-commands:\n"
        "    - path: binary.jar\n"
        "      name: codegen-java\n"
        "      desc: Daml to Java compiler\n"
        '      jar-args: ["java"]\n'
        "    - path: binary.jar\n"
        "      name: codegen-js\n"
        "      desc: Daml to Javascript compiler\n"
        '      jar-args: ["js"]\n',
        encoding="utf-8",
    )


def ensure_canton_dpm_sdk_available(repo_copy: Path) -> None:
    """Make Canton's GitHub-only Daml SDK snapshot visible to DPM."""
    version = _canton_daml_version(repo_copy)
    if version is None or version != CANTON_DAML_SDK_VERSION:
        return

    with _CANTON_DPM_BOOTSTRAP_LOCK:
        if _dpm_canton_sdk_available(version):
            return

        classic_sdk = Path.home() / ".daml" / "sdk" / version
        if not classic_sdk.exists():
            install = subprocess.run(
                ["daml", "install", _CANTON_DAML_RELEASE_TAG],
                capture_output=True,
                text=True,
                check=False,
            )
            if install.returncode != 0:
                raise RuntimeError(
                    f"Failed to install Canton Daml SDK {_CANTON_DAML_RELEASE_TAG}:\n"
                    f"{install.stderr or install.stdout}"
                )
        if not classic_sdk.exists():
            raise RuntimeError(f"Daml SDK install did not create expected directory: {classic_sdk}")

        _import_classic_sdk_into_dpm_cache(classic_sdk, version)


def _build_canton_dependency_package(repo_copy: Path, package_root: Path) -> None:
    """Build one sibling Canton package to satisfy a missing data dependency."""
    res = build_daml_package(
        package_root,
        command_prefix=_command_prefix_for_repo_copy(repo_copy),
        extra_args=canton_package_build_options(package_root),
    )
    if res.returncode != 0:
        raise RuntimeError(f"Canton dependency build failed in {package_root}:\n{res.stderr or res.stdout}")


def _build_canton_resource_managed_package(repo_copy: Path, package_root: Path, dar_name: str) -> Path | None:
    """Build and return the DAR expected in Canton managed resources."""
    dist_dar = package_root / ".daml" / "dist" / dar_name
    if not dist_dar.exists():
        res = build_daml_package(
            package_root,
            command_prefix=_command_prefix_for_repo_copy(repo_copy),
            extra_args=canton_package_build_options(package_root),
        )
        if res.returncode != 0:
            raise RuntimeError(f"Canton resource managed build failed in {package_root}:\n{res.stderr or res.stdout}")
    if dist_dar.exists():
        return dist_dar
    dar_files = sorted((package_root / ".daml" / "dist").glob("*.dar"))
    return dar_files[0] if len(dar_files) == 1 else None


def _restore_deps_for_canton(repo_root: Path, repo_copy: Path, target_files: list[Path]) -> None:
    """Apply Canton-only patches to a copy of the repository.

    `repo_root` is the original canton checkout. `copy` is the
    disposable or prepared-baseline tree used by validation; only that tree is
    edited.


    We do this manully instead of using SBT like Canton repository itself does, because if we ran Canton SBT broadly, it could:

  - Try to build the target package while the target file is empty, causing prep to fail before evaluation.
  - Later, if run after model output, compile too much and blur the signal between “target implementation builds” and “repo-wide Canton build works.”
  - Populate many generated/resource-managed outputs, including artifacts for packages we intentionally want the final daml_build() step to compile.

    This approach is more granular: avoiding building target files related dars.
    """
    if repo_root.name != "canton":
        raise ValueError

    ensure_canton_dpm_sdk_available(repo_copy)

    package_roots = repo_package_roots(repo_copy)
    target_package_roots: set[Path] = set()
    required_deps: list[tuple[Path, str]] = []
    for target in target_files:
        package_root = find_package_root_for_target(repo_root, repo_copy, target)
        if package_root is None:
            continue
        target_package_roots.add(package_root)
        daml_yaml = package_root / "daml.yaml"

        # Pin $DAML_VERSION (used in override-components) to the concrete snapshot so
        # `dpm build`/`dpm damlc test` resolve components offline — the eval/agent
        # containers have that exact version baked into the dpm cache.
        set_daml_sdk_version(daml_yaml, CANTON_DAML_SDK_VERSION)

        for dependency, dar_name in list_resource_managed_dependencies(daml_yaml):
            required_deps.append((package_root / dependency, dar_name))
        resolve_missing_data_dependencies(
            daml_yaml,
            lambda sibling_dir: _build_canton_dependency_package(repo_copy, sibling_dir),
        )

    for dest, dar_name in sorted(set(required_deps)):
        if dest.exists():
            continue
        source_package = source_package_for_dar(package_roots, dar_name)
        if source_package is None or source_package in target_package_roots:
            continue
        built_dar = _build_canton_resource_managed_package(repo_copy, source_package, dar_name)
        if built_dar is None:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(built_dar.read_bytes())


def _canton_build_args(impl_file: str, build_root: Path) -> list[str]:
    del impl_file
    return canton_package_build_options(build_root)


register(
    RepoHandler(
        name="canton",
        restore_deps=_restore_deps_for_canton,
        # dpm expands the `$DAML_VERSION` placeholder in canton's daml.yaml override-components
        # from the environment, and canton has no .envrc to supply it.
        container_env={"DAML_VERSION": CANTON_DAML_SDK_VERSION},
        build_args=_canton_build_args,
        # CantonExamples is one standalone package, and the rest of the repository holds
        # near-duplicate Iou models and Scala tests spelling out the blanked templates.
        repo_copy_scope_root=lambda target_file: find_daml_yaml_root(target_file),
        # canton's daml.yaml takes its version from `$DAML_VERSION`, which canton's .envrc
        # computes with a shell command. That cannot be read without running it.
        sdk_version=lambda test_file: CANTON_DAML_SDK_VERSION,
        # repo_canton.sh installs the SDK by its release tag and registers it with dpm.
        sdk_installed_by_repo_script=True,
        # The release tag to install and the SDK version to register it as.
        sdk_store_env={"CANTON_TAG": _CANTON_DAML_RELEASE_TAG, "CANTON_VER": CANTON_DAML_SDK_VERSION},
    )
)
