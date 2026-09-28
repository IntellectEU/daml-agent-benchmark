"""Experiment configuration for the Daml agent benchmark.

Copy this file to `experiment.py` (ignored by git) and run

    uv run python -m daml_agent_benchmark.runner              # loads ./experiment.py
    uv run python -m daml_agent_benchmark.runner --config path/to/file.py

Each experiment is a SimpleNamespace of overrides for `ExperimentConfig` in
`daml_agent_benchmark.config`; `CURRENT_EXP` names the one to run, or `CURRENT_EXP_LIST`
runs several in sequence. A task is named by its id, `<repository>/<path to the test file
inside it>`, whichever directory the sources are checked out in.

Sources kept somewhere else? Fetch them there with
`uv run python -m daml_agent_benchmark.fetch_sources --sources-dir /data/daml-sources`,
and point the harness at them here, before the experiments:

    from pathlib import Path
    from daml_agent_benchmark.locations import configure
    configure(sources_root=Path("/data/daml-sources"))
"""

from types import SimpleNamespace

ALL_TASKS = SimpleNamespace(
    codex_model="gpt-6-luna",
    task_set="all",
    max_task_runtime_seconds=600,
    ground_truth_control=True,
)

ONE_TASK_PER_REPOSITORY = SimpleNamespace(
    codex_model="gpt-6-luna",
    task_set="one_per_repo",
    max_task_runtime_seconds=600,
)

SMOKE = SimpleNamespace(
    tasks=["ex-models/tic-tac-toe/daml/Test.daml"],
    max_task_runtime_seconds=300,
)

# Give the agent a skill. `skill` takes the directory the skill lives in, which is usually where
# you already keep it, and the run stores its own copy plus a zipped snapshot. A git URL works
# too, and is then cloned at `skill_ref`.
WITH_SKILL = SimpleNamespace(
    codex_model="gpt-6-luna",
    task_set="one_per_repo",
    skill="~/skills/daml",
    max_task_runtime_seconds=600,
)

CURRENT_EXP = SMOKE
