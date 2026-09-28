from __future__ import annotations

from uuid import uuid4


def default_agent_run_name(model_name: str, task_selection: str) -> str:
    model = str(model_name or "").strip()
    selection = str(task_selection or "").strip()
    if model and selection:
        return f"{model}_{selection}"
    if model:
        return model
    if selection:
        return selection
    return "run"


def build_uuid_run_identity(
    configured_run_name: str | None,
    default_run_name: str,
) -> tuple[str, str]:
    configured = str(configured_run_name or "").strip()
    run_name = configured or str(default_run_name or "").strip() or "run"
    run_id = str(uuid4())
    return run_id, run_name
