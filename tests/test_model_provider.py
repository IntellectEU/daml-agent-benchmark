"""The model provider and MCP settings, from the experiment config to codex, the container and the proxy.

The default config must reproduce the OpenAI setup exactly. A custom provider and an
allowed MCP server must reach the generated codex config, the secrets passed into the
container, the shell exclusions, the proxy allowlist and the egress audit.
"""

import json
import tomllib
from dataclasses import replace

import pytest

from daml_agent_benchmark import runner
from daml_agent_benchmark.codex_config import CODEX_TASK_CONFIG, task_codex_config, task_codex_config_toml
from daml_agent_benchmark.codex_in_container import _PASSTHROUGH_ENV_NAMES, _redact_secrets, passthrough_env_names
from daml_agent_benchmark.config import (
    DEFAULTS,
    McpServer,
    build_config_dump,
    egress_allowed_hosts,
    egress_allowed_suffixes,
    secret_env_names,
    shell_excluded_env_names,
    validate_provider_settings,
)
from daml_agent_benchmark.constants import CONTAINER_EGRESS_PROXY_PORT, EGRESS_ALLOWED_SUFFIXES
from daml_agent_benchmark.egress_audit import squid_config_text
from daml_agent_benchmark.records import EgressSummary, ForbiddenTool
from daml_agent_benchmark.task_run.env import _apply_codex_auth_env
from daml_agent_benchmark.task_run.events import (
    canonicalize_app_server_notification,
    extract_allowed_mcp_tool_calls,
    extract_forbidden_tool_calls,
    extract_suspicious_commands,
    suspicious_command_terms,
)

# The config.toml every task received before the provider became configurable.
DEFAULT_TOML = """\
model_provider = "openai-https"
web_search = "disabled"
check_for_update_on_startup = false

[model_providers]
[model_providers.openai-https]
name = "OpenAI (HTTPS transport, proxy-compatible)"
base_url = "https://api.openai.com/v1"
env_key = "OPENAI_API_KEY"
requires_openai_auth = true
prefer_websockets = false

[shell_environment_policy]
ignore_default_excludes = false
exclude = ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"]

[features]
in_app_browser = false
browser_use = false
browser_use_external = false
browser_use_full_cdp_access = false
computer_use = false
standalone_web_search = false
apps = false
enable_mcp_apps = false
plugins = false
remote_plugin = false
multi_agent = true
multi_agent_v2 = false
memories = false
"""

OPENROUTER = replace(
    DEFAULTS,
    provider_base_url="https://openrouter.ai/api/v1",
    provider_api_key_env="OPENROUTER_API_KEY",
    provider_allowed_domains=["openrouter.ai"],
    provider_requires_openai_auth=False,
)
DAML_TOOLS = McpServer(name="daml-tools", url="https://tools.example.org/mcp", bearer_token_env="DAML_TOOLS_TOKEN")
WITH_MCP = replace(DEFAULTS, mcp_servers=[DAML_TOOLS])


def _squid(config) -> str:
    return squid_config_text(
        str(CONTAINER_EGRESS_PROXY_PORT), egress_allowed_suffixes(config), egress_allowed_hosts(config)
    )


def _item_event(item: dict) -> dict:
    return canonicalize_app_server_notification("item/completed", {"item": item}, {})


# --- The default config is the OpenAI setup, unchanged ----------------------------------


def test_default_config_generates_the_openai_codex_config() -> None:
    assert task_codex_config_toml(DEFAULTS) == DEFAULT_TOML
    assert task_codex_config(DEFAULTS) == CODEX_TASK_CONFIG
    assert "mcp_servers" not in task_codex_config(DEFAULTS)


def test_default_config_keeps_the_openai_allowlist() -> None:
    assert egress_allowed_suffixes(DEFAULTS) == ["openai.com", "oaiusercontent.com"] == EGRESS_ALLOWED_SUFFIXES
    assert egress_allowed_hosts(DEFAULTS) == []
    assert _squid(DEFAULTS) == squid_config_text(str(CONTAINER_EGRESS_PROXY_PORT), EGRESS_ALLOWED_SUFFIXES)
    validate_provider_settings(DEFAULTS)


