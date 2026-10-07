"""A task's mutations as data: the `Mutation` record, the loader for its YAML file, applying a
patch, and the validator's report on them.

Nothing here builds or runs anything, so the task list can use it to tell which tasks have
mutations, and the dashboard can show them. Building and testing the mutants is
`task_run/mutant_grading.py`; validating them is `validate.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path

import yaml

from daml_agent_benchmark.locations import locations, task_file_name


class MutationSource(StrEnum):
    LLM = "llm"  # written by a model, aimed at one ground-truth script
    REAL_BUG = "real-bug"  # a fix from the repository's history, undone


class MutationKind(StrEnum):
    """What sort of mistake a mutation makes. A descriptive label: the kinds overlap, so it never feeds a score."""

    AUTHORIZATION = "authorization"  # a signatory, observer or controller missing or wrong
    ARCHIVAL = "archival"  # a choice consuming when it should not be, or the reverse; a missing `archive`
    VALIDATION = "validation"  # an `ensure`, `assert` or `assertMsg` weakened, dropped or inverted
    ARITHMETIC = "arithmetic"  # a wrong operator, an off-by-one, a wrong constant
    BRANCH = "branch"  # a wrong condition, swapped branches, a missed case
    LOOKUP = "lookup"  # fetching the wrong contract, a wrong filter, a wrong key
    TIME = "time"  # a wrong comparison with the ledger time or a deadline
    FIELD = "field"  # a wrong or swapped field in a `create`, a record update or a return value
    OTHER = "other"  # none of the above; the mutation's `reason` says what it is


@dataclass(frozen=True)
class Mutation:
    """One seeded bug, as a task's mutation file lists it."""

    id: str  # unique within the task, in kebab case
    source: MutationSource
    kind: MutationKind  # a descriptive label; the kinds overlap, so it never feeds a score
    reason: str  # why a developer would make this mistake
    patch: str  # a unified diff against the task's implementation files, paths relative to the repository
    target_script: str | None = None  # the ground-truth script it was aimed at; only the authoring validator reads it
    commit: str | None = None  # for a real bug: the fix it undoes

    @classmethod
    def from_yaml(cls, entry: dict) -> Mutation:
        unknown = set(entry) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"mutation {entry.get('id')!r} has unknown fields {sorted(unknown)}")
        return cls(**{**entry, "source": MutationSource(entry["source"]), "kind": MutationKind(entry["kind"])})

    def patched_paths(self) -> list[str]:
        return patched_paths(self.patch)


def mutation_file(task_id: str) -> Path | None:
    """The task's mutation file, from the first task-list directory that has one, or None."""
    for tasklist_dir in locations.tasklist_dirs:
        path = tasklist_dir / "mutations" / f"{task_file_name(task_id)}.yaml"
        if path.exists():
            return path
    return None


def load_mutations(task_id: str) -> list[Mutation]:
    """The task's mutations, in file order. A task without a mutation file has none."""
    path = mutation_file(task_id)
    if path is None:
        return []
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    if spec["task"] != task_id:
        raise ValueError(f"{path} names task {spec['task']!r}, not {task_id!r}")
    mutations = [Mutation.from_yaml(entry) for entry in spec["mutations"]]
    ids = [m.id for m in mutations]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path} repeats a mutation id: {sorted(i for i in set(ids) if ids.count(i) > 1)}")
    return mutations


def load_hidden_from_copy(task_id: str) -> list[str]:
    """The paths, relative to the repository, that the task's copy leaves out: files or
    package directories outside the answer that would give it away, such as a sibling test
    package holding a copy of the test file's scripts. A task without such paths has none."""
    path = mutation_file(task_id)
    if path is None:
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("hidden_from_copy", [])


