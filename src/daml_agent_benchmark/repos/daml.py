"""daml-repo-specific copy fixes: codegen fixtures and SDK tutorial packages."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from daml_agent_benchmark.repos.common import set_daml_sdk_version
from daml_agent_benchmark.repos.registry import RepoHandler, register


_DAML_SANDBOX_SDK_VERSION = "3.4.11"

# SDK used to render the tutorials' daml.yaml.template placeholders. The checkout has
# only the templates, so the handler also reports this version for tutorial tasks. That
# is how the SDK store knows to install it.
_DAML_TUTORIAL_SDK_VERSION = "3.4.9"

_TUTORIALS_SUBDIR = Path("sdk/docs/source/sdk/tutorials/smart-contracts/daml")
_DIST_DATA_DEP_RE = re.compile(r"\.\./([^/\s]+)/\.daml/dist/[^\s]+\.dar")


def _write_codegen_fixture_yaml(package_root: Path, *, name: str, source: str, version: str) -> None:
    """Write a minimal `daml.yaml` for a Bazel-only codegen fixture package."""
    (package_root / "daml.yaml").write_text(
        "\n".join(
            [
                f"sdk-version: {_DAML_SANDBOX_SDK_VERSION}",
                f"name: {name}",
                f"source: {source}",
                f"version: {version}",
                "dependencies:",
                "  - daml-prim",
                "  - daml-stdlib",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _build_codegen_fixture_dependency(repo_copy: Path, package_root: Path, dar_name: str) -> None:
    """Build one synthetic codegen fixture package and copy its DAR to pkg-root."""
    subprocess.run(
        ["daml", "build"],
        cwd=package_root,
        capture_output=True,
        text=True,
        check=True,
    )
    dist_dar = package_root / ".daml" / "dist" / dar_name
    if not dist_dar.exists():
        raise FileNotFoundError(dist_dar)


def _render_tutorial_yaml(package_root: Path) -> None:
    """Materialize daml.yaml from daml.yaml.template (repo does this at release time)."""
    template = package_root / "daml.yaml.template"
    if not template.exists() or (package_root / "daml.yaml").exists():
        return
    rendered = (
        template.read_text(encoding="utf-8")
        .replace("__VERSION__", _DAML_TUTORIAL_SDK_VERSION)
        .replace("__PROJECT_NAME__", package_root.name)
    )
    (package_root / "daml.yaml").write_text(rendered, encoding="utf-8")


def _tutorial_package_dirs(tutorial_root: Path) -> list[Path]:
    """Package roots of a tutorial: itself and/or its direct subpackages."""
    candidates = [tutorial_root, *sorted(p.parent for p in tutorial_root.glob("*/daml.yaml.template"))]
    return [p for p in candidates if (p / "daml.yaml.template").exists() or (p / "daml.yaml").exists()]


def _dist_dep_names(package_root: Path) -> list[str]:
    """Sibling package dir names referenced via ../<dir>/.daml/dist data-dependencies."""
    yaml_path = package_root / "daml.yaml"
    if not yaml_path.exists():
        return []
    return _DIST_DATA_DEP_RE.findall(yaml_path.read_text(encoding="utf-8"))


def _daml_tutorial_root(target_file: str) -> str | None:
    """Return the daml-intro-* tutorial directory containing a daml-repository target, if any.

    Used as the copy scope root for tutorial tasks: the SDK's tutorials duplicate
    each other (compose ≡ intro-test/asset verbatim, multi-trade ≡ intro-9), so a task
    copy exposes only the target's own tutorial directory.
    """
    abs_path = os.path.abspath(target_file)
    parts = abs_path.split(os.sep)
    for i, part in enumerate(parts):
        if part.startswith("daml-intro-"):
            return os.sep.join(parts[: i + 1])
    return None


def _daml_sdk_version(test_file: str) -> str | None:
    """The tutorials' SDK version for a tutorial task, else None to read daml.yaml."""
    return _DAML_TUTORIAL_SDK_VERSION if _daml_tutorial_root(test_file) is not None else None