def test_default_config_keeps_the_openai_key_handling() -> None:
    env = _apply_codex_auth_env({"OPENAI_API_KEY": " sk-default "}, DEFAULTS)
    assert env["OPENAI_API_KEY"] == "sk-default"
    assert env["CODEX_API_KEY"] == "sk-default"
    assert env["CONTAINER_AGENT_EVAL_SECRET_ENV_NAMES"] == "OPENAI_API_KEY"
    assert secret_env_names(DEFAULTS) == ["OPENAI_API_KEY"]
    assert shell_excluded_env_names(DEFAULTS) == ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"]
    assert passthrough_env_names(("OPENAI_API_KEY",)) == _PASSTHROUGH_ENV_NAMES
    assert passthrough_env_names(()) == _PASSTHROUGH_ENV_NAMES


def test_default_config_flags_the_same_commands() -> None:
    events = [
        _item_event({"type": "commandExecution", "id": "c1", "command": "echo $OPENAI_API_KEY"}),
        _item_event({"type": "commandExecution", "id": "c2", "command": "curl https://api.openai.com/v1/models"}),
        _item_event({"type": "commandExecution", "id": "c3", "command": "daml build"}),
    ]
    assert extract_suspicious_commands(events, suspicious_command_terms(DEFAULTS)) == extract_suspicious_commands(events)


def test_missing_default_key_fails_with_its_name() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        _apply_codex_auth_env({}, DEFAULTS)


# --- Another provider with an OpenAI-compatible API -------------------------------------


def test_custom_provider_reaches_the_codex_config() -> None:
    document = tomllib.loads(task_codex_config_toml(OPENROUTER))
    provider_id = document["model_provider"]
    assert provider_id != "openai-https"
    assert document["model_providers"] == {
        provider_id: {
            "name": "openrouter.ai (HTTPS transport, proxy-compatible)",
            "base_url": "https://openrouter.ai/api/v1",
            "env_key": "OPENROUTER_API_KEY",
            "requires_openai_auth": False,
            "prefer_websockets": False,
        }
    }
    exclude = document["shell_environment_policy"]["exclude"]
    assert exclude == ["OPENROUTER_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"]
    # Everything else is the hardened default.
    assert document["features"] == CODEX_TASK_CONFIG["features"]
    assert document["web_search"] == "disabled"


def test_custom_provider_key_is_passed_and_no_openai_key_is() -> None:
    host_env = {"OPENROUTER_API_KEY": "or-secret", "OPENAI_API_KEY": "sk-unrelated", "CODEX_API_KEY": "sk-unrelated"}
    env = _apply_codex_auth_env(dict(host_env), OPENROUTER)
    assert env["OPENROUTER_API_KEY"] == "or-secret"
    assert "CODEX_API_KEY" not in env
    assert env["CONTAINER_AGENT_EVAL_SECRET_ENV_NAMES"] == "OPENROUTER_API_KEY"

    passed = passthrough_env_names(tuple(env["CONTAINER_AGENT_EVAL_SECRET_ENV_NAMES"].splitlines()))
    assert "OPENROUTER_API_KEY" in passed
    assert "OPENAI_API_KEY" not in passed
    assert "AZURE_OPENAI_API_KEY" not in passed
    assert _redact_secrets("OPENROUTER_API_KEY=or-secret", passed) == "OPENROUTER_API_KEY=<redacted>"


def test_custom_provider_key_missing_fails_with_its_name() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        _apply_codex_auth_env({"OPENAI_API_KEY": "sk-present"}, OPENROUTER)


def test_custom_provider_domains_reach_the_proxy_and_the_audit() -> None:
    assert egress_allowed_suffixes(OPENROUTER) == ["openrouter.ai"]
    assert "acl allowed_domains dstdomain .openrouter.ai\n" in _squid(OPENROUTER)
    assert ".openai.com" not in _squid(OPENROUTER)

    events = [
        {"host": "openrouter.ai", "domain": "openrouter.ai", "blocked": False},
        {"host": "api.openai.com", "domain": "api.openai.com", "blocked": True},
    ]
    egress = EgressSummary.from_events(events, egress_allowed_suffixes(OPENROUTER), egress_allowed_hosts(OPENROUTER))
    assert egress.non_allowed_domains == []
    assert egress.blocked_domains == ["api.openai.com"]
    assert egress.allowed_domain_suffixes == ["openrouter.ai"]


