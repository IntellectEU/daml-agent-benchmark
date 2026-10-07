"""Leaving a package out of a task's repository copy."""

import pytest

from daml_agent_benchmark.task_run.repo_copy import hide_in_repo_copy

MANIFEST = "# header\n\npackages:\n- ./asset\n- ./asset-tests\n- ./multi-trade-tests\n"


def test_a_hidden_package_goes_and_leaves_the_multi_package_manifest(tmp_path) -> None:
    tutorial = tmp_path / "docs" / "intro"
    for package in ("asset", "asset-tests", "multi-trade-tests"):
        (tutorial / package / "daml").mkdir(parents=True)
    (tutorial / "multi-trade-tests" / "daml" / "TradeSetup.daml").write_text("module TradeSetup where\n")
    (tutorial / "multi-package.yaml").write_text(MANIFEST)

    hide_in_repo_copy(str(tmp_path), ["docs/intro/multi-trade-tests"])

    assert not (tutorial / "multi-trade-tests").exists()
    assert (tutorial / "asset-tests").is_dir()
    assert (tutorial / "multi-package.yaml").read_text() == "# header\n\npackages:\n- ./asset\n- ./asset-tests\n"


def test_a_hidden_path_that_is_not_there_is_an_error(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        hide_in_repo_copy(str(tmp_path), ["docs/intro/missing.daml"])
