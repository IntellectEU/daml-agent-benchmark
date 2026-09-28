"""The command and the environment an attempt runs with.

Which codex binary, what is on PATH, how the API key reaches the process, and
which extra environment a repository's tasks need.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import MutableMapping, TypeVar

from daml_agent_benchmark.config import (
    ExperimentConfig,
    egress_allowed_hosts,
    egress_allowed_suffixes,
    secret_env_names,
)
from daml_agent_benchmark.constants import (
    CODEX_AUTH_MODE,
    CODEX_BIN,
    CODEX_EXTRA_ARGS,
    CONTAINER_DOCKER_BIN,
    CONTAINER_IMAGE,
    CONTAINER_PIDS_LIMIT,
    WRAPPER_PATH,
)
from daml_agent_benchmark.repos.build import repo_name_for_package_path
from daml_agent_benchmark.repos.envrc import capture_envrc_environment
from daml_agent_benchmark.repos.registry import handler_for
from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.task_run.inputs import configure_copyback_rel_paths_env

EnvMap = TypeVar("EnvMap", bound=MutableMapping[str, str])


def configure_wrapper_environment(shared_proxy_state: dict) -> None:
    """Export container settings as env vars so the Python container wrapper can pick them up."""
    os.environ["CONTAINER_AGENT_EVAL_DOCKER_BIN"] = CONTAINER_DOCKER_BIN
    os.environ["CONTAINER_AGENT_EVAL_IMAGE"] = CONTAINER_IMAGE
    os.environ["CONTAINER_AGENT_EVAL_PROJECT_ROOT"] = str(locations.root)
    os.environ["CONTAINER_AGENT_EVAL_PIDS_LIMIT"] = str(CONTAINER_PIDS_LIMIT)
    os.environ["CONTAINER_AGENT_EVAL_CONTAINER_UID"] = str(os.getuid())
    os.environ["CONTAINER_AGENT_EVAL_CONTAINER_GID"] = str(os.getgid())

    # The runner owns the run's proxy; the wrapper creates a private network per
    # task and attaches this proxy to it.
    os.environ["CONTAINER_AGENT_EVAL_EGRESS_PROXY_CONTAINER"] = shared_proxy_state["proxy_container_id"]
    os.environ["CONTAINER_AGENT_EVAL_EGRESS_ACCESS_LOG"] = shared_proxy_state["access_log_path"]


def prepend_common_tool_paths(env: EnvMap) -> EnvMap:
    """Ensure daml, homebrew, and nix binaries are on PATH regardless of how the shell
    was started (e.g. cron, venv, or non-login shell may not source the user's profile)."""
    entries = []
    daml_path = str((Path.home() / ".daml" / "bin"))
    homebrew_path = "/opt/homebrew/bin"
    usr_local_bin = "/usr/local/bin"
    nix_global_profile_bin = "/nix/var/nix/profiles/default/bin"
    nix_user_profile_bin = f"/etc/profiles/per-user/{os.environ.get('USER', '')}/bin"
    if os.path.isdir(daml_path):
        entries.append(daml_path)
    if os.path.isdir(homebrew_path):
        entries.append(homebrew_path)
    if os.path.isdir(usr_local_bin):
        entries.append(usr_local_bin)
    if os.path.isdir(nix_global_profile_bin):
        entries.append(nix_global_profile_bin)
    if os.path.isdir(nix_user_profile_bin):
        entries.append(nix_user_profile_bin)

    existing = env.get("PATH", "")
    if existing:
        entries.extend(existing.split(os.pathsep))

    deduped = []
    seen = set()
    for entry in entries:
        if not entry or entry in seen:
            continue
        deduped.append(entry)
        seen.add(entry)
    env["PATH"] = os.pathsep.join(deduped)
    return env


def _assert_safe_codex_settings() -> None:
    """Check the codex settings suit running inside a container.

    Dangerous mode is required here: the container is the safety boundary, so codex's own
    approval prompts and sandbox are bypassed inside it.
    """
    lower_args = [str(a).lower() for a in CODEX_EXTRA_ARGS]
    if "--dangerously-bypass-approvals-and-sandbox" not in lower_args:
        raise ValueError("CODEX_EXTRA_ARGS must include --dangerously-bypass-approvals-and-sandbox")


def resolve_codex_bin(codex_bin: str) -> str:
    """The codex runner as an executable path. `CODEX_BIN` is the absolute path of the
    container wrapper, so there is nothing to look up."""
    path = Path(codex_bin).expanduser()
    if not (path.is_absolute() and path.exists() and os.access(path, os.X_OK)):
        raise FileNotFoundError(f"Configured CODEX_BIN not executable: {path}")
    return str(path)


def is_container_codex_runner(codex_bin: str) -> bool:
    """Return True when Codex is launched via the container wrapper script."""
    try:
        resolved = Path(codex_bin).expanduser().resolve()
    except Exception:
        resolved = Path(codex_bin).expanduser()
    return resolved == WRAPPER_PATH or resolved.name == WRAPPER_PATH.name


def _apply_codex_auth_env(env: dict[str, str], config: ExperimentConfig) -> dict[str, str]:
    """Inject authentication credentials into the environment dict that will be passed
    to the codex subprocess.

    The provider's API key is read from the host variable `provider_api_key_env`, and
    each allowed MCP server's token from its `bearer_token_env`. A missing one fails
    the run before any task starts.
    """
    auth_mode = CODEX_AUTH_MODE
    if auth_mode == "chatgpt":
        return env
    if auth_mode == "api_key":
        key_env = config.provider_api_key_env
        api_key = (env.get(key_env) or "").strip()
        if not api_key:
            raise ValueError(
                f"The model provider's API key is missing: set the environment variable {key_env} "
                f"(named by the experiment setting provider_api_key_env) before starting the run."
            )
        env[key_env] = api_key
        if config.provider_requires_openai_auth:
            # Codex authenticates from CODEX_API_KEY in its environment, so no auth.json
            # has to be placed where the agent can read it. The key's own variable stays
            # set for the proxy-compatible model provider, whose env_key names it.
            env["CODEX_API_KEY"] = api_key
        else:
            # Codex reads the key from the provider's env_key alone, so no copy of it,
            # nor any other codex credential, goes into the container.
            env.pop("CODEX_API_KEY", None)
        for server in config.mcp_servers:
            if server.bearer_token_env and not (env.get(server.bearer_token_env) or "").strip():
                raise ValueError(
                    f"The token of MCP server {server.name!r} is missing: set the environment variable "
                    f"{server.bearer_token_env} before starting the run."
                )
        # The container wrapper passes exactly these secrets into the container.
        env["CONTAINER_AGENT_EVAL_SECRET_ENV_NAMES"] = "\n".join(secret_env_names(config))
        return env
    raise ValueError(f"Unsupported CODEX_AUTH_MODE={auth_mode!r}; expected 'api_key' or 'chatgpt'")


def _build_codex_env(codex_bin: str, config: ExperimentConfig) -> dict[str, str]:
    """Build the full environment dict for the codex subprocess. Ensures codex's own
    directory and common tool paths are all on PATH, then applies auth credentials."""
    env = prepend_common_tool_paths(os.environ.copy())

    # Put codex's own bin dir on PATH
    path_entries = []
    codex_path = Path(codex_bin).expanduser()
    codex_dir = str(codex_path.parent)
    path_entries.append(codex_dir)
    resolved_codex_dir = str(codex_path.resolve().parent)
    if resolved_codex_dir != codex_dir:
        path_entries.append(resolved_codex_dir)

    existing = env.get("PATH", "")
    if existing:
        path_entries.extend(existing.split(os.pathsep))

    deduped_entries = []
    seen = set()
    for entry in path_entries:
        if not entry or entry in seen:
            continue
        deduped_entries.append(entry)
        seen.add(entry)

    env["PATH"] = os.pathsep.join(deduped_entries)
    env = _apply_codex_auth_env(env, config)
    return env


def ensure_codex_auth_ready(codex_bin: str, config: ExperimentConfig) -> None:
    """Fail the run early unless the API key codex needs, and every MCP token, is present.

    There is no terminal inside Docker to log in with, so the key must already be in
    the environment; it reaches codex from there. Nothing else about auth is set up
    at run level: each task's codex home is generated from code and holds no
    credential file.
    """
    if CODEX_AUTH_MODE != "api_key":
        raise ValueError("CODEX_AUTH_MODE must be 'api_key': a task runs non-interactively.")
    _build_codex_env(codex_bin, config)


def task_container_env(repo_copy_root: str) -> dict[str, str]:
    """All extra env vars a task's containers need (agent and eval alike).

    Combines the environment the copy's own `.envrc` declares with any extras the
    repository needs, such as canton's `DAML_VERSION`. The `.envrc` is captured by running
    direnv on the host, because direnv cannot run inside the container. This reads the copy
    before the agent has touched it.
    """
    env_map = capture_envrc_environment(repo_copy_root)
    repo_name = repo_name_for_package_path(repo_copy_root)
    if repo_name is not None:
        env_map.update(handler_for(repo_name).container_env)
    return env_map


def prepare_codex_invocation(
    config: ExperimentConfig, copyback_rel_paths: list[str], repo_copy_root: str, codex_home_override: str
) -> tuple[str, dict[str, str]]:
    """Shared per-task codex setup: resolve the binary and build its environment
    (copyback list, container env vars for the copy, codex home)."""
    _assert_safe_codex_settings()
    codex_bin = resolve_codex_bin(CODEX_BIN)
    codex_env = _build_codex_env(codex_bin, config)
    # For the wrapper's log line only; the proxy's own configuration is what enforces the allowlist.
    codex_env["CONTAINER_AGENT_EVAL_EGRESS_ALLOWED_DOMAINS"] = ",".join(
        [*egress_allowed_suffixes(config), *egress_allowed_hosts(config)]
    )
    configure_copyback_rel_paths_env(codex_env, copyback_rel_paths)
    env_map = task_container_env(repo_copy_root)
    if env_map:
        codex_env["CONTAINER_AGENT_EVAL_EXTRA_ENV_JSON"] = json.dumps(env_map)
    codex_env["CODEX_HOME"] = codex_home_override
    return codex_bin, codex_env


def resolve_container_export_timeout_seconds(task_timeout_seconds: int) -> float:
    """Resolve app-server container export timeout (auto mode).
    Export window scales with task runtime and stays bounded.
    """
    return min(120.0, max(20.0, float(task_timeout_seconds) * 0.5))
