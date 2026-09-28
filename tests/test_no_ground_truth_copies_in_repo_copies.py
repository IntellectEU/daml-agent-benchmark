"""Guard against the eval grading ground truth instead of the agent's code.

Failure mode this protects against (it happened): a repository handler materialized a
COPY of the implementation source inside the test package (recreating dropped
symlinks as real files). The copy both leaked the ground truth to agents and —
worse — was what `daml test` compiled, so tests graded the ground truth no matter
what the agent wrote. GT/empty validation cannot catch this: ground truth equals
the copy, and empty impls already fail at the earlier lint stage.

The generic detectable signature of the whole class: when the copy is prepared, some
file OTHER than the target itself carries the target's content. The benchmark
runner performs this scan on every task of every run (`scan_repo_copy_integrity`);
this test runs the same scan against the real source repositories so a handler
change is caught before a paid run is.

Prep-only (no eval containers), but repo handlers run (some build DARs on the
host), so the full sweep takes minutes: marked slow, and a fast per-repository
representative subset runs by default.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from daml_agent_benchmark.task_run.repo_copy import prepare_task_repo_copy
from daml_agent_benchmark.tasklist_catalog import repo_root_for_path, impl_files_by_test_file
from daml_agent_benchmark.repo_copy_integrity import describe_integrity_failure, scan_repo_copy_integrity
from daml_agent_benchmark.sdk_store import SDK_STORE_ROOT, find_answers_in_store_archives
from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.tasklist_catalog import repo_name_for_path


def _tasks_by_repo() -> dict[str, list[tuple[str, list[str]]]]:
    by_repo: dict[str, list[tuple[str, list[str]]]] = {}
    for test_file, impl_files in impl_files_by_test_file().items():
        repository = repo_name_for_path(test_file)
        by_repo.setdefault(repository, []).append((test_file, impl_files))
    return by_repo


def _representative_tasks() -> list[tuple[str, list[str]]]:
    """One task per repository — the copy hazard is a property of each repo's handler."""
    return [tasks[0] for tasks in _tasks_by_repo().values()]


def _assert_repo_copy_integrity(test_file: str, impl_files: list[str]) -> None:
    repo_root = repo_root_for_path(test_file)
    locations.repo_copies_dir.mkdir(parents=True, exist_ok=True)
    run_dir = tempfile.mkdtemp(prefix="gt_copy_probe_", dir=str(locations.repo_copies_dir))
    try:
        repo_copy = Path(prepare_task_repo_copy(repo_root, impl_files, run_dir, test_file_path=test_file)[0])
        repo_copy_impls = [str(repo_copy / os.path.relpath(impl, repo_root)) for impl in impl_files]

        report = scan_repo_copy_integrity(repo_copy, repo_copy_impls)

        assert report["ok"], (
            f"The copy of {os.path.relpath(test_file, repo_root)} failed the integrity scan: "
            f"{describe_integrity_failure(report)}. Agents can read leaked content, and the eval's test "
            "stage may compile a copy instead of the agent's code."
        )
    finally:
        # A copy holds a whole source repository; the all-tasks sweep would
        # otherwise leave gigabytes behind, and this test is meant to run routinely.
        shutil.rmtree(run_dir, ignore_errors=True)


@pytest.mark.parametrize(
    "test_file,impl_files",
    _representative_tasks(),
    ids=lambda value: os.path.relpath(value, locations.sources_root) if isinstance(value, str) else None,
)
def test_no_ground_truth_copies_representative(test_file: str, impl_files: list[str]) -> None:
    if not Path(test_file).exists():
        pytest.skip("source repository not present on this machine")
    _assert_repo_copy_integrity(test_file, impl_files)


@pytest.mark.slow
@pytest.mark.parametrize(
    "test_file,impl_files",
    [task for tasks in _tasks_by_repo().values() for task in tasks],
    ids=lambda value: os.path.relpath(value, locations.sources_root) if isinstance(value, str) else None,
)
def test_no_ground_truth_copies_all_tasks(test_file: str, impl_files: list[str]) -> None:
    if not Path(test_file).exists():
        pytest.skip("source repository not present on this machine")
    _assert_repo_copy_integrity(test_file, impl_files)


@pytest.mark.slow
def test_sdk_store_holds_no_task_answer() -> None:
    """The SDK store is mounted read-only into every agent container, outside the copy
    the per-task scan covers. The SDK's own tutorial code is among the tasks, and every
    SDK ships it as `daml new` examples, and canton's jars bundle the canton tasks' answers.
    Those are removed from the store, and this checks that no task's answer is left in it,
    in an archive or as a plain file."""
    if not SDK_STORE_ROOT.is_dir():
        pytest.skip("SDK store not present on this machine")
    targets = [impl for impls in impl_files_by_test_file().values() for impl in impls]
    found = find_answers_in_store_archives(targets)
    assert found == [], f"task answers found in the SDK store: {[(str(f['path']), f['entry']) for f in found][:10]}"
