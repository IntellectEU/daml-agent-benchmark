"""Build and test a task's package, on the host or inside the eval container.

Three checks in order: a syntax check with the Daml linter, the package build, and the
test file's scripts. The per-repository hooks (extra build flags, dependency builds
before the build or the tests) come from the repository's handler.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from daml_agent_benchmark.repos.build import (
    build_daml_package,
    in_eval_container,
    lint_daml_files,
    repo_name_for_package_path,
    test_daml_package,
)
from daml_agent_benchmark.repos.sdk_version import find_daml_build_root, find_daml_yaml_root
from daml_agent_benchmark.repos.registry import RepoHandler, handler_for
from daml_agent_benchmark.tasklist_catalog import repo_root_for_path


@dataclass(frozen=True)
class CheckResult:
    syntax_pass: bool
    compile_pass: bool
    error: str
    # None = tests not run / no tests detected for this sample.
    tests_pass: bool | None = None
    test_results: dict[str, bool] | None = None

    @property
    def passed(self) -> bool:
        return self.syntax_pass and self.compile_pass


def _handler(path: str) -> RepoHandler:
    name = repo_name_for_package_path(path)
    if name is None:
        raise ValueError(f"{path} does not belong to a repository checkout or copy")
    return handler_for(name)


def command_prefix(path_in_repo: str) -> list[str]:
    """`direnv exec` for repositories with an .envrc, so builds see the repository's environment.

    Inside an eval container the .envrc exports are already injected as environment
    variables (direnv's allow database is host-side), so there the prefix is empty.
    """
    repo_root = repo_root_for_path(path_in_repo)
    if not os.path.exists(os.path.join(repo_root, ".envrc")):
        return []
    if in_eval_container():
        return []
    subprocess.run(["direnv", "allow"], cwd=repo_root)
    return ["direnv", "exec", repo_root]


def build(impl_file: str) -> subprocess.CompletedProcess[str]:
    """Build the package that contains the implementation file. Returns the build process."""
    print("Building project...")
    build_root = find_daml_build_root(impl_file)
    handler = _handler(impl_file)
    prefix = command_prefix(impl_file)
    if handler.pre_build is not None:
        failed = handler.pre_build(impl_file, build_root, prefix)
        if failed is not None and failed.returncode != 0:
            return failed
    extra_args = handler.build_args(impl_file, build_root) if handler.build_args is not None else []
    is_multi_package = os.path.exists(os.path.join(build_root, "multi-package.yaml"))
    return build_daml_package(build_root, command_prefix=prefix, extra_args=extra_args, build_all=is_multi_package)


def syntax_check(impl_files: list[str]) -> tuple[bool, str | None]:
    """Lint the implementation files. Only errors fail the check; warnings are printed.

    The first run for an SDK version can take minutes while the SDK is installed.
    """
    package_root = find_daml_yaml_root(impl_files[0])
    relative_impl_files = [os.path.relpath(p, package_root) for p in impl_files]
    prefix = command_prefix(impl_files[0])
    print(package_root, " ".join(prefix + ["<dpm-or-daml>", "damlc", "lint"] + relative_impl_files))
    lint = lint_daml_files(package_root, relative_impl_files, command_prefix=prefix)
    if "Severity: DsError" in lint.stderr:
        print("Daml syntax check failed.")
        print("Errors:", lint.stderr)
        return False, "Syntax Error:\n" + lint.stderr
    if lint.returncode != 0:
        print("Daml linter reported warnings or suggestions but no errors; treating as success.")
        print(lint.stderr)
    return True, None


def build_and_syntax_check(impl_files: list[str]) -> CheckResult:
    syntax_ok, syntax_error = syntax_check(impl_files)
    if not syntax_ok:
        return CheckResult(syntax_pass=False, compile_pass=False, error=syntax_error or "syntax error")
    build_process = build(impl_files[0])
    if build_process.returncode != 0:
        return CheckResult(syntax_pass=True, compile_pass=False, error="Build error:\n" + build_process.stderr)
    return CheckResult(syntax_pass=True, compile_pass=True, error="")


def test_results_path(test_file: str) -> Path:
    """Where `daml test` writes the JUnit report of a test file's run: in the package's build directory."""
    return find_daml_yaml_root(test_file) / ".daml" / "test-results.xml"


def run_tests(test_file: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run the test file's scripts with `daml test` (or `dpm damlc test`). Compiles the package too.

    The run writes a JUnit report to `test_results_path(test_file)`, which `read_test_results`
    reads. Raises subprocess.TimeoutExpired when `timeout` seconds pass.
    """
    test_dir = find_daml_yaml_root(test_file)
    prefix = command_prefix(test_file)
    handler = _handler(test_file)
    if handler.pre_test is not None:
        handler.pre_test(test_file, test_dir, prefix)
    report = test_results_path(test_file)
    report.unlink(missing_ok=True)
    return test_daml_package(
        test_dir, test_file, command_prefix=prefix, extra_args=["--junit", str(report)], timeout=timeout
    )


def read_test_results(test_file: str, run_passed: bool) -> dict[str, bool]:
    """Map each script of the last `run_tests` run to whether it passed, keyed `<file>:<script>`.

    The JUnit report lists every script, including one that failed while running, which
    SDK 1.x leaves out of its stdout. `daml test` writes no report when the test file does
    not compile, so a failed run without one ran no scripts. A passed run always writes one.
    Without it the harness itself went wrong, and this raises, which the runner records as
    an infrastructure failure.
    """
    report = test_results_path(test_file)
    if not report.exists():
        if run_passed:
            raise RuntimeError(f"daml test passed but wrote no test report at {report}")
        return {}
    return {
        f"{case.get('classname')}:{case.get('name')}": case.find("failure") is None and case.find("error") is None
        for case in ElementTree.parse(report).iter("testcase")
    }