def _restore_tutorial_packages(repo_copy: Path, target_files: list[Path], repo_root: Path) -> None:
    """Prepare the daml-intro tutorial containing the targets for standalone builds.

    Renders daml.yaml from each package's daml.yaml.template and prebuilds the
    dependency DARs (../<pkg>/.daml/dist/...) that the test and target packages
    data-depend on — EXCEPT any package containing a target file: its DAR embeds
    the solution and must be (re)built from (agent-written) sources by the eval.
    """
    tutorials_root = repo_copy / _TUTORIALS_SUBDIR
    if not tutorials_root.is_dir():
        return

    def rel_to_tutorials(path: Path) -> Path | None:
        """Path relative to the tutorials dir, whether rooted in repo_root or copy."""
        resolved = Path(path).resolve()
        for root in (Path(repo_root).resolve(), Path(repo_copy).resolve()):
            try:
                return resolved.relative_to(root / _TUTORIALS_SUBDIR)
            except ValueError:
                continue
        return None

    target_rels = [rel for rel in (rel_to_tutorials(t) for t in target_files) if rel is not None]
    for tutorial_name in sorted({rel.parts[0] for rel in target_rels}):
        tutorial_root = tutorials_root / tutorial_name
        packages = _tutorial_package_dirs(tutorial_root)
        for package_root in packages:
            _render_tutorial_yaml(package_root)
        target_package_roots = {
            package_root
            for package_root in packages
            for rel in target_rels
            if (tutorials_root / rel).is_relative_to(package_root)
        }
        # Build dependency packages in topological order (deps before dependents),
        # skipping target packages and packages that (transitively) depend on one —
        # those must compile from the copy's (blanked/agent) sources at eval time.
        built: set[Path] = set()
        remaining = [p for p in packages if p not in target_package_roots]
        for _ in range(len(remaining) + 1):
            for package_root in list(remaining):
                dep_dirs = {package_root.parent / name for name in _dist_dep_names(package_root)}
                if any(dep in target_package_roots for dep in dep_dirs):
                    remaining.remove(package_root)  # depends on a target package: eval's job
                    continue
                if all(dep in built or dep not in packages for dep in dep_dirs):
                    proc = subprocess.run(
                        ["daml", "build"], cwd=package_root, capture_output=True, text=True, check=False
                    )
                    if proc.returncode != 0:
                        raise RuntimeError(
                            f"Tutorial dep build failed in {package_root}:\n{proc.stderr or proc.stdout}"
                        )
                    built.add(package_root)
                    remaining.remove(package_root)


def _restore_deps_for_daml(repo_root: Path, repo_copy: Path, target_files: list[Path]) -> None:
    """Patch copied `daml` repo fixtures for standalone validator builds."""
    _restore_tutorial_packages(repo_copy, target_files, repo_root)
    pkg_root = repo_copy / "sdk" / "language-support" / "java" / "codegen" / "src" / "it" / "daml" / "pkg-root"
    pkg_root_yaml = pkg_root / "daml.yaml"
    if not pkg_root_yaml.exists():
        return

    set_daml_sdk_version(pkg_root_yaml, _DAML_SANDBOX_SDK_VERSION) # Hardcoding instead of using daml.bzl, as building via bazel would maybe also build the package containing the target files.
    yaml_text = pkg_root_yaml.read_text(encoding="utf-8").replace("source: src/it/daml/pkg-root/daml", "source: daml")
    pkg_root_yaml.write_text(yaml_text, encoding="utf-8")

    pkg1_root = repo_copy / "sdk" / "language-support" / "java" / "codegen" / "src" / "it" / "daml" / "Pkg1.0"
    pkg2_root = repo_copy / "sdk" / "language-support" / "java" / "codegen" / "src" / "it" / "daml" / "Pkg2.0"
    _write_codegen_fixture_yaml(pkg1_root, name="pkg", source=".", version="1.0.0")
    _write_codegen_fixture_yaml(pkg2_root, name="pkg", source=".", version="2.0.0")
    _build_codegen_fixture_dependency(repo_copy, pkg1_root, "pkg-1.0.0.dar")
    _build_codegen_fixture_dependency(repo_copy, pkg2_root, "pkg-2.0.0.dar")
    (pkg_root / "pkg1.dar").write_bytes((pkg1_root / ".daml" / "dist" / "pkg-1.0.0.dar").read_bytes())
    (pkg_root / "pkg2.dar").write_bytes((pkg2_root / ".daml" / "dist" / "pkg-2.0.0.dar").read_bytes())


register(
    RepoHandler(
        name="daml",
        restore_deps=_restore_deps_for_daml,
        restore_in_repo_copy=True,
        # Scoped to the tutorial directory rather than the package, so multi-package tutorials
        # keep all their packages; a non-tutorial task keeps the full tree.
        repo_copy_scope_root=_daml_tutorial_root,
        # sdk/docs/sharable is a generated mirror of docs/source with an exact copy of every
        # tutorial target file.
        excluded_subpaths=("sdk/docs/sharable",),
        sdk_version=_daml_sdk_version,
    )
)