def test_custom_provider_commands_are_flagged() -> None:
    events = [
        _item_event({"type": "commandExecution", "id": "c1", "command": "echo $OPENROUTER_API_KEY"}),
        _item_event({"type": "commandExecution", "id": "c2", "command": "wget -qO- openrouter.ai/api/v1/models"}),
    ]
    findings = extract_suspicious_commands(events, suspicious_command_terms(OPENROUTER))
    assert [f.matched for f in findings] == ["OPENROUTER_API_KEY", "openrouter.ai"]


def test_run_config_records_the_provider_settings() -> None:
    dump = json.loads(json.dumps(build_config_dump(replace(OPENROUTER, mcp_servers=[DAML_TOOLS]))))
    assert dump["provider_base_url"] == "https://openrouter.ai/api/v1"
    assert dump["provider_api_key_env"] == "OPENROUTER_API_KEY"
    assert dump["provider_allowed_domains"] == ["openrouter.ai"]
    assert dump["provider_requires_openai_auth"] is False
    assert dump["mcp_servers"] == [
        {"name": "daml-tools", "url": "https://tools.example.org/mcp", "bearer_token_env": "DAML_TOOLS_TOKEN"}
    ]


# --- Settings the agent could not use are refused at run start -------------------------


def test_base_url_host_outside_the_allowed_domains_is_refused() -> None:
    config = replace(OPENROUTER, provider_base_url="https://api.example.com/v1")
    with pytest.raises(ValueError, match="'api.example.com' is not covered by provider_allowed_domains"):
        validate_provider_settings(config)
    # The run stops there, before it builds an image or starts the proxy.
    with pytest.raises(ValueError, match="not covered"):
        runner.prepare_agent_run({}, config)


