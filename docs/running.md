# Running experiments

An experiment is a set of tasks and the settings that the agent runs them with.

## Experiment files

Copy the example and edit it:

```bash
cp experiment.example.py experiment.py   # ignored by git
uv run python -m daml_agent_benchmark.runner
```

The runner loads `./experiment.py`. `--config PATH` loads another file. The file sets `CURRENT_EXP` to one experiment, or `CURRENT_EXP_LIST` to a list that runs one after the other. `--exp-index N` runs only the N-th of the list, counting from 1. `--tasklist-dir PATH` adds a task-list directory to the package's own (see [Keeping tasks in a separate directory](tasks.md#keeping-tasks-in-a-separate-directory)).

An experiment is a `SimpleNamespace` that holds only the settings that it changes. Every other setting keeps its default. An unknown setting name fails the run before it starts.

```python
from types import SimpleNamespace

ONE_PER_REPO = SimpleNamespace(
    codex_model="gpt-6-luna",
    task_set="one_per_repo",
    max_task_runtime_seconds=600,
    max_parallel_tasks=2,
)

CURRENT_EXP = ONE_PER_REPO
```

Keep the experiments that you ran as named presets in the file, so it records what each run was.

## Common settings

- `codex_model`: the model that the agent runs on. `codex_app_server_effort` sets its reasoning effort.
- `task_set`: `"all"`, or `"one_per_repo"` for the first task of each repository, by test file path. With `task_set="all"`, `tasks_per_repo` caps the tasks taken from each repository. `tasks` is an explicit list of task ids and overrides both.
- `max_task_runtime_seconds`: how long the agent may work on one task. The default of 120 suits a smoke test. For a benchmark run, 600 is a common choice. Preparing the copy and grading do not count against it. A timed-out attempt is still graded on the files that it wrote.
- `max_parallel_tasks`: how many tasks run at once (see [Machine sizing](#machine-sizing)).
- `ground_truth_control`: build and test each task with its original code before the agent runs. A task that fails is an infrastructure failure, and the agent does not run on it. Turn it on for benchmark runs. Passing controls are cached.
- `run_name`: the name that the dashboard shows.
- `task_kind`: `"implementation"` (the default), or `"test_generation"` for the agent to write tests graded on mutants. A test-generation run takes only tasks that have mutants. See [Test generation](test-generation.md).
- `max_mutants_per_task`: for a quick test-generation run, grade only each task's first N mutants. A capped run is not a baseline.

Every setting and its default is documented in [`config.py`](../src/daml_agent_benchmark/config.py).

## Another model provider

The agent can use any provider that serves the OpenAI Responses API (`/responses`). A provider with only the Chat Completions API does not work. This example runs a model through OpenRouter:

```python
OPENROUTER = SimpleNamespace(
    codex_model="nvidia/nemotron-3-super-120b-a12b:free",  # the model's id at the provider
    provider_base_url="https://openrouter.ai/api/v1",
    provider_api_key_env="OPENROUTER_API_KEY",
    provider_allowed_domains=["openrouter.ai"],
    provider_requires_openai_auth=False,
)
```

The network proxy allows only `provider_allowed_domains` and their subdomains, so in this example the agent cannot reach OpenAI. The base URL has to be `https://` on port 443, with its host under one of those domains, and the key's variable has to be set. The run checks this before it builds anything. Set `provider_requires_openai_auth=False` for any provider other than OpenAI.

## MCP servers

MCP (Model Context Protocol) servers give the agent tools that a separate server runs. By default the agent has none, and an MCP tool call is a security finding. `mcp_servers` allows remote servers by name:

```python
from daml_agent_benchmark.config import McpServer

WITH_TOOLS = SimpleNamespace(
    task_set="one_per_repo",
    mcp_servers=[
        McpServer(name="daml-tools", url="https://tools.example.org/mcp", bearer_token_env="DAML_TOOLS_TOKEN"),
    ],
)
```

The `url` has to be `https://` on port 443 and reachable on the internet, because the container cannot reach the host machine. The proxy allows the server's exact host, not its subdomains. `bearer_token_env` optionally names the variable that holds a token for the server. Servers that run as a local command are not supported. Each call to an allowed server is recorded in the task record.

## Giving the agent a skill

A skill is a directory with a `SKILL.md` at its top, plus any scripts or reference files that it uses. Point `skill` at it:

```python
WITH_SKILL = SimpleNamespace(task_set="one_per_repo", skill="~/skills/daml")
```

A git URL works too, cloned at `skill_ref`. Use `skill_subdir` when `SKILL.md` is not at the top. Each task gets a fresh copy of the skill, and the run stores a snapshot of it. Everything the skill needs has to be inside its directory, because the agent can reach only the model provider and the allowed MCP servers.

## Sources kept elsewhere

The fetcher puts the repositories into `sources/` by default. To keep them elsewhere, fetch them there with `--sources-dir`:

```bash
uv run python -m daml_agent_benchmark.fetch_sources --sources-dir /data/daml-sources
```

Then point the harness at the same directory at the top of the experiment file:

```python
from pathlib import Path
from daml_agent_benchmark.locations import configure

configure(sources_root=Path("/data/daml-sources"))
```

## Machine sizing

Each running task has its own agent container and, while it is graded, a grading container. Memory is usually the limit. The default of one task per CPU core is too many for a laptop. On a Mac with 16 GB of memory, set `max_parallel_tasks=2`. On a larger machine, watch memory during the first run and raise the number from there.

## What a run does to Docker

The runner starts Docker if it is not running. When the run ends, it stops Docker only if the runner started it, and a Docker that was already running stays up. On macOS it opens and quits Docker Desktop. On Linux it runs `sudo systemctl start docker` and `sudo systemctl stop docker`. It builds its images the first time they are missing, and removes the benchmark's own containers and networks that are more than a day old.

Each task log contains the Codex error "could not find bubblewrap on PATH", which is expected and harmless because the container is the sandbox.
