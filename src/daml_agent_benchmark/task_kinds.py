"""What kind of task a run measures: the few places where the kinds differ.

Every task is a test file and the implementation files it exercises. A kind decides four
things:
- which of those files the agent writes
- which of them grading takes from the pristine copy
- what the agent is told
- how its work is graded

Everything else about a task (the copy, the checks on it, the sandbox, the attempt, the
audit and the record) is the same for every kind and lives in `task_run/`.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from dataclasses import fields as dataclass_fields

from daml_agent_benchmark.config import ExperimentConfig, TaskKindName
from daml_agent_benchmark.mutations.model import MutationSource, load_hidden_from_copy, load_mutations
from daml_agent_benchmark.records import Grade, MutantResult, TestGenerationGrade
from daml_agent_benchmark.task_run.ground_truth import grade_in_environment
from daml_agent_benchmark.task_run.inputs import build_codex_prompt
from daml_agent_benchmark.task_run.mutant_grading import run_mutants


@dataclass(frozen=True)
class TaskCopy:
    """A task as a kind sees it: its id, and its files in the repository copy as absolute paths."""

    task_id: str  # `<repository>/<test file path>`, as in the run record
    root: str  # the copy of the repository
    test_file: str
    impl_files: list[str]
    module_source: str  # the test file as it was before the task blanked anything, for its module header

    def rel(self, path: str) -> str:
        """A path in the copy relative to its root, with forward slashes."""
        return os.path.relpath(path, self.root).replace("\\", "/")


class TaskKind(ABC):
    """What every kind supplies. Python refuses to create a kind that lacks one of these."""

    name: TaskKindName

    @abstractmethod
    def answer_files(self, test_file: str, impl_files: list[str]) -> list[str]:
        """The files the agent writes: blanked before the attempt, copied back after it, and
        the answer the copy and the SDK folder must not hold anywhere else. Of the task's own
        files, wherever they are: in the source repository or in its copy."""

    @abstractmethod
    def protected_files(self, test_file: str, impl_files: list[str]) -> list[str]:
        """The files grading takes from the pristine copy, so that an edit to them is tampering."""

    @abstractmethod
    def hidden_from_copy(self, task_id: str) -> list[str]:
        """The paths, relative to the repository, removed from the task's copy before anything
        runs in it, because they would give the answer away."""

    @abstractmethod
    def prompt(self, copy: TaskCopy, docs_skill_content: str | None) -> str:
        """What the agent is told."""

    @abstractmethod
    def grade(self, config: ExperimentConfig, copy: TaskCopy) -> Grade:
        """What the agent's files do, once they are back in the copy."""


class ImplementationKind(TaskKind):
    """The agent writes the implementation files; the ground-truth tests grade them."""

    name = TaskKindName.IMPLEMENTATION

    def answer_files(self, test_file: str, impl_files: list[str]) -> list[str]:
        return impl_files

    def protected_files(self, test_file: str, impl_files: list[str]) -> list[str]:
        return [test_file]

    def hidden_from_copy(self, task_id: str) -> list[str]:
        return []

    def prompt(self, copy: TaskCopy, docs_skill_content: str | None) -> str:
        return build_codex_prompt(copy.test_file, copy.impl_files, copy.root, docs_skill_content=docs_skill_content)

    def grade(self, config: ExperimentConfig, copy: TaskCopy) -> Grade:
        return grade_in_environment(copy.test_file, copy.impl_files, copy.impl_files)


_TEST_GENERATION_PROMPT = """You are solving one Daml benchmark task: write tests.

Goal:
- Write Daml Script tests for the implementation files listed below, in the test file {rel_test}. The file is empty; its module must be `{module}`.
- The tests must compile and pass against the implementation as it is now.
- They are graded on how many realistic bugs in the implementation they catch. Small mistakes of the kind a developer makes will be put into the implementation, one at a time, and a mistake counts as caught when at least one of your test scripts fails on it. A test file with any script failing on the correct implementation catches nothing. So test the implementation's behaviour thoroughly, including what it must allow and what it must refuse.
- You may run shell commands as needed (lint, build, test).

Constraints:
- Implementation files under test (do not change them):
{impl_list}
- Only the test file is kept; changes to any other file are discarded.
- Stop when your tests pass and cover the implementation's behaviour, or when you are blocked.

When done, reply with a short summary of what your tests cover and whether they pass."""


