"""Grading a copy, and the ground-truth control that validates the task itself."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import replace
from pathlib import Path
from time import time

from daml_agent_benchmark import grading
from daml_agent_benchmark.config import ExperimentConfig
from daml_agent_benchmark.constants import CONTAINER_DOCKER_BIN, CONTAINER_IMAGE
from daml_agent_benchmark.container.image import container_image_id
from daml_agent_benchmark.records import Finding, Grade, GroundTruthControl, RepoCopyIntegrity, TaskFlag, now_utc
from daml_agent_benchmark.repo_copy_integrity import describe_integrity_failure, scan_repo_copy_integrity
from daml_agent_benchmark.run_files import load_json_dict, write_json_atomic
from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.task_run.env import task_container_env
from daml_agent_benchmark.tasklist_catalog import repo_root_for_path
from daml_agent_benchmark.workspace_audit import hash_tree, no_ignore, remove_added_files


def grade_task(test_file_path: str, impl_files: list[str], lint_files: list[str]) -> Grade:
    """The three-stage verdict on what the agent wrote.

    1. Syntax check `lint_files`; when the list is empty, the stage passes without a check
       (test files are not linted, see `TestGenerationKind.grade`)
    2. Build the package holding the implementation files
    3. Run the test file

    Each stage runs only if the one before it passed, so a compile error leaves the test
    stage unrun rather than failed. An unrun stage is recorded as False, so a reader needs
    the stage before it to tell "failed" from "never ran".
    """
    syntax_passed, syntax_error = grading.syntax_check(lint_files) if lint_files else (True, None)

    compile_passed = False
    compile_error = None
    if syntax_passed:
        build_process = grading.build(impl_files[0])
        compile_passed = build_process.returncode == 0
        if not compile_passed:
            compile_error = build_process.stderr
    else:
        compile_error = None

    tests_passed = False
    tests2success = {}
    tests_error = None
    if compile_passed:
        test_process = grading.run_tests(test_file_path)
        tests_passed = test_process.returncode == 0
        tests2success = grading.read_test_results(test_file_path, tests_passed)
        if not tests_passed:
            tests_error = test_process.stderr

    return Grade(
        syntax_passed=syntax_passed,
        compile_passed=compile_passed,
        tests_passed=tests_passed,
        test_results=tests2success,
        syntax_error=syntax_error,
        compile_error=compile_error,
        tests_error=tests_error,
    )


def grade_in_environment(test_file_path: str, impl_files: list[str], lint_files: list[str]) -> Grade:
    """Grade inside the eval container.

    Old-SDK repositories do not build on a developer machine, so the build and the tests
    always run in the container. The repository root is mounted at the identical path it
    has on the host, so paths in the record mean the same thing either way.
    """
    from daml_agent_benchmark.repos.build import eval_in_container as run_eval_in_container

    mount_root = repo_root_for_path(impl_files[0])
    with run_eval_in_container(
        mount_root,
        CONTAINER_IMAGE,
        docker_bin=CONTAINER_DOCKER_BIN,
        env=task_container_env(mount_root),
    ):
        return grade_task(test_file_path, impl_files, lint_files)


_GROUND_TRUTH_CONTROL_CACHE_LOCK = threading.Lock()
# Part of the control cache key. Raised whenever grading changes.
GRADING_VERSION = 1


def _ground_truth_control_cache_path() -> Path:
    return locations.logs_dir / "ground_truth_controls.json"


def run_ground_truth_control(
    *,
    task_id: str,
    repo_copy_test_file: str,
    repo_copy_impl_files: list[str],
    repo_copy_digest: str,
    log_prefix: str,
) -> GroundTruthControl:
    """Build and test the copy with the ground-truth implementation still in place.

    A task whose ground truth does not pass in this exact environment cannot say
    anything about the agent, so the caller records it as an infrastructure error
    and skips the agent run. Results are cached by copy's content, eval image and
    grading version, so repeated runs over an unchanged benchmark pay the build once.
    """
    image_id = container_image_id(CONTAINER_IMAGE)
    cache_key = hashlib.sha256(
        f"{task_id}\0{repo_copy_digest}\0{image_id}\0{GRADING_VERSION}".encode("utf-8")
    ).hexdigest()
    cache_path = _ground_truth_control_cache_path()
    with _GROUND_TRUTH_CONTROL_CACHE_LOCK:
        cache = load_json_dict(cache_path)
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        print(f"{log_prefix}ground-truth control: cached pass", flush=True)
        return replace(GroundTruthControl.from_record(cached), cached=True)

    print(f"{log_prefix}ground-truth control: building and testing the unblanked copy", flush=True)
    started = time()
    grade = grade_in_environment(repo_copy_test_file, repo_copy_impl_files, repo_copy_impl_files)

    control = GroundTruthControl(
        grade=grade,
        cached=False,
        image_id=image_id,
        evaluated_at_utc=now_utc(),
        build_outputs_removed=None,
    )
    # Only passes are cached: a failure may be transient (Docker hiccup, killed run)
    # and must be re-established, not replayed, on the next run.
    if control.passed():
        with _GROUND_TRUTH_CONTROL_CACHE_LOCK:
            cache = load_json_dict(cache_path)
            cache[cache_key] = control.to_record()
            write_json_atomic(cache_path, cache)
    print(
        f"{log_prefix}ground-truth control: {'pass' if control.passed() else 'FAIL'} in {round(time() - started, 1)}s",
        flush=True,
    )
    return control


def verify_copy(
    config: ExperimentConfig,
    *,
    task_id: str,
    repo_copy_dir: str,
    repo_copy_test_file: str,
    repo_copy_impl_files: list[str],
    repo_copy_answer_files: list[str],
    pruned_archives: list[dict],
    log_prefix: str,
) -> tuple[RepoCopyIntegrity, GroundTruthControl | None, Finding | None]:
    """Whether this copy is fit to measure an agent on, checked before the agent sees it.

    Two things have to hold. The copy must not contain the answer anywhere but in the files
    the agent is asked to write, `repo_copy_answer_files`, or the measurement is meaningless.
    And the task must build and pass its own tests in this environment, or a failure says
    nothing about the agent.

    The scan runs while the ground truth is still in the copy, which is the only moment it
    can. The control build then writes DARs and interface files that hold the compiled
    ground truth, so everything it added is removed again and the copy is re-scanned.

    A finding means the caller must not run the agent. The integrity and the control come
    back either way: they belong in the task's record whether or not it went ahead.
    """

    def scan_copy() -> tuple[dict, RepoCopyIntegrity]:
        """Scan the copy for the answer, returning the raw report and the record built from it.

        The report carries the detail a failure message needs; the record is what the task
        keeps. Both are wanted, and the scan is expensive, so it runs once for the two.
        """
        scan = scan_repo_copy_integrity(repo_copy_dir, repo_copy_answer_files)
        return scan, RepoCopyIntegrity.from_scan(scan, pruned_archives)

    scan, integrity = scan_copy()
    print(
        f"{log_prefix}repository-copy integrity: {'ok' if integrity.ok else 'FAILED'} "
        f"({integrity.files_scanned} files, {integrity.elapsed_seconds}s)",
        flush=True,
    )
    if not integrity.ok:
        detail = f"Repository-copy integrity check failed: {describe_integrity_failure(scan)}"
        return integrity, None, Finding(flag=TaskFlag.REPO_COPY_INTEGRITY_FAILURE, detail=detail)

    if not config.ground_truth_control:
        return integrity, None, None

    pre_control_hashes = hash_tree(repo_copy_dir, ignore=no_ignore)
    control = run_ground_truth_control(
        task_id=task_id,
        repo_copy_test_file=repo_copy_test_file,
        repo_copy_impl_files=repo_copy_impl_files,
        repo_copy_digest=integrity.repo_copy_digest,
        log_prefix=log_prefix,
    )
    cleanup = remove_added_files(repo_copy_dir, pre_control_hashes)
    control = replace(control, build_outputs_removed=len(cleanup["removed"]))
    if not control.passed():
        detail = (
            "Ground-truth control failed: the unblanked copy does not build and pass its tests in this "
            "environment. See ground_truth_control."
        )
        return integrity, control, Finding(flag=TaskFlag.GROUND_TRUTH_CONTROL_FAILURE, detail=detail)
    if cleanup["modified"]:
        detail = f"Ground-truth control modified files that were already in the copy: {cleanup['modified'][:20]}"
        return integrity, control, Finding(flag=TaskFlag.GROUND_TRUTH_CONTROL_FAILURE, detail=detail)

    scan, integrity = scan_copy()
    if not integrity.ok:
        detail = (
            f"Repository-copy integrity check failed after the ground-truth control: "
            f"{describe_integrity_failure(scan)}"
        )
        return integrity, control, Finding(flag=TaskFlag.REPO_COPY_INTEGRITY_FAILURE, detail=detail)
    return integrity, control, None
