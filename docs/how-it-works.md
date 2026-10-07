# How a task runs

A score means something only if the agent had to write the code itself, and if a failure is the agent's failure. This page follows one task and says what guarantees each of those conditions.

The files that the agent has to write are the target files: the implementation files in an implementation task, and the test file in a test-generation task (see [Test generation](test-generation.md)). Their original content is the answer.

## Steps of a task

1. **Copy.** The harness copies the source repository into a directory of its own, the repository copy. The agent never works in the original checkout.
2. **Scan.** While the original files are still in the copy, the harness scans every other file for the answer.
3. **Control.** With `ground_truth_control=True`, the harness builds and tests the original code, to check that the task works in this environment. It then deletes everything that the build added and scans again.
4. **Blank.** The harness empties the target files. They stay in place, so the agent knows their names. The prompt names the targets and the files they belong with, and asks the agent to edit only the targets: in an implementation task, to make the test file pass; in a test-generation task, to write tests for the implementation.
5. **Attempt.** The agent runs in a fresh container until it says it is done or its time runs out.
6. **Copy back.** Only the target files come back to the host. The harness also records which other files the agent changed, and collects its network log and its event stream: every message, command and tool call that the agent reported.
7. **Grade.** A second container, with no network, grades the targets. In an implementation task, it grades them against the untouched test file in three stages. The Daml linter checks syntax, with only errors failing. Then the project is built (see [Grading scope](#grading-scope)). Then the test file's scripts run, and the stage passes when the test runner exits zero. Each stage runs only when the one before it passed. In a test-generation task, the agent's test file runs on the untouched implementation and then on each mutant, as [Test generation](test-generation.md#how-the-tests-are-graded) describes.

## Avoiding answer leaking into sandbox

- The copy has no git history, no build outputs and no known duplicates of the sources.
- For `ex-models`, `canton` and the `daml` tutorials, the copy holds only the task's own package, because neighbouring packages contain near-copies of the answer.
- No prebuilt Daml archive (`.dar`) that contains a target stays in the copy.
- The scan finds exact copies of a target, reformatted copies and copies inside archives. A copy with a hit is never given to the agent. The scan also rejects git objects and symbolic links that point outside the copy.
- The SDK store, the host folder of Daml SDKs that every container mounts, holds no answer. SDK installs ship the `daml` tutorials as project templates, so the harness deletes them. SDK jars bundle example packages, so the harness removes a bundled `.dar` whose source for a target module holds the target, and every `.dar` that bundles such a package as a dependency. It also removes any other copy of a target's source in the store, in an archive or as a plain file, with the compiled forms of that module. A run refuses to start if the store holds an answer.

Some repositories publish generated API documentation, which lists the declarations of the target modules without their bodies. The test file exposes the same declarations, so these files stay.

## Sandbox isolation

- The copy is copied into the container, not mounted. The container runs as the host user, drops all Linux capabilities and cannot gain new privileges. It mounts the SDK store read-only.
- Each task has its own private network. The only way out is a proxy that forwards HTTPS to the model provider's domains and to the exact hosts of allowed MCP servers. It refuses everything else and logs every request.
- Web search, the browser, apps and plugins are turned off, because they would fetch outside content where the proxy cannot see it.
- The agent starts with a fresh home directory. No session, memory or credential carries over.
- The API key reaches the agent but not its shell commands, and it is redacted from the stored output.

## Grading scope

Grading sees only the targets that came back and the original test file. A changed test file or other changed sources cannot change the score. They are recorded as warnings, so that a reader of the transcript knows about them.

The build stage builds the whole project that holds the targets, not only the modules the test file imports. For a single package that is the whole package, including its other test modules. When the package is part of a multi-package project (a `multi-package.yaml` above it), every package in the project is built. So the targets must provide everything the rest of the project uses from them, even when the test file itself exercises less.

## Model verification

The task image pins one version of the agent. The agent's reported model and reasoning effort must match the experiment, for the main agent and every sub-agent. Every run stores a snapshot of the harness code and of the skill, when it has one.

## Findings

Every problem that a check notices becomes a finding in the task record. Its severity decides whether the task counts:

| Severity | Meaning | Effect |
|---|---|---|
| `security` | Something the isolation should make impossible happened. | Left out of the pass rates. |
| `infra` | The harness failed, not the agent. | Left out of the pass rates. |
| `warning` | Recorded for a reader of the record. | Graded normally. |

Infrastructure failures include a failing ground-truth control, a failed scan, a model or effort mismatch, an attempt that never reached the model, a turn that the provider ended by refusing the account (for example when the API credit runs out part-way through a task) and an exception in the harness. Such tasks are still graded where there are files, so the record shows what happened.

The flags are `out_of_workspace_writes`, `forbidden_tool_calls` and `non_allowed_egress` (security); `infra_failure`, `runtime_identity_mismatch`, `export_timed_out`, `repo_copy_integrity_failure` and `ground_truth_control_failure` (infra); and nine warnings. [`records.py`](../src/daml_agent_benchmark/records.py) defines them all.

## Red-team testing

In a red-team sweep the agent's only job is to search its sandbox for the answer, through the same path as a benchmark task. In some of the sandboxes, a copy of the answer, whichever files the run's task kind has the agent write, is planted without being named, to show that the search works. On the public implementation tasks, the agent found no leak in any of 17 clean sandboxes, and it found the planted copy in 16 of 17 others. [`redteam.py`](../src/daml_agent_benchmark/redteam.py) runs a sweep. It writes one JSON file per sweep: a summary with the canary and leak counts, one verdict per task the agent finished (`tasks`), and the tasks the harness could not run (`failed`). `--skip-tasks-in` reads such files to leave their finished tasks out of the next sweep.