class TestGenerationKind(TaskKind):
    """The agent writes the test file for the implementation; the mutants it catches grade it."""

    name = TaskKindName.TEST_GENERATION
    __test__ = False  # not a pytest test class, whatever its name

    def answer_files(self, test_file: str, impl_files: list[str]) -> list[str]:
        return [test_file]

    def protected_files(self, test_file: str, impl_files: list[str]) -> list[str]:
        return impl_files

    def hidden_from_copy(self, task_id: str) -> list[str]:
        return load_hidden_from_copy(task_id)

    def prompt(self, copy: TaskCopy, docs_skill_content: str | None) -> str:
        # The module name comes from the test file, read before the task blanked it.
        module = _module_name(copy.module_source)
        prompt = _TEST_GENERATION_PROMPT.format(
            rel_test=copy.rel(copy.test_file),
            module=module,
            impl_list="\n".join(f"  - {copy.rel(p)}" for p in copy.impl_files),
        )
        if docs_skill_content:
            prompt += f"\n\nAdditional documentation-navigation guidance:\n\n{docs_skill_content.rstrip()}\n"
        return prompt

    def grade(self, config: ExperimentConfig, copy: TaskCopy) -> TestGenerationGrade:
        """Run the agent's test file on the correct code and, when it passes there, on each mutant.

        Only the scripts in the agent's own file count: `daml test` also reports scripts of
        modules the file imports.

        Each mutant is built in a clone of this copy made after the build on the correct code,
        so the clone starts with that build's outputs and Daml rebuilds only the packages the
        patch changed and those that depend on them.
        """
        mutations = load_mutations(copy.task_id)
        real_bugs = sum(1 for m in mutations if m.source is MutationSource.REAL_BUG)
        # No lint: `damlc lint` does not load the package's dependencies, so it cannot resolve
        # `Daml.Script`, which every test file imports. `daml test` compiles the file instead.
        on_correct = _own_scripts(grade_in_environment(copy.test_file, copy.impl_files, []), copy.rel(copy.test_file))
        fields_of = {f.name: getattr(on_correct, f.name) for f in dataclass_fields(Grade)}
        unbuilt = TestGenerationGrade(**fields_of, mutants=[], mutants_total=len(mutations), real_bugs_total=real_bugs)
        if not unbuilt.passes_on_correct_code():
            return unbuilt
        chosen = mutations if config.max_mutants_per_task is None else mutations[: config.max_mutants_per_task]
        results = []
        for run in run_mutants(copy.root, copy.rel(copy.test_file), [copy.rel(p) for p in copy.impl_files], chosen):
            results.append(
                MutantResult(
                    id=run.mutation.id,
                    source=run.mutation.source,
                    kind=run.mutation.kind,
                    grade=_own_scripts(run.grade, copy.rel(copy.test_file)),
                )
            )
        return replace(unbuilt, mutants=results)


def _own_scripts(grade: Grade, test_rel: str) -> Grade:
    """The grade with only the scripts of the test file at `test_rel`, keyed `<path in package>:<script>`."""
    own = {key: passed for key, passed in grade.test_results.items() if test_rel.endswith(key.rsplit(":", 1)[0])}
    return replace(grade, test_results=own)


def _module_name(source: str) -> str:
    return re.search(r"^module\s+([\w.]+)", source, re.M).group(1)


def task_kind(name: TaskKindName) -> TaskKind:
    """The kind an experiment names in `task_kind`."""
    kinds: dict[TaskKindName, TaskKind] = {
        TaskKindName.IMPLEMENTATION: ImplementationKind(),
        TaskKindName.TEST_GENERATION: TestGenerationKind(),
    }
    return kinds[name]
