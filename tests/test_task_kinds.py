"""Task kinds: which files each kind treats as the answer, the test-generation grade and its
record, and the run-level numbers computed from it."""

from dataclasses import dataclass, replace

import pytest

from daml_agent_benchmark.config import ExperimentConfig, TaskKindName
from daml_agent_benchmark.locations import task_file_name
from daml_agent_benchmark.mutations.validate import ValidationOutcome, _patch_problem
from daml_agent_benchmark.mutations.model import Mutation, MutationKind, MutationSource, apply_patch, load_mutations
from daml_agent_benchmark.records import (
    Grade,
    MutantResult,
    Record,
    RunResult,
    RunStatus,
    TaskResult,
    TestGenerationGrade,
    _record_class,
)
from daml_agent_benchmark.task_kinds import TaskCopy, TaskKind, _own_scripts, task_kind
from daml_agent_benchmark.tasklist_catalog import package_task_ids

from test_records import NOW, _task

PASSING = Grade(True, True, True, {"Test.daml:a": True, "Test.daml:b": True}, None, None, None)


def _mutant(mid: str, results: dict[str, bool] | None, source: MutationSource = MutationSource.LLM) -> MutantResult:
    grade = (
        Grade(True, True, all(results.values()), results, None, None, None)
        if results is not None
        else Grade(True, False, False, {}, None, "type error", None)
    )
    return MutantResult(id=mid, source=source, kind=MutationKind.BRANCH, grade=grade)


def _tg_grade(mutants: list[MutantResult], total: int | None = None, real_bugs: int = 0) -> TestGenerationGrade:
    return TestGenerationGrade(
        **{f: getattr(PASSING, f) for f in PASSING.__dataclass_fields__},
        mutants=mutants,
        mutants_total=len(mutants) if total is None else total,
        real_bugs_total=real_bugs,
    )


def test_kinds_swap_the_answer_and_the_protected_files() -> None:
    impl, tests = task_kind(TaskKindName.IMPLEMENTATION), task_kind(TaskKindName.TEST_GENERATION)
    assert impl.answer_files("t.daml", ["a.daml", "b.daml"]) == ["a.daml", "b.daml"]
    assert impl.protected_files("t.daml", ["a.daml", "b.daml"]) == ["t.daml"]
    assert tests.answer_files("t.daml", ["a.daml", "b.daml"]) == ["t.daml"]
    assert tests.protected_files("t.daml", ["a.daml", "b.daml"]) == ["a.daml", "b.daml"]
    with pytest.raises(ValueError):
        ExperimentConfig(task_kind="spec")


def test_a_kind_missing_a_method_cannot_be_created() -> None:
    class Partial(TaskKind):
        def answer_files(self, test_file: str, impl_files: list[str]) -> list[str]:
            return impl_files

    with pytest.raises(TypeError, match="abstract"):
        Partial()
    assert all(isinstance(task_kind(name), TaskKind) for name in TaskKindName)


def test_test_generation_prompt_names_the_module_and_the_files() -> None:
    copy = TaskCopy("repo/daml/Test.daml", "/c", "/c/daml/Test.daml", ["/c/daml/Impl.daml"], "module Test.Shop where\n")
    prompt = task_kind(TaskKindName.TEST_GENERATION).prompt(copy, None)
    assert "daml/Test.daml" in prompt and "`Test.Shop`" in prompt and "  - daml/Impl.daml" in prompt


def test_only_the_agents_own_scripts_count() -> None:
    grade = Grade(True, True, True, {"daml/Test.daml:a": True, "daml/Utils.daml:helper": True}, None, None, None)
    assert _own_scripts(grade, "pkg/daml/Test.daml").test_results == {"daml/Test.daml:a": True}


def test_a_mutant_is_caught_by_a_failing_script_or_a_broken_build() -> None:
    grade = _tg_grade([
        _mutant("caught", {"Test.daml:a": False, "Test.daml:b": True}),
        _mutant("missed", {"Test.daml:a": True, "Test.daml:b": True}),
        _mutant("no-build", None),
    ])
    assert [grade.caught(m) for m in grade.mutants] == [True, False, True]
    assert grade.caught_count() == 2
    assert grade.passes_on_correct_code()


def test_test_generation_grade_reads_back_as_itself_and_a_plain_grade_as_a_grade() -> None:
    grade = _tg_grade([_mutant("m", {"Test.daml:a": False, "Test.daml:b": True})], total=3, real_bugs=1)
    task = _task("tg", grade=grade)
    back = TaskResult.from_record(task.to_record())
    assert isinstance(back.grade, TestGenerationGrade) and back == task
    plain = TaskResult.from_record(_task("impl").to_record())
    assert type(plain.grade) is Grade


