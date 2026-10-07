# Test generation

The benchmark has two kinds of task. In an **implementation** task the agent writes the implementation files that a test file exercises. In a **test-generation** task it is the other way round: the implementation stays, the test file is emptied, and the agent writes tests for the implementation. The tests are graded on how many seeded bugs, called mutants, they catch.

A run has one kind, set with `task_kind` (see [Running experiments](running.md)). Everything else about a task is the same for both kinds: the repository copy and the checks on it, the sandbox, the audit of what the agent did, and the record. [`task_kinds.py`](../src/daml_agent_benchmark/task_kinds.py) holds the few places where the kinds differ: which files the agent writes, which files grading takes from the untouched copy, the prompt, and the grading.

## What the agent is given

The agent sees the implementation files and the rest of the repository, with the task's test file emptied. It may build and run tests in its sandbox. It is told that its tests are graded on realistic bugs, but not how many there are, which kinds, or anything about the original test file. Only the test file it writes is copied back; changes to any other file are discarded, and a change to an implementation file is recorded as tampering.

## How the tests are graded

1. The agent's test file is built and run against the correct implementation. Only the scripts in the agent's own file count, since `daml test` also reports scripts of modules the file imports. The test file is not linted: `damlc lint` does not load a package's dependencies, so it cannot resolve `Daml.Script`, which every test file imports. `daml test` compiles the file instead.
2. If the file does not compile, or any of its scripts fails on the correct implementation, the task catches nothing and no mutant is built. A test suite that fails on correct code is broken.
3. Otherwise each mutant is built from a clone of the copy, with its patch applied, and the agent's test file is run on it. A mutant is **caught** when a script that passed on the correct code fails on it, or when the test file no longer builds against it.

Each mutant costs one build and test run. The clones reuse the correct build's outputs, so Daml's own build cache rebuilds only the package the patch changed and the packages that depend on it.

## What the numbers mean

For a test-generation run the benchmark reports:

- **Mutant catch rate**, the headline: of all mutants of the graded tasks, the share caught. A task whose tests do not compile or fail on the correct code counts with all its mutants as missed. A task with 25 mutants weighs 25 times as much as one with one.
- **Per-task catch rate**: the same share for each task, averaged, so that every task weighs the same.
- **Test files passing on the correct code**: how many tasks produced a working test suite at all.
- **Real bugs caught**: how many of the mutants that undo a fix from the repository's history were caught.

As for implementation runs, only tasks without a security or infrastructure finding count.

`max_mutants_per_task` grades only each task's first N mutants, in file order, for quick development runs. A capped run is marked on the dashboard and is not comparable to a full one.

## The mutants

Each task's mutants are in `tasklist/mutations/<task file name>.yaml` (the task id with each `/` replaced by `__`). A mutation is a small patch to the implementation files: one mistake a developer could plausibly make, such as a wrong controller, a dropped check, an inverted comparison or a missing archive. The package ships 159 mutants for its 34 tasks.

They were made in three steps:

1. **Written.** A model wrote mutations for each task following [`mutations/generate.md`](../src/daml_agent_benchmark/mutations/generate.md): for each script of the original test file, mistakes that make that script fail, never tailored to the test's values and never cosmetic. Where a repository's history has a fix that the test file exercises, the fix undone is a **real-bug** mutation.
2. **Validated.** [`mutations/validate.py`](../src/daml_agent_benchmark/mutations/validate.py) builds every mutant and runs the original test file on it, in the same container that grading uses. A model-written mutation is kept only when it applies, compiles and makes its target script fail. A real-bug mutation is kept whether or not the original tests catch it.
3. **Reviewed for realism.** A separate reviewer judged each model-written mutation against [`mutations/review.md`](../src/daml_agent_benchmark/mutations/review.md): plausible, borderline or unrealistic. Only the plausible ones are in the benchmark. Two mutations that are the same mistake at the same place in another form, such as dropping a check and inverting it, count as one.

Each mutation records a `kind` (authorization, archival, validation and so on). The categories overlap, so the kind is a label for reading results and never part of a score.

The dashboard's **mutation catalogue**, linked from its test-generation view, shows every task's mutations: each patch side by side, optionally inside its whole file, with its kind and origin, and, where the logs directory has a validation report, whether the original tests catch it and which of their scripts it breaks.

## Leak checks

The answer of a test-generation task is its test file. The same checks as for implementation tasks run on it: the repository copy and the SDK folder must not hold a copy of the test file, and the red-team driver (`python -m daml_agent_benchmark.redteam --task-kind test_generation`) asks an agent to search the sandbox for the reference test file or a list of the seeded bugs. The mutation files are part of the package, which is never mounted into the agent's container.
