"""The knobs of one experiment.

`ExperimentConfig` is the whole surface an experiment file sets: an experiment is a
`SimpleNamespace` of overrides merged onto `DEFAULTS`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields, replace
from enum import StrEnum
from types import SimpleNamespace
from urllib.parse import urlsplit

from daml_agent_benchmark.constants import CONTAINER_EGRESS_ALLOWED_DOMAINS, DEFAULT_PROVIDER_BASE_URL
from daml_agent_benchmark.egress_audit import domain_is_allowed, normalize_domain, normalize_host

# Environment variables that hold a codex credential whatever the provider. They stay
# out of the agent's shell even when the configured key is another one.
CODEX_CREDENTIAL_ENV_NAMES = ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"]
# The proxy tunnels HTTPS to port 443 and forwards nothing else.
_HTTPS_PORT = 443
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Codex accepts these characters in an MCP server name, and the name is a bare TOML key.
_MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class McpServer:
    """A remote MCP (Model Context Protocol) server the agent may call.

    Codex reaches it over streamable HTTP at `url`. When `bearer_token_env` is set, codex
    sends the value of that host environment variable as a bearer token.
    """

    name: str  # the key codex knows the server by, and the name its tool calls report
    url: str  # the server's https:// endpoint
    bearer_token_env: str | None = None  # the host environment variable that holds its token


class TaskKindName(StrEnum):
    IMPLEMENTATION = "implementation"  # the implementation is blanked and the agent writes it
    TEST_GENERATION = "test_generation"  # the test file is blanked and the agent writes tests


class TaskSet(StrEnum):
    ALL = "all"
    ONE_PER_REPO = "one_per_repo"


@dataclass
class ExperimentConfig:
    # fmt: off
    codex_model: str                    = "gpt-6-luna"
    codex_app_server_effort: str | None = None
    # The model provider. Codex talks to it through the OpenAI Responses API at this URL.
    provider_base_url: str              = DEFAULT_PROVIDER_BASE_URL
    provider_api_key_env: str           = "OPENAI_API_KEY"  # The host environment variable that holds the provider's API key.
    # Domains the egress proxy lets the agent reach. Each one covers its subdomains, and must cover the base URL's host.
    provider_allowed_domains: list[str] = field(default_factory=lambda: list(CONTAINER_EGRESS_ALLOWED_DOMAINS))
    provider_requires_openai_auth: bool = True  # Codex's OpenAI login flow; set False for any provider other than OpenAI.
    # Remote MCP servers the agent may call. The proxy allows each one's exact host, not its subdomains.
    # A call to any other MCP server is a security finding.
    mcp_servers: list[McpServer]        = field(default_factory=list)
    # A skill to install in every task's agent home: a local directory, or a git URL to clone.
    skill: str | None                   = None
    skill_ref: str                      = "main"  # Branch or tag to clone; ignored for a local directory.
    skill_subdir: str                   = ""  # Subdirectory holding SKILL.md, when it is not at the top.
    prompt_guidance_file: str | None    = None  # Markdown appended to every task prompt.
    task_docs_dir: str | None           = None  # Directory of Daml docs JSON files; the ones matching the task's SDK are copied into the copy.
    run_tasks_in_parallel: bool         = True
    max_parallel_tasks: int | None      = None
    task_set: TaskSet                   = TaskSet.ALL  # Ignored when `tasks` is set.
    tasks_per_repo: int | None          = None  # Cap on tasks taken from each repository; ignored when `tasks` is set.
    tasks: list[str] | None             = None
    ground_truth_control: bool          = False  # Build+test the unblanked copy before the agent runs; a failing ground truth is an infra error, not a model failure. Cached per copy content.
    max_task_runtime_seconds: int       = 120
    run_name: str | None                = None
    # What the agent writes: the implementation files, graded by the task's tests, or the test
    # file, graded by the mutants its tests catch. One kind per run.
    task_kind: TaskKindName             = TaskKindName.IMPLEMENTATION
    # For quick test-generation runs: grade only each task's first N mutants, in file order. A capped run is not a baseline.
    max_mutants_per_task: int | None    = None
    # fmt: on

    def __post_init__(self) -> None:
        # An experiment file names these as strings; an unknown one fails here.
        self.task_kind = TaskKindName(self.task_kind)
        self.task_set = TaskSet(self.task_set)

    def merge(self, *overrides: SimpleNamespace) -> "ExperimentConfig":
        """Merge experiment overrides, rejecting keys that are not experiment config fields."""
        valid_keys = {field.name for field in fields(self)}
        values = dict(self.__dict__)
        for namespace in overrides:
            for key, value in vars(namespace).items():
                if key not in valid_keys:
                    raise ValueError(f"Unknown experiment config key: {key}")
                values[key] = value
        return replace(self, **values)


def build_config_dump(config: ExperimentConfig) -> dict[str, object]:
    return asdict(config)


def _url_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def egress_allowed_suffixes(config: ExperimentConfig) -> list[str]:
    """The provider's domains, which the run's proxy allows together with their subdomains."""
    return list(dict.fromkeys(normalize_domain(d) for d in config.provider_allowed_domains if normalize_domain(d)))


def egress_allowed_hosts(config: ExperimentConfig) -> list[str]:
    """The hosts of the allowed MCP servers, which the run's proxy allows by exact name only."""
    hosts = (normalize_host(_url_host(server.url)) for server in config.mcp_servers)
    return list(dict.fromkeys(h for h in hosts if h))