def test_two_record_classes_with_the_same_fields_are_refused() -> None:
    @dataclass(frozen=True)
    class Base(Record):
        x: int

    @dataclass(frozen=True)
    class Twin(Base):
        pass

    with pytest.raises(ValueError, match="same fields"):
        _record_class(Base, {"x": 1})


def test_run_numbers_count_all_mutants_and_only_gradable_tasks() -> None:
    real = _mutant("bug", {"Test.daml:a": False, "Test.daml:b": True}, MutationSource.REAL_BUG)
    tasks = [
        # 2 of 4 caught, one of them the real bug.
        _task("a", grade=_tg_grade([real, _mutant("m1", {"Test.daml:a": True, "Test.daml:b": True}), _mutant("m2", None)], total=4, real_bugs=1)),
        # Its tests fail on the correct code: its 3 mutants count, none caught.
        _task("b", grade=replace(_tg_grade([], total=3), tests_passed=False,
                                 test_results={"Test.daml:a": False})),
    ]
    run = RunResult("r", "n", RunStatus.COMPLETED, NOW, NOW, tasks, None, [], [])
    assert (run.mutants_caught(), run.mutants_total()) == (2, 7)
    assert run.mutant_catch_rate() == pytest.approx(2 / 7)
    assert run.mean_task_catch_rate() == pytest.approx((2 / 4 + 0) / 2)
    assert run.tests_pass_on_correct_code() == 1
    assert run.real_bugs_caught() == (1, 1)
    # Task a built 3 of its 4 mutants although its tests pass on the correct code: a capped run.
    assert run.mutants_capped()


def test_mutation_file_fields_are_checked(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        Mutation.from_yaml({"id": "x", "source": "llm", "kind": "branch", "reason": "r", "patch": "", "severity": 3})
    with pytest.raises(ValueError, match="authorisation"):
        Mutation.from_yaml({"id": "x", "source": "llm", "kind": "authorisation", "reason": "r", "patch": ""})
    patch = "--- a/f.daml\n+++ b/f.daml\n@@ -1 +1 @@\n-one\n+two\n"
    patched, _ = apply_patch(patch, {"f.daml": b"one\n"})
    assert patched == {"f.daml": b"two\n"}
    assert apply_patch(patch, {"f.daml": b"other\n"})[0] is None


def test_every_package_task_mutation_file_loads() -> None:
    loaded = {task: load_mutations(task) for task in package_task_ids()}
    assert sum(len(m) for m in loaded.values()) > 0
    assert all(m.source in MutationSource for ms in loaded.values() for m in ms)


def test_task_file_names_are_flat_and_refuse_ambiguous_ids() -> None:
    assert task_file_name("ex-models/shop/daml/Test.daml") == "ex-models__shop__daml__Test.daml"
    # "a/b__c" and "a__b/c" would both become "a__b__c".
    with pytest.raises(ValueError, match="__"):
        task_file_name("repo/some__dir/Test.daml")


def test_the_validator_rejects_a_patch_outside_the_implementation_or_one_that_does_not_apply() -> None:
    patch = "--- a/f.daml\n+++ b/f.daml\n@@ -1 +1 @@\n-one\n+two\n"
    mutation = Mutation(id="m", source=MutationSource.LLM, kind=MutationKind.OTHER, reason="r", patch=patch)
    assert _patch_problem(mutation, ["f.daml"], {"f.daml": b"one\n"}) is None
    assert _patch_problem(mutation, ["g.daml"], {"g.daml": b"one\n"}).outcome is ValidationOutcome.NOT_IMPL_ONLY
    assert _patch_problem(mutation, ["f.daml"], {"f.daml": b"other\n"}).outcome is ValidationOutcome.DOES_NOT_APPLY


def test_why_no_mutants_follows_the_run_on_the_correct_code() -> None:
    built = _tg_grade([])
    assert built.why_no_mutants() is None and built.passes_on_correct_code()
    assert replace(built, compile_passed=False, test_results={}).why_no_mutants() == "the test file does not compile"
    assert replace(built, test_results={}, tests_error="parse error").why_no_mutants() == "the test file does not compile"
    assert replace(built, test_results={}).why_no_mutants() == "the test file has no scripts"
    failing = replace(built, test_results={"Test.daml:a": True, "Test.daml:b": False})
    assert failing.why_no_mutants() == "scripts fail on the correct implementation: ['Test.daml:b']"
