"""What the dashboard's mutation catalogue shows: every task that has mutations, and each
task's mutations in full.

The list is small, ids and labels only, so the page can filter and search all tasks at
once. One task's patches come with the original text of every file they change, read
from the task's repository checkout, so the page can show a patch inside its whole file.
The validator's report is optional: without one, a mutation shows without its outcome.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from daml_agent_benchmark.config import ExperimentConfig, TaskKindName
from daml_agent_benchmark.locations import task_file_name
from daml_agent_benchmark.mutations.model import (
    MutationKind,
    MutationSource,
    ValidationOutcome,
    load_mutations,
    load_validation_report,
)
from daml_agent_benchmark.records import Record
from daml_agent_benchmark.tasklist_catalog import get_selected_tasks, repo_relative_id, repo_root_for_path

# Folder names that say nothing about a task, left out of its short name.
GENERIC_PATH_PARTS = frozenset(
    {"daml", "src", "test", "tests", "main", "docs", "source", "sdk", "tutorials", "smart-contracts", "community", "common", "package"}
)
SHORT_NAME_PARTS = 2  # how many meaningful path parts a short name starts with


@dataclass(frozen=True)
class MutationLabel(Record):
    """A mutation as the task list shows it: enough to filter and search by."""

    id: str
    kind: MutationKind
    source: MutationSource


@dataclass(frozen=True)
class CatalogueEntry(Record):
    """One task in the catalogue's task list."""

    task_id: str
    file_name: str  # the task's file name, which the detail endpoint takes
    repo: str
    short_name: str
    mutations: list[MutationLabel]


@dataclass(frozen=True)
class Catalogue(Record):
    tasks: list[CatalogueEntry]


@dataclass(frozen=True)
class CatalogueValidation(Record):
    """A mutation's last validation against the original test file."""

    outcome: ValidationOutcome
    outcome_detail: str | None  # what the failing step reported; None for a mutant that ran its tests
    newly_failing: list[str]  # the scripts it breaks, by grading key; the messages stay in the report


@dataclass(frozen=True)
class CatalogueMutation(Record):
    id: str
    source: MutationSource
    kind: MutationKind
    reason: str
    patch: str
    target_script: str | None
    commit: str | None
    validation: CatalogueValidation | None  # None when no report lists the mutation


@dataclass(frozen=True)
class CatalogueTaskDetail(Record):
    """One task's mutations in full, with the original text of the files they patch."""

    task_id: str
    scripts: int | None  # the original test file's own scripts; None without a report
    mutations: list[CatalogueMutation]
    files: dict[str, str]  # path in the repository -> original text, for each patched file checked out


@dataclass(frozen=True)
class CatalogueTask:
    task_id: str
    test_path: str  # absolute, in the repository checkout

    @property
    def repo(self) -> str:
        return self.task_id.split("/", 1)[0]


def _tasks() -> list[CatalogueTask]:
    """Every task that has mutations: the tasks a test-generation run can take, in id order."""
    selected = get_selected_tasks(ExperimentConfig(task_kind=TaskKindName.TEST_GENERATION))
    return sorted((CatalogueTask(repo_relative_id(path), path) for path in selected), key=lambda t: t.task_id)


def _meaningful_parts(task_id: str) -> list[str]:
    """The task's path inside its repository without generic folders or the `.daml` suffix."""
    path = task_id.split("/")[1:]
    path[-1] = path[-1].removesuffix(".daml")
    return [p for p in path if p.lower() not in GENERIC_PATH_PARTS] or path[-1:]


def short_names(task_ids: list[str]) -> dict[str, str]:
    """A short name for each task: the last two meaningful parts of its path, and more for
    the tasks whose name another task of the same repository shares, until none does."""
    parts = {t: _meaningful_parts(t) for t in task_ids}
    taken = dict.fromkeys(task_ids, SHORT_NAME_PARTS)
    while True:
        by_name: dict[tuple[str, str], list[str]] = defaultdict(list)
        for t in task_ids:
            by_name[(t.split("/", 1)[0], " / ".join(parts[t][-taken[t] :]))].append(t)
        clashing = [t for group in by_name.values() if len(group) > 1 for t in group if taken[t] < len(parts[t])]
        if not clashing:
            return {t: " / ".join(parts[t][-taken[t] :]) for t in task_ids}
        for t in clashing:
            taken[t] += 1


def catalogue() -> Catalogue:
    """Every task that has mutations, with its short name and each mutation's id, kind and origin."""
    tasks = _tasks()
    names = short_names([t.task_id for t in tasks])
    return Catalogue(
        tasks=[
            CatalogueEntry(
                task_id=t.task_id,
                file_name=task_file_name(t.task_id),
                repo=t.repo,
                short_name=names[t.task_id],
                mutations=[MutationLabel(id=m.id, kind=m.kind, source=m.source) for m in load_mutations(t.task_id)],
            )
            for t in tasks
        ]
    )


def _own_script_count(test_rel_path: str, script_keys: list[str]) -> int:
    """How many of the reported scripts are the test file's own. A key is `<path>:<script>`
    with the path relative to the test's package, so a suffix of the repository path; the
    others come from modules the test file imports."""
    return sum(1 for key in script_keys if test_rel_path.endswith(key.rsplit(":", 1)[0]))


def task_mutations(file_name: str) -> CatalogueTaskDetail | None:
    """One task's mutations in full, with the original text of the files they patch, or None
    for a file name that is no task with mutations.

    A mutation carries its last validation when the logs directory has a report that lists
    it. A patched file missing from the checkout, a repository not fetched, has no text.
    """
    task = next((t for t in _tasks() if task_file_name(t.task_id) == file_name), None)
    if task is None:
        return None
    mutations = load_mutations(task.task_id)
    report = load_validation_report(task.task_id)
    verdicts = {} if report is None else {v.id: v for v in report.mutations}
    repo_root = Path(repo_root_for_path(task.test_path))
    files = {}
    for rel in sorted({p for m in mutations for p in m.patched_paths()}):
        source = repo_root / rel
        if source.is_file():
            files[rel] = source.read_text(encoding="utf-8")
    test_rel = task.task_id.split("/", 1)[1]
    return CatalogueTaskDetail(
        task_id=task.task_id,
        scripts=None if report is None else _own_script_count(test_rel, report.ground_truth_scripts),
        mutations=[
            CatalogueMutation(
                id=m.id,
                source=m.source,
                kind=m.kind,
                reason=m.reason,
                patch=m.patch,
                target_script=m.target_script,
                commit=m.commit,
                validation=None
                if (v := verdicts.get(m.id)) is None
                else CatalogueValidation(outcome=v.outcome, outcome_detail=v.outcome_detail, newly_failing=sorted(v.newly_failing)),
            )
            for m in mutations
        ],
        files=files,
    )
