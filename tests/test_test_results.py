"""Reading `daml test` results: which scripts passed.

The JUnit report is what SDK 1.12 wrote in the eval container for a passing script, a
failed assertion and a missing authorization. SDK 1.x prints only the passing script on
stdout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daml_agent_benchmark import grading

JUNIT = (
    "<?xml version='1.0' ?><testsuites errors=\"0\" failures=\"2\" tests=\"3\">"
    "<testsuite name=\"daml/Main.daml\" tests=\"3\">"
    "<testcase name=\"test_assert\" classname=\"daml/Main.daml\"><failure>Scenario execution failed: Aborted:  boom</failure></testcase>"
    "<testcase name=\"test_auth\" classname=\"daml/Main.daml\"><failure>Scenario execution failed on commit at Main:22:3: "
    "0: create of Main:T failed due to a missing authorization from &#39;Alice&#39;</failure></testcase>"
    "<testcase name=\"test_pass\" classname=\"daml/Main.daml\" /></testsuite></testsuites>"
)


def test_every_script_is_read_from_the_junit_report(tmp_path: Path) -> None:
    (tmp_path / "daml.yaml").write_text("sdk-version: 1.12.0\n", encoding="utf-8")
    test_file = str(tmp_path / "daml" / "Main.daml")
    report = grading.test_results_path(test_file)
    report.parent.mkdir(parents=True)
    report.write_text(JUNIT, encoding="utf-8")

    assert grading.read_test_results(test_file, run_passed=False) == {
        "daml/Main.daml:test_assert": False,
        "daml/Main.daml:test_auth": False,
        "daml/Main.daml:test_pass": True,
    }


def test_a_failed_run_without_a_report_ran_no_scripts(tmp_path: Path) -> None:
    # `daml test` writes no report when the test file does not compile.
    (tmp_path / "daml.yaml").write_text("sdk-version: 1.12.0\n", encoding="utf-8")

    assert grading.read_test_results(str(tmp_path / "daml" / "Main.daml"), run_passed=False) == {}


def test_a_passed_run_without_a_report_is_a_harness_failure(tmp_path: Path) -> None:
    (tmp_path / "daml.yaml").write_text("sdk-version: 1.12.0\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="wrote no test report"):
        grading.read_test_results(str(tmp_path / "daml" / "Main.daml"), run_passed=True)
