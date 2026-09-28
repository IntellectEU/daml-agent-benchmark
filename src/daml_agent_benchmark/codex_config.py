"""The codex `config.toml` written into every task's private codex home.

The file is generated for every task from `CODEX_TASK_CONFIG` and the experiment's
provider and MCP settings. Nothing is copied from a per-machine codex home, so a fresh
checkout needs only the API key.
"""

from __future__ import annotations

import copy
import json
from urllib.parse import urlsplit

from daml_agent_benchmark.config import ExperimentConfig, shell_excluded_env_names
from daml_agent_benchmark.constants import DEFAULT_PROVIDER_BASE_URL

# Codex settings written to every task's config.toml.
#
# Transport: codex prefers a WebSocket to api.openai.com for the responses API. A
# WebSocket client resolves DNS itself instead of deferring to HTTP_PROXY, and task
# containers sit on an internal network whose only route out is the squid egress
# proxy, so the connection fails and the task times out having run nothing. The
# built-in `openai` provider cannot be modified, so an identical provider with the
# WebSocket preference off is declared and selected; its HTTPS transport honours
# HTTP_PROXY, keeping egress inside the proxy's allowlist.
#
# Retrieval and delegation: with a full-access sandbox codex's web search defaults
# to live search over the allowed OpenAI connection, which bypasses the egress
# allowlist entirely; browser, apps and plugins are further routes to outside
# content. Memories are off because state carried between tasks is a contamination
# channel. Everything else codex offers stays enabled: skills and image generation
# cannot reach outside content, and sub-agents (`multi_agent`, stable and on by
# default in codex) run in the same container under the same config, report every
# event and their token usage on the same connection, and are accounted per thread
# by the runner. `multi_agent_v2` is under development and withholds the parent
# turn's completion while helper threads exist, so it stays off.
#
# Key handling: the shell policy keeps the API key out of the agent's shell (codex
# ignores its own KEY/SECRET/TOKEN exclusion by default).
#
# This is the config for the default provider. `task_codex_config` swaps in the
# experiment's provider, its shell exclusions and its allowed MCP servers.
CODEX_TASK_CONFIG: dict[str, object] = {
    "model_provider": "openai-https",
    "web_search": "disabled",
    "check_for_update_on_startup": False,
    "model_providers": {
        "openai-https": {
            "name": "OpenAI (HTTPS transport, proxy-compatible)",
            "base_url": "https://api.openai.com/v1",
            "env_key": "OPENAI_API_KEY",
            "requires_openai_auth": True,
            "prefer_websockets": False,
        },
    },
    "shell_environment_policy": {
        "ignore_default_excludes": False,
        "exclude": ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"],
    },
    "features": {
        "in_app_browser": False,
        "browser_use": False,
        "browser_use_external": False,
        "browser_use_full_cdp_access": False,
        "computer_use": False,
        "standalone_web_search": False,
        "apps": False,
        "enable_mcp_apps": False,
        "plugins": False,
        "remote_plugin": False,
        "multi_agent": True,
        "multi_agent_v2": False,
        "memories": False,
    },
}


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {value!r}")


def _render_toml(document: dict, _path: tuple[str, ...] = ()) -> str:
    """Serialize a dict of scalars, lists and nested tables as TOML.

    Scalars of a table are written before its sub-tables, and sub-tables under dotted
    headers. Keys are bare names, which is all the task config uses.
    """
    lines: list[str] = []
    for key, value in document.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_scalar(value)}")
    for key, value in document.items():
        if isinstance(value, dict):
            table_path = (*_path, key)
            lines.append("")
            lines.append(f"[{'.'.join(table_path)}]")
            lines.append(_render_toml(value, table_path).rstrip("\n"))
    text = "\n".join(lines).strip()
    return text + "\n" if text else ""


# A provider other than the default one is declared under this id. The id is what codex
# reports as the thread's model provider.
CONFIGURED_PROVIDER_ID = "configured-provider"


def _model_provider(config: ExperimentConfig) -> tuple[str, dict[str, object]]:
    """The provider entry the task's codex selects, and its id.

    Every provider gets the HTTPS transport with the WebSocket preference off, for the
    reason given above `CODEX_TASK_CONFIG`.
    """
    if config.provider_base_url == DEFAULT_PROVIDER_BASE_URL:
        provider_id, name = "openai-https", "OpenAI (HTTPS transport, proxy-compatible)"
    else:
        host = urlsplit(config.provider_base_url).hostname
        provider_id, name = CONFIGURED_PROVIDER_ID, f"{host} (HTTPS transport, proxy-compatible)"
    return provider_id, {
        "name": name,
        "base_url": config.provider_base_url,
        "env_key": config.provider_api_key_env,
        "requires_openai_auth": config.provider_requires_openai_auth,
        "prefer_websockets": False,
    }


def task_codex_config(config: ExperimentConfig) -> dict[str, object]:
    """The codex settings a task of this experiment runs with."""
    document = copy.deepcopy(CODEX_TASK_CONFIG)
    provider_id, provider = _model_provider(config)
    document["model_provider"] = provider_id
    document["model_providers"] = {provider_id: provider}
    document["shell_environment_policy"]["exclude"] = shell_excluded_env_names(config)
    if config.mcp_servers:
        document["mcp_servers"] = {
            server.name: {"url": server.url}
            | ({"bearer_token_env_var": server.bearer_token_env} if server.bearer_token_env else {})
            for server in config.mcp_servers
        }
    return document


def task_codex_config_toml(config: ExperimentConfig) -> str:
    """The config.toml text every task's codex home receives."""
    return _render_toml(task_codex_config(config))
