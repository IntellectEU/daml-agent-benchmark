# Reviewing a task's mutations for realism

`generate.md` asks for mutations that are mistakes a developer could plausibly make, and the
validator checks that each one compiles and is caught by the ground-truth tests. Neither
checks the first part. This file is the instruction for that check: a reviewer who did not
write the mutations reads each one and judges whether it looks like a real mistake.

## What you are given

- The task's mutation file, `tasklist/mutations/<task file name>.yaml` (the task id with each
  `/` replaced by `__`), and the source repository at its pinned commit.
- The validator's report for the task, with the scripts each mutant fails.

## What to judge

For each `source: llm` mutation, read the patch in the context of the whole implementation
file, and ask: would a competent developer writing this code plausibly make this mistake?

- **plausible**: a forgotten check, a wrong party in a signatory, observer or controller, an
  inverted or off-by-one comparison, two similar names mixed up, a forgotten archive or state
  update, a copy-paste slip that the surrounding code invites.
- **borderline**: plausible only under an assumption about how the code was written, for
  example a mix-up that needs the author to have spelled out fields the original fills in with
  `..`. Say what the assumption is.
- **unrealistic**: an edit nobody makes by accident, such as multiplying an amount by two, a
  typed constant that no reasoning leads to, or a change that only makes sense as an attempt to
  break a test.

Also mark as `unrealistic` a mutation that repeats another mutation of the task at the same
place in a different form (rule 6 of `generate.md`), and keep the more natural of the two. The
same slip at a different place, such as the same wrong party in another choice or a
neighbouring line, is not a repeat: judge it on its own. A choice whose body looks copied from
a neighbouring choice and left unfinished is a copy-paste slip, and plausible when the two
choices are written side by side.

`source: real-bug` mutations are real by definition: leave them unjudged.

## Output

Report a verdict for each `llm` mutation, by its id, and leave the mutation file unchanged:

```
<mutation id>: plausible | borderline | unrealistic — <one sentence: why, and for borderline the assumption it needs>
```

Only the plausible mutations stay in the mutation file. The others are removed from it.
