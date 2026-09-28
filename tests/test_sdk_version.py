from pathlib import Path

from daml_agent_benchmark.repos.sdk_version import resolve_daml_sdk_version, templated_version_variable


def _write_package(root: Path, daml_yaml: str) -> Path:
    pkg = root / "pkg"
    (pkg / "daml").mkdir(parents=True)
    (pkg / "daml.yaml").write_text(daml_yaml, encoding="utf-8")
    file_path = pkg / "daml" / "A.daml"
    file_path.write_text("module A where\n", encoding="utf-8")
    return file_path


def test_resolves_literal_sdk_version(tmp_path: Path):
    file_path = _write_package(
        tmp_path,
        """
name: pkg
sdk-version: 3.4.11
""",
    )

    resolution = resolve_daml_sdk_version(file_path)

    assert resolution.version == "3.4.11"
    assert resolution.raw_version == "3.4.11"
    assert resolution.daml_yaml_path == tmp_path / "pkg" / "daml.yaml"


def test_resolves_canton_style_damlc_override_from_makefile(tmp_path: Path):
    (tmp_path / "multi-package.yaml").write_text("packages:\n- pkg\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("DAML_VERSION := 3.5.0-snapshot.20260403.0\n", encoding="utf-8")
    file_path = _write_package(
        tmp_path,
        """
name: pkg
override-components:
  damlc:
    version: $DAML_VERSION
""",
    )

    resolution = resolve_daml_sdk_version(file_path, repo_root=tmp_path)

    assert resolution.version == "3.5.0-snapshot.20260403.0"
    assert resolution.raw_version == "$DAML_VERSION"


def test_unresolved_template_returns_unknown(tmp_path: Path):
    file_path = _write_package(
        tmp_path,
        """
name: pkg
sdk-version: __VERSION__
""",
    )

    resolution = resolve_daml_sdk_version(file_path, repo_root=tmp_path)

    assert resolution.version is None
    assert resolution.raw_version == "__VERSION__"
    assert resolution.error == "Variable DAML_VERSION not resolved"


def test_templated_version_variable_forms():
    assert templated_version_variable("$DAML_VERSION") == "DAML_VERSION"
    assert templated_version_variable("${SDK_VERSION}") == "SDK_VERSION"
    assert templated_version_variable("__VERSION__") == "DAML_VERSION"
    assert templated_version_variable("3.4.11") is None