def secret_env_names(config: ExperimentConfig) -> list[str]:
    """The host environment variables whose values go into the container as secrets.

    The provider's API key, and the bearer token of each allowed MCP server that has one.
    """
    names = [config.provider_api_key_env, *(s.bearer_token_env for s in config.mcp_servers if s.bearer_token_env)]
    return list(dict.fromkeys(names))


def shell_excluded_env_names(config: ExperimentConfig) -> list[str]:
    """The environment variables codex keeps out of the agent's shell: every secret, and codex's own credentials."""
    return list(dict.fromkeys([config.provider_api_key_env, *CODEX_CREDENTIAL_ENV_NAMES, *secret_env_names(config)]))


def _check_reachable_url(setting: str, url: str) -> str:
    """The host of `url`, after checking the proxy can forward a request to it.

    The proxy tunnels HTTPS to port 443 and nothing else, so the URL must be https://
    with no port or with port 443.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"{setting} must be an https:// URL with a host, got {url!r}.")
    try:
        port = parts.port or _HTTPS_PORT
    except ValueError as exc:
        raise ValueError(f"{setting} has an invalid port: {url!r}.") from exc
    if port != _HTTPS_PORT:
        raise ValueError(
            f"{setting} uses port {port}, but the egress proxy forwards HTTPS only to port {_HTTPS_PORT}: {url!r}."
        )
    return parts.hostname.lower()


def _check_env_name(setting: str, name: str | None) -> None:
    if not name or not _ENV_NAME_RE.match(name):
        raise ValueError(f"{setting} must name an environment variable, got {name!r}.")


def validate_provider_settings(config: ExperimentConfig) -> None:
    """Refuse a provider or MCP setup that the agent could not use.

    The agent's container reaches the internet only through the egress proxy, so the
    provider's host has to be under one of the allowed domains, and every URL has to
    be an https:// URL on port 443, which is the only one the proxy forwards.
    """
    host = _check_reachable_url("provider_base_url", config.provider_base_url)
    _check_env_name("provider_api_key_env", config.provider_api_key_env)
    if not config.provider_allowed_domains:
        raise ValueError("provider_allowed_domains is empty: the agent could not reach any model provider.")
    if not domain_is_allowed(host, config.provider_allowed_domains):
        raise ValueError(
            f"provider_base_url host {host!r} is not covered by provider_allowed_domains "
            f"{config.provider_allowed_domains}: the egress proxy would refuse every request to the model. "
            f"Add {host!r} or one of its parent domains to provider_allowed_domains."
        )
    names: set[str] = set()
    for server in config.mcp_servers:
        if not isinstance(server, McpServer):
            raise TypeError(f"mcp_servers entries must be McpServer values, got {server!r}.")
        if not _MCP_SERVER_NAME_RE.match(server.name):
            raise ValueError(f"MCP server name {server.name!r} may hold only letters, digits, '_' and '-'.")
        if server.name in names:
            raise ValueError(f"MCP server name {server.name!r} appears twice in mcp_servers.")
        names.add(server.name)
        _check_reachable_url(f"url of MCP server {server.name!r}", server.url)
        if server.bearer_token_env is not None:
            _check_env_name(f"bearer_token_env of MCP server {server.name!r}", server.bearer_token_env)


DEFAULTS = ExperimentConfig()
