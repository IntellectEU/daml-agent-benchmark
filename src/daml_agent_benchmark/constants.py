
from daml_agent_benchmark.egress_audit import normalize_domain
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
WRAPPER_PATH = PACKAGE_DIR / "codex_in_container.py"

# Codex Execution Constants
CODEX_BIN = str(WRAPPER_PATH)
CODEX_SANDBOX = "danger-full-access"
CODEX_APPROVAL_POLICY = "never"
CODEX_EXTRA_ARGS = ["--dangerously-bypass-approvals-and-sandbox"]
CODEX_SHOW_HEARTBEAT = True
CODEX_TIMEOUT_INTERRUPT_GRACE_SECONDS = 8.0
CODEX_POST_COMPLETION_WAIT_SECONDS = 4.0
CODEX_HEARTBEAT_INTERVAL_SECONDS = 10
CODEX_APP_SERVER_EXPERIMENTAL_API = True
CODEX_QUOTA_RETRY_MAX_RETRIES = 2
CODEX_QUOTA_RETRY_BASE_DELAY_SECONDS = 60.0
CODEX_QUOTA_RETRY_JITTER_SECONDS = 20.0
CODEX_QUOTA_RETRY_BACKOFF_MULTIPLIER = 1.0

# Repository copies: task copies of the source repositories live under this directory at the
# root; a copy's directory name is <repo>_____<task>, split on the marker.
REPO_COPIES_DIR_NAME = "tmp_repo_copies"
REPO_COPY_DIR_SPLITTER = "_____"

# Container Constants
CONTAINER_DOCKER_BIN = "docker"
CONTAINER_IMAGE = "daml-agent-eval:latest"
CONTAINER_DOCKERFILE = "docker/container_agent_eval.Dockerfile"  # relative to PACKAGE_DIR
CONTAINER_VERIFY_COMMANDS = ["codex", "daml", "direnv"]
CONTAINER_NETWORK_MODE = "bridge"
CONTAINER_PIDS_LIMIT = 1024
CONTAINER_DOCKER_INFO_TIMEOUT_SECONDS = 5
CONTAINER_DOCKER_START_TIMEOUT_SECONDS = 90
CONTAINER_DOCKER_START_STATUS_INTERVAL_SECONDS = 10
CONTAINER_CLEANUP_STALE_AFTER_SECONDS = 24 * 60 * 60
CONTAINER_EGRESS_PROXY_IMAGE = "daml-agent-egress-proxy:latest"
CONTAINER_EGRESS_PROXY_PORT = 3128
CONTAINER_EGRESS_PROXY_DOCKERFILE = "docker/container_egress_proxy.Dockerfile"
# The default model provider, OpenAI, and the domains its API needs. An experiment
# names another provider with the `provider_*` settings in config.py.
DEFAULT_PROVIDER_BASE_URL = "https://api.openai.com/v1"
CONTAINER_EGRESS_ALLOWED_DOMAINS = ["openai.com", "oaiusercontent.com"]
# Each task container gets its own --internal network carved from this pool, so
# concurrently running tasks cannot reach each other and proxy-log lines are
# attributable per task. Pick a range no other Docker network on the host uses.
CONTAINER_TASK_NETWORK_POOL = "10.219.0.0/16"
# A task network only ever holds the task container and the proxy, plus Docker's
# gateway, network and broadcast addresses, so /29 is the smallest size that works.
# /28 leaves a little headroom and still yields 4096 subnets for random selection.
CONTAINER_TASK_NETWORK_PREFIX_LEN = 28


# The default egress allowlist as squid and the audit compare it: normalised domain suffixes.
EGRESS_ALLOWED_SUFFIXES = [normalize_domain(d) for d in CONTAINER_EGRESS_ALLOWED_DOMAINS]
