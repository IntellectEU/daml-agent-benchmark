"""Building each mutant of a task and running a test file on it.

Two callers use it. The authoring validator runs the ground-truth test file on each mutant, to
check that the mutation is caught. Evaluation runs the agent's test file on each mutant, to see
which mutants its tests catch.

The repository copy is prepared once by the caller. Each mutant then gets a clone of that copy,
with the patch applied, which is graded and deleted.

The clone is there because patching the one copy in place, mutant after mutant, gives wrong
results. The build runs in the container, which reads the host's files through Docker Desktop's
file sharing, and that sharing remembers a file's size and existence for a moment. A file
patched right after the previous mutant's build can then read, inside the container, as missing
or cut off at its old length, so the mutant fails to compile for a reason that has nothing to
do with the patch. A clone's files sit at paths the container has not read before, so nothing
about them is remembered.

The clone also carries the correct code's build outputs, so Daml's own cache rebuilds only what
the patch changed. On macOS it is made with one clonefile(2) call, a copy-on-write clone that
takes a fraction of a second where a file-by-file copy of a large repository takes seconds.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import time
from xml.etree import ElementTree

from daml_agent_benchmark import grading
from daml_agent_benchmark.mutations.model import Mutation, apply_patch
from daml_agent_benchmark.records import Grade
from daml_agent_benchmark.task_run.ground_truth import grade_in_environment


@dataclass(frozen=True)
class MutantRun:
    """What building one mutant and running the test file on it produced.

    `reported_failures` is what the test run itself reported: every script it marks as failed,
    whatever the cause. Which of those failures the mutation caused is for the caller to decide,
    against how the same scripts did on the correct code (see the validator's `newly_failing`).
    """

    mutation: Mutation
    grade: Grade
    seconds: float
    reported_failures: dict[str, str]  # each script the report marks as failed, keyed as grading keys them, with its message


def _clone_tree(source: str, destination: str) -> None:
    """Copy the directory tree at `source` to the new path `destination`, keeping symbolic links
    and timestamps.

    On macOS one clonefile(2) call clones the whole tree: every file gets a new inode that
    shares the source's blocks until one of them is written, and symbolic links inside the
    tree are cloned as links. On Linux `cp --reflink=auto` does
    the same where the filesystem supports it, and copies otherwise.
    """
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.clonefile(os.fsencode(source), os.fsencode(destination), 0) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno), destination)
    elif sys.platform.startswith("linux"):
        subprocess.run(["cp", "-a", "--reflink=auto", source, destination], check=True)
    else:
        shutil.copytree(source, destination, symlinks=True)


def grade_timed(test_file: str, impl_files: list[str], lint_files: list[str]) -> tuple[Grade, float]:
    # `daml test` writes no report when the test file does not compile, so a report left
    # by the previous grade would be read as this one's.
    grading.test_results_path(test_file).unlink(missing_ok=True)
    started = time()
    grade = grade_in_environment(test_file, impl_files, lint_files)
    return grade, round(time() - started, 1)


def reported_failures(test_file: str) -> dict[str, str]:
    """The failure text of every failed script in the last `daml test` report, keyed as grading keys them."""
    report = grading.test_results_path(test_file)
    if not report.exists():
        return {}
    messages = {}
    for case in ElementTree.parse(report).iter("testcase"):
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is not None:
            key = f"{case.get('classname')}:{case.get('name')}"
            messages[key] = (failure.get("message") or "") + ("\n" + failure.text if failure.text else "")
    return messages


def run_mutants(
    copy_dir: str, test_rel: str, impl_rel: list[str], mutations: list[Mutation]
) -> list[MutantRun]:
    """Build every mutant from `copy_dir` and run the test file at `test_rel` on it, in order.

    The mutations must be usable: each patch changes only implementation files and applies to
    them. One that is not raises, since checking that is authoring's job, not grading's.
    """
    originals = {rel: Path(copy_dir, rel).read_bytes() for rel in impl_rel}
    runs = []
    for index, mutation in enumerate(mutations):
        # A suffix on the copy's name keeps the repository name the harness reads from it.
        mutant_dir = f"{copy_dir}m{index}"
        try:
            runs.append(_run_one(mutation, copy_dir, mutant_dir, test_rel, impl_rel, originals))
        finally:
            shutil.rmtree(mutant_dir, ignore_errors=True)
    return runs


def _run_one(
    mutation: Mutation,
    copy_dir: str,
    mutant_dir: str,
    test_rel: str,
    impl_rel: list[str],
    originals: dict[str, bytes],
) -> MutantRun:
    stray = [p for p in mutation.patched_paths() if p not in impl_rel]
    if stray:
        raise ValueError(f"mutation {mutation.id} changes files that are not implementation files: {stray}")
    patched, apply_output = apply_patch(mutation.patch, originals)
    if patched is None:
        raise ValueError(f"mutation {mutation.id} does not apply: {apply_output}")
    unchanged = [p for p in mutation.patched_paths() if patched[p] == originals[p]]
    if unchanged:
        raise RuntimeError(f"git apply succeeded but left {unchanged} unchanged: {apply_output}")
    _clone_tree(copy_dir, mutant_dir)
    for rel, content in patched.items():
        Path(mutant_dir, rel).write_bytes(content)
    mutant_test = os.path.join(mutant_dir, test_rel)
    mutant_impls = [os.path.join(mutant_dir, p) for p in impl_rel]
    # The lint checks what the patch changed.
    grade, seconds = grade_timed(mutant_test, mutant_impls, mutant_impls)
    return MutantRun(mutation, grade, seconds, reported_failures(mutant_test))
