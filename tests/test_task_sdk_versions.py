"""Each shipped repository's tasks report an SDK version that the SDK store can install.

The checkouts here are small stand-ins in tmp_path with the same layout and daml.yaml
contents as the pinned commits. No SDK is downloaded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from daml_agent_benchmark import sdk_store
from daml_agent_benchmark.container import image
from daml_agent_benchmark.locations import configure, locations
from daml_agent_benchmark.repos.canton import _CANTON_DAML_RELEASE_TAG, CANTON_DAML_SDK_VERSION
from daml_agent_benchmark.repos.registry import sdk_store_env
from daml_agent_benchmark.repos.splice import SPLICE_DAML_RELEASE_TAG, SPLICE_DAML_SDK_VERSION, _use_installable_sdk
from daml_agent_benchmark.tasklist_catalog import task_sdk_version

TUTORIAL = "sdk/docs/source/sdk/tutorials/smart-contracts/daml/daml-intro-compose"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def sources(tmp_path: Path):
    """Stand-in checkouts of splice, daml and canton, with the harness pointed at them."""
    _write(tmp_path / "splice/daml/splice-wallet-test/daml.yaml", f"sdk-version: {SPLICE_DAML_SDK_VERSION}\nname: splice-wallet-test\n")
    _write(tmp_path / "splice/daml/splice-wallet/daml.yaml", f"sdk-version: {SPLICE_DAML_SDK_VERSION}\nname: splice-wallet\n")
    _write(tmp_path / "splice/daml/splice-wallet-test/daml/Test.daml", "module Test where\n")
    # The daml repository keeps only a template for each tutorial package.
    _write(tmp_path / f"daml/{TUTORIAL}/daml.yaml.template", "sdk-version: __VERSION__\nname: __PROJECT_NAME__\n")
    _write(tmp_path / f"daml/{TUTORIAL}/daml/Test/Intro/Asset.daml", "module Test.Intro.Asset where\n")
    # canton's daml.yaml takes its version from $DAML_VERSION, which its .envrc computes.
    examples = tmp_path / "canton/community/common/src/main/daml/CantonExamples"
    _write(examples / "daml.yaml", "override-components:\n  damlc:\n    version: $DAML_VERSION\nname: CantonExamples\n")
    _write(examples / "Paint.daml", "module Paint where\n")
    _write(tmp_path / "canton/.envrc", 'export DAML_VERSION=$(grep "val version" DamlVersions.scala)\n')
    previous = locations.sources_root
    configure(sources_root=tmp_path)
    yield tmp_path
    configure(sources_root=previous)


def test_splice_tasks_report_the_release_tag(sources: Path) -> None:
    assert task_sdk_version(str(sources / "splice/daml/splice-wallet-test/daml/Test.daml")) == SPLICE_DAML_RELEASE_TAG


def test_daml_tutorial_tasks_report_the_tutorial_sdk(sources: Path) -> None:
    assert task_sdk_version(str(sources / f"daml/{TUTORIAL}/daml/Test/Intro/Asset.daml")) == "3.4.9"


def test_canton_tasks_report_the_canton_snapshot(sources: Path) -> None:
    test_file = sources / "canton/community/common/src/main/daml/CantonExamples/Paint.daml"
    assert task_sdk_version(str(test_file)) == CANTON_DAML_SDK_VERSION


def test_the_store_installs_the_splice_tag_and_the_tutorial_sdk(sources: Path, tmp_path: Path, monkeypatch) -> None:
    tasks = {
        str(sources / "splice/daml/splice-wallet-test/daml/Test.daml"): [],
        str(sources / f"daml/{TUTORIAL}/daml/Test/Intro/Asset.daml"): [],
        # canton's SDK comes from repo_canton.sh, so it is not in the list.
        str(sources / "canton/community/common/src/main/daml/CantonExamples/Paint.daml"): [],
    }
    versions = image._collect_required_daml_versions(tasks)
    assert versions == [SPLICE_DAML_RELEASE_TAG, "3.4.9"]

    commands: list[list[str]] = []
    monkeypatch.setattr(sdk_store, "SDK_STORE_ROOT", tmp_path / "store")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: commands.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    sdk_store.ensure_sdk_store(versions, image="daml-agent-eval", env=sdk_store_env(), answer_files=[])
    assert commands[0][-2:] == [SPLICE_DAML_RELEASE_TAG, "3.4.9"]
    # repo_canton.sh gets canton's release tag and SDK version from the canton handler.
    command = " ".join(commands[0])
    assert f"-e CANTON_TAG={_CANTON_DAML_RELEASE_TAG}" in command
    assert f"-e CANTON_VER={CANTON_DAML_SDK_VERSION}" in command


def test_the_splice_copy_names_the_release_tag(sources: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    subprocess.run(["cp", "-R", str(sources / "splice"), str(copy)], check=True)
    other = _write(copy / "daml/other/daml.yaml", "sdk-version: 3.4.9\nname: other\n")

    _use_installable_sdk(copy)

    for package in ("splice-wallet-test", "splice-wallet"):
        text = (copy / "daml" / package / "daml.yaml").read_text(encoding="utf-8")
        assert text.startswith(f"sdk-version: {SPLICE_DAML_RELEASE_TAG}\n")
    assert other.read_text(encoding="utf-8") == "sdk-version: 3.4.9\nname: other\n"