class ValidationOutcome(StrEnum):
    """How far a mutation got through validation: the first step it failed, in this order, or
    KILLED when it passed them all. Only a killed mutation is usable."""

    NOT_IMPL_ONLY = "not-impl-only"  # the patch touches a file that is not an implementation file
    DOES_NOT_APPLY = "does-not-apply"
    DOES_NOT_COMPILE = "does-not-compile"
    SURVIVED = "survived"  # the mutant compiles and the target script still passes
    KILLED = "killed"  # the target script passes on the ground truth and fails on the mutant


@dataclass(frozen=True)
class MutationValidation:
    """The validator's verdict on one mutation, as its report lists it.

    `newly_failing` holds the failures the mutation caused: the scripts that pass on the
    ground truth and fail on the mutant. It is a selection from the mutant run's
    `reported_failures`, which lists every failed script whatever the cause; a script missing
    from the mutant's report also counts as failing here, with "" as its message.
    """

    id: str
    source: MutationSource
    kind: MutationKind  # a descriptive label; it never feeds a score
    # None for a real-bug mutation that no ground-truth script is known to catch.
    target_script: str | None
    outcome: ValidationOutcome
    outcome_detail: str | None  # what the failing step reported; None for a mutant that ran its tests
    # Keyed `<file>:<script>` as grading reports them, with what `daml test` reported ("" when nothing).
    newly_failing: dict[str, str]
    seconds: float

    @classmethod
    def from_json(cls, entry: dict) -> MutationValidation:
        return cls(
            **{
                **entry,
                "source": MutationSource(entry["source"]),
                "kind": MutationKind(entry["kind"]),
                "outcome": ValidationOutcome(entry["outcome"]),
            }
        )


@dataclass(frozen=True)
class ValidationReport:
    """What the validator wrote for one task: the ground truth's scripts and a verdict per mutation."""

    task: str
    ground_truth_seconds: float
    ground_truth_scripts: list[str]  # every script the ground-truth run reported, keyed as grading keys them
    mutations: list[MutationValidation]  # in the mutation file's order at the time of validation

    @classmethod
    def from_json(cls, report: dict) -> ValidationReport:
        return cls(**{**report, "mutations": [MutationValidation.from_json(m) for m in report["mutations"]]})


def validation_report_path(task_id: str) -> Path:
    """Where the validator writes the task's report: the logs directory's `mutations/<task file name>.json`."""
    return locations.logs_dir / "mutations" / f"{task_file_name(task_id)}.json"


def load_validation_report(task_id: str) -> ValidationReport | None:
    """The task's last validation report, or None when its mutations were never validated here."""
    path = validation_report_path(task_id)
    if not path.exists():
        return None
    return ValidationReport.from_json(json.loads(path.read_text(encoding="utf-8")))


def patched_paths(patch: str) -> list[str]:
    """The repository-relative paths a unified diff changes."""
    return [line.removeprefix("+++ b/") for line in patch.splitlines() if line.startswith("+++ b/")]


def apply_patch(patch: str, originals: dict[str, bytes]) -> tuple[dict[str, bytes] | None, str]:
    """The files with the patch applied, keyed by repository-relative path, or None and git's
    error when it does not apply.

    The patch is applied to a scratch copy of the files. Git is stopped from looking above the
    scratch directory: inside a repository, it applies paths relative to the repository's top
    level and silently skips the ones outside the current directory.
    """
    with tempfile.TemporaryDirectory() as scratch:
        for rel, content in originals.items():
            Path(scratch, rel).parent.mkdir(parents=True, exist_ok=True)
            Path(scratch, rel).write_bytes(content)
        patch_file = Path(scratch, "mutation.patch")
        patch_file.write_text(patch, encoding="utf-8")
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(Path(scratch).parent)}
        applied = subprocess.run(["git", "apply", str(patch_file)], cwd=scratch, env=env, capture_output=True, text=True)
        if applied.returncode != 0:
            return None, applied.stderr
        return {rel: Path(scratch, rel).read_bytes() for rel in originals}, applied.stderr
