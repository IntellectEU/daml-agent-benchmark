# Writing mutations for a task

A test-generation task gives an agent a task's implementation files and asks it to write
tests for them. The tests are graded on mutants: copies of the implementation with one
bug put in. Good tests fail on a mutant. This file is the instruction for writing the
mutations of one task. Whoever writes them, a person or a model, follows it.

## What you are given

- The task: a ground-truth test file and the implementation files it exercises, in a
  source repository at its pinned commit (`tasklist/tasks.yaml`, `tasklist/repos.yaml`).
- The rest of the repository, read-only, for context.

## What to write

For every test script in the ground-truth test file, mutations that make that script fail:
one for each distinct mistake the script can catch. Each mutation is a mistake a developer could plausibly make while writing the
implementation: the kind of bug that a reviewer would call a real bug, not an edit made to
break a test.

Rules for every mutation:

1. **Implementation files only.** The patch changes only the task's implementation files,
   never the test file or anything else.
2. **It compiles.** The mutant still type-checks and builds. A type error is not a bug that
   tests detect.
3. **It changes behaviour for general inputs.** No conditions on the values the test
   happens to use (`if amount == 100 then ...`, a party name the test uses, a count the
   test reaches). If a test with different values would miss it, it is tailored to the test.
4. **Not cosmetic.** Changing only an error message, a string the test compares, a name,
   or a comment is not a mutation.
5. **Small.** One mistake, usually a few lines. Two unrelated changes are two mutations.
6. **Distinct.** No two mutations of a task are the same mistake at the same place in another
   form: dropping a check and inverting that same check, or two different wrong signatories on
   the same line, are one mistake. The same kind of slip at a different place is a separate
   mutation: a wrong controller on two different choices, or a copy-paste slip repeated on
   the next line, can each be caught without the other. Two different mistakes that fail the
   same ground-truth scripts are both kept: a long ground-truth script checks many things at
   once, and the tests that the benchmark grades may catch one of the two and miss the other.

Aim at the target script, but it is fine if other scripts fail too, since scripts often share
setup. Prefer mutations that break few scripts over ones that break every script.

Pick the kind that fits best:

| kind | examples |
|---|---|
| `authorization` | a signatory, observer or controller missing or wrong |
| `archival` | a choice consuming when it should not be, or the reverse; a missing `archive` |
| `validation` | an `ensure`, `assert` or `assertMsg` weakened, dropped or inverted |
| `arithmetic` | a wrong operator, an off-by-one, a wrong constant |
| `branch` | a wrong condition, swapped branches, a missed case |
| `lookup` | fetching the wrong contract, a wrong filter, a wrong key |
| `time` | a wrong comparison with the ledger time or a deadline |
| `field` | a wrong or swapped field in a `create`, a record update or a return value |
| `other` | none of the above; say what it is in `reason` |

If none fits the rules for a script, write none for it and say why in `notes`. The same goes for a task whose implementation files hold
little behaviour (only data types, say): write the few mutations there are and say so.

What counts as a script is what the validator reports: a top-level `Script` value with no
arguments. A helper that takes arguments (a fixture, say) is not one. `target_script` is the
name exactly as the validator's report shows it, including a trailing `'` when the name has
one. When several scripts run the same flow, one mutation can serve them all; say so in
`notes` instead of forcing a separate one per script.

## Checking your mutations

Do not reason out by hand whether a mutant compiles or which scripts fail on it: run it.
The validator builds each mutant and runs the ground-truth test file on it, in the same
container that grading uses:

```bash
python -m daml_agent_benchmark.mutations.validate <task id> --only <mutation id> ...
```

A checkout that keeps its source repositories and task list elsewhere runs it through the code
that configures those locations first; the brief you were given says how.

For each mutation it prints one outcome and the scripts that pass on the ground truth but
fail on the mutant:

| outcome | meaning |
|---|---|
| `killed` | the target script fails on the mutant: the mutation is usable |
| `survived` | the mutant compiles and the target script still passes |
| `does-not-compile` | the implementation or the test file no longer compiles |
| `does-not-apply` | the patch does not apply to the pinned files |
| `not-impl-only` | the patch changes a file that is not an implementation file |

Work in rounds: write mutations, validate them, then fix, replace or drop every one that is
not `killed`, and every one that repeats another mistake (rule 6). Validate again after every
change. Hand back only mutations whose last validation was `killed`, and finish with one run
over all of them without `--only`.

A `real-bug` mutation is exempt: it undoes a fix from the repository's history, so it is real
whether or not the ground-truth tests catch it. One that no script is known to catch has no
`target_script`, and the validator then reports it `killed` when any script fails on it. Leave
real-bug entries as they are, whatever their outcome.

Keep your working files (scratch copies, diffs, helper scripts) in the scratch directory
you are given, never in the repository.

## Output

One YAML file per task, at `tasklist/mutations/<task file name>.yaml`: the task id,
`<repo>/<test file path>`, with each `/` replaced by `__`, as task records name their files.
Paths in patches are relative to the repository root, as `git diff` writes them.

Make each patch with `git diff`, never by hand: copy the pinned implementation file into a
scratch directory, edit the copy, and diff it against the original. Hand-written hunk headers
and context lines rarely apply. In the YAML block, a blank context line of the diff is a line
holding just the block's indentation followed by one space.

```yaml
task: <repo>/<test file path>
notes: <anything a reviewer should know, or empty>
hidden_from_copy:          # optional: paths left out of the task's copy because they hold the test's scripts
  - <path relative to the repository>
mutations:
  - id: <short-kebab-case-name>
    source: llm            # or real-bug, for a fix undone from the repository's history
    commit: <the fix's commit, for a real-bug mutation only>
    target_script: <script name in the ground-truth test file; omitted for a real bug no script catches>
    kind: <kind from the table>
    reason: <one sentence: why a developer would make this mistake>
    patch: |
      diff --git a/<path> b/<path>
      ...
```

`reason` describes the mistake in terms of the implementation, not the test: "the
controller of the cancel choice is the new owner instead of the current one", not "makes
testCancel fail".