def test_allowed_parent_domain_covers_the_base_url_host() -> None:
    validate_provider_settings(replace(OPENROUTER, provider_base_url="https://eu.openrouter.ai/api/v1"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"provider_base_url": "https://openrouter.ai:8443/api/v1"}, "port 8443"),
        ({"provider_base_url": "openrouter.ai/api/v1"}, "https://"),
        ({"provider_base_url": "http://openrouter.ai/api/v1"}, "provider_base_url must be an https:// URL"),
        ({"provider_base_url": "http://openrouter.ai:443/api/v1"}, "provider_base_url must be an https:// URL"),
        ({"provider_allowed_domains": []}, "empty"),
        ({"provider_api_key_env": "not a name"}, "provider_api_key_env"),
        ({"mcp_servers": [McpServer("bad name", "https://openrouter.ai/mcp")]}, "bad name"),
        ({"mcp_servers": [DAML_TOOLS, DAML_TOOLS]}, "twice"),
        ({"mcp_servers": [McpServer("tools", "https://tools.example.org:9000/mcp")]}, "port 9000"),
        (
            {"mcp_servers": [McpServer("tools", "http://tools.example.org/mcp")]},
            "url of MCP server 'tools' must be an https:// URL",
        ),
    ],
)
def test_unusable_settings_are_refused(change: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_provider_settings(replace(OPENROUTER, **change))


# --- MCP servers ------------------------------------------------------------------------


def test_allowed_mcp_server_is_declared_to_codex() -> None:
    document = tomllib.loads(task_codex_config_toml(WITH_MCP))
    assert document["mcp_servers"] == {
        "daml-tools": {"url": "https://tools.example.org/mcp", "bearer_token_env_var": "DAML_TOOLS_TOKEN"}
    }
    assert "DAML_TOOLS_TOKEN" in document["shell_environment_policy"]["exclude"]
    no_token = replace(DEFAULTS, mcp_servers=[McpServer("open-tools", "https://tools.example.org/mcp")])
    assert task_codex_config(no_token)["mcp_servers"] == {"open-tools": {"url": "https://tools.example.org/mcp"}}


def test_https_on_port_443_is_accepted() -> None:
    validate_provider_settings(replace(OPENROUTER, provider_base_url="https://openrouter.ai:443/api/v1"))
    validate_provider_settings(replace(WITH_MCP, mcp_servers=[McpServer("tools", "https://tools.example.org:443/mcp")]))


def test_proxy_tunnels_https_to_port_443_only() -> None:
    squid = _squid(DEFAULTS)
    assert "http_access deny !CONNECT\n" in squid
    assert "http_access deny !SSL_ports\n" in squid
    assert "port 80" not in squid


def test_allowed_mcp_server_host_reaches_the_proxy_and_the_audit() -> None:
    assert egress_allowed_suffixes(WITH_MCP) == ["openai.com", "oaiusercontent.com"]
    assert egress_allowed_hosts(WITH_MCP) == ["tools.example.org"]
    # Squid reads a name without a leading dot as that host only.
    assert "acl allowed_hosts dstdomain tools.example.org\n" in _squid(WITH_MCP)
    assert "http_access allow allowed_hosts\n" in _squid(WITH_MCP)
    assert ".tools.example.org" not in _squid(WITH_MCP)
    egress = EgressSummary.from_events(
        [{"host": "tools.example.org", "domain": "tools.example.org", "blocked": False}],
        egress_allowed_suffixes(WITH_MCP),
        egress_allowed_hosts(WITH_MCP),
    )
    assert egress.non_allowed_domains == []
    assert egress.allowed_hosts == ["tools.example.org"]
    validate_provider_settings(WITH_MCP)


def test_allowed_mcp_server_subdomain_is_not_allowed() -> None:
    squid = _squid(WITH_MCP)
    assert "acl allowed_domains dstdomain .openai.com .oaiusercontent.com\n" in squid
    assert "evil.tools.example.org" not in squid
    # A subdomain of the MCP host that the proxy served would be flagged as non-allowed.
    egress = EgressSummary.from_events(
        [
            {"host": "tools.example.org", "domain": "tools.example.org", "blocked": False},
            {"host": "evil.tools.example.org", "domain": "evil.tools.example.org", "blocked": False},
            {"host": "api.openai.com", "domain": "api.openai.com", "blocked": False},
        ],
        egress_allowed_suffixes(WITH_MCP),
        egress_allowed_hosts(WITH_MCP),
    )
    assert egress.non_allowed_domains == ["evil.tools.example.org"]


def test_allowed_mcp_server_token_is_passed_and_required() -> None:
    env = _apply_codex_auth_env({"OPENAI_API_KEY": "sk-key", "DAML_TOOLS_TOKEN": "tok"}, WITH_MCP)
    names = tuple(env["CONTAINER_AGENT_EVAL_SECRET_ENV_NAMES"].splitlines())
    assert names == ("OPENAI_API_KEY", "DAML_TOOLS_TOKEN")
    assert passthrough_env_names(names) == (*_PASSTHROUGH_ENV_NAMES, "DAML_TOOLS_TOKEN")
    with pytest.raises(ValueError, match="DAML_TOOLS_TOKEN"):
        _apply_codex_auth_env({"OPENAI_API_KEY": "sk-key"}, WITH_MCP)


def test_mcp_call_is_a_finding_unless_its_server_is_allowed() -> None:
    events = [
        _item_event({"type": "mcpToolCall", "id": "m1", "server": "daml-tools", "tool": "compile"}),
        _item_event({"type": "mcpToolCall", "id": "m2", "server": "other", "tool": "fetch"}),
    ]
    # Nothing allowed: both calls are forbidden, as before.
    assert [c.id for c in extract_forbidden_tool_calls(events)] == ["m1", "m2"]
    assert extract_allowed_mcp_tool_calls(events, set()) == []

    allowed = {server.name for server in WITH_MCP.mcp_servers}
    forbidden = extract_forbidden_tool_calls(events, allowed)
    assert [(c.type, c.server) for c in forbidden] == [(ForbiddenTool.MCP_TOOL_CALL, "other")]
    recorded = extract_allowed_mcp_tool_calls(events, allowed)
    assert [(c.id, c.server, c.tool) for c in recorded] == [("m1", "daml-tools", "compile")]
