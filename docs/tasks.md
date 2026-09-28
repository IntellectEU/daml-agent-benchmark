# The tasks

A task is one test file from a real Daml repository and the implementation files that the test exercises. The agent gets the repository with those implementation files emptied and has to write them again so that the test passes.

The package ships 34 tasks from six public repositories. [`tasks.yaml`](../src/daml_agent_benchmark/tasklist/tasks.yaml) lists every task, and [`repos.yaml`](../src/daml_agent_benchmark/tasklist/repos.yaml) declares the repositories.

## How a task is built

A task entry names three things:

- `repo`: the repository, as declared in `repos.yaml`.
- `test`: the test file, relative to the repository root.
- `impl`: the implementation files that the test exercises.

The agent may read everything else in its copy of the repository: the test file, the package's `daml.yaml`, sibling modules and, where the copy keeps it, the rest of the repository. Only the implementation files are graded, against the untouched test file.

A test qualifies only when it lives apart from the implementation it checks. Many Daml projects put their test scripts inside the implementation module, and emptying that module would remove the test too.

Some related files stay visible on purpose. A module that every other module imports as shared vocabulary, such as a `Types.daml`, is not emptied, because asking the agent to reinvent it would mean guessing the whole domain model. The comments in `tasks.yaml` record these choices.

## Source repositories

All six repositories are licensed under the Apache License 2.0. They are not redistributed here. `uv run python -m daml_agent_benchmark.fetch_sources` fetches each one at the commit below, which is the commit that the tasks were written against.

| Repository | Tasks | Copyright | Commit | Source |
|---|--:|---|---|---|
| `account-hierarchy` | 2 | ASX Operations Pty Ltd (Synfini) | `407a020` | https://github.com/SynfiniDLT/account-hierarchy |
| `canton` | 3 | Digital Asset (Switzerland) GmbH | `bb8170f` | https://github.com/digital-asset/canton |
| `daml` | 8 | Digital Asset (Switzerland) GmbH | `c69d4fd` | https://github.com/digital-asset/daml |
| `daml-finance` | 1 | Digital Asset (Switzerland) GmbH | `155f931` | https://github.com/digital-asset/daml-finance |
| `ex-models` | 10 | Digital Asset (Switzerland) GmbH | `ca6818c` | https://github.com/digital-asset-archive/ex-models |
| `splice` | 10 | Digital Asset (Switzerland) GmbH | `e04a9d5` | https://github.com/hyperledger-labs/splice |

`repos.yaml` holds the full commit hashes. See [`NOTICE`](../NOTICE) for the full attribution.

## Task ids

A task's id is `<repository>/<path to the test file>`, for example `ex-models/tic-tac-toe/daml/Test.daml`. The id is the same on every machine, wherever the sources are checked out. An experiment names tasks by id in its `tasks` setting (see [Running experiments](running.md)).

## Adding tasks

A task takes two entries: its repository in `repos.yaml` and the task itself in `tasks.yaml`. In a checkout of this package, add both to the package's own files in [`src/daml_agent_benchmark/tasklist/`](../src/daml_agent_benchmark/tasklist). The fetcher and the runner read them from there, with no other setup. Added tasks go through the same steps as the package's: the copy is scanned for the answer, the sandbox is isolated, and grading and the dashboard work unchanged (see [How a task runs](how-it-works.md)).

The examples in this section add one task from a made-up repository, `example-repo`. Its name, URL, commit and file paths are placeholders for your own.

### Declaring the repository

`repos.yaml` declares each repository under the name of the folder that it is checked out in. The entry for the made-up repository:

```yaml
example-repo:
  url: git@git.example.com:example-org/example-repo.git
  commit: 0123456789abcdef0123456789abcdef01234567  # the full hash of the commit to pin
  license: null
  build_tool: daml
```

Every entry needs all four fields. `build_tool` is `daml` for a repository that builds with the Daml assistant, or `dpm` for one that builds with dpm. `license` is only recorded, and `null` is allowed. `url` and `commit` may both be `null` for a repository that you put in place yourself (see [Getting the source into place](#getting-the-source-into-place)).

### Declaring the task

`tasks.yaml` lists the tasks. The paths are relative to the repository root, and `impl` is always a list. The made-up task, with its test file and the two implementation files that test exercises:

```yaml
- repo: example-repo
  test: daml/Test/Example.daml
  impl:
    - daml/Example/Main.daml
    - daml/Example/Helpers.daml
```

The rules in [How a task is built](#how-a-task-is-built) apply: the test must live apart from the implementation files. The task's id is `example-repo/daml/Test/Example.daml`, and the SDK version is the one that the test file's `daml.yaml` names. An experiment runs it like any other task, by id in `tasks` or through `task_set="all"`.

### Getting the source into place

The fetcher fetches the new repository at its pinned commit, as it does the package's:

```bash
uv run python -m daml_agent_benchmark.fetch_sources example-repo
```

The fetcher does not let git ask for a password, so a private repository must be reachable without one: over SSH with a key, or over HTTPS with a credential helper.

A repository declared with `url` and `commit` set to `null` is not fetched at all, and naming it gives "unknown repository". Copy its source to `sources/example-repo` by hand.

Run the new tasks once with `ground_truth_control=True`, to check that each one builds and passes with its original code.

### When a repository needs a handler

By default the harness copies the repository without its git history and build outputs, and many repositories build from that plain copy. A repository needs a handler when its copy needs more to build, such as a dependency built first, or when part of it has to stay out of the copy. The package's handlers in [`repos/`](../src/daml_agent_benchmark/repos) are the examples to follow, and [`registry.py`](../src/daml_agent_benchmark/repos/registry.py) documents every field.

A new handler goes in a module in `repos/`, whose name is added to `_BUILTIN_HANDLER_MODULES` in `registry.py`. A handler must never put a compiled form of a target file into the copy, because that would hand the agent the answer.

### Keeping tasks in a separate directory

Tasks that must stay out of the package's checkout, such as tasks from private repositories, can live in a directory of their own. It holds a `repos.yaml` and a `tasks.yaml` in the same format, and optionally a `handlers.py` with the handlers of its repositories. Pass it to the runner and the fetcher with `--tasklist-dir`:

```bash
uv run python -m daml_agent_benchmark.runner --tasklist-dir /path/to/example-tasklist
uv run python -m daml_agent_benchmark.fetch_sources --tasklist-dir /path/to/example-tasklist example-repo
```

The package's own tasks stay included, and the names in the directory must differ from theirs.
