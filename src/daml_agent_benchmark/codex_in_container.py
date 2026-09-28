#!/usr/bin/env python3
"""Runs Codex in a container, from the host, with copy-in/copy-back isolation.

This is the host side. It drives docker and never runs in the container itself; the
process inside is `docker/codex_diff_wrapper.py`, which this copies in and starts.

- parse codex args and rewrite host working directory to /workspace in-container
- create container without mounting the task's repository copy
- copy the task's repository copy into the container
- run codex via the in-container diff wrapper
- copy the configured implementation files back to host (never the full workspace)
- clean up container/proxy/network resources on exit/signals

Every task gets its own `--internal` Docker network with a unique subnet. The
egress proxy joins that network under the alias `egress-proxy`, so a task can
reach the proxy and nothing else: not the host, not the internet, and not the
containers of concurrently running tasks. The unique subnet also lets the wrapper
attribute lines of the (possibly shared) proxy access log to this task.

After the run the wrapper emits machine-readable audit lines on stderr for the
orchestrator: `[egress-event] {json}` per proxied request attributed to this
task, and `[workspace-change] {json}` per file the agent added, modified or
deleted in its workspace outside build-output directories.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import shlex
import signal
import subprocess
import sys
import random
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    # Run as a script by its path, so the package it belongs to has to be put on the
    # path: this file is `<src>/daml_agent_benchmark/codex_in_container.py`.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daml_agent_benchmark.constants import (
    CONTAINER_EGRESS_ALLOWED_DOMAINS,
    CONTAINER_EGRESS_PROXY_PORT,
    CONTAINER_TASK_NETWORK_POOL,
    CONTAINER_TASK_NETWORK_PREFIX_LEN,
)
from daml_agent_benchmark.docker_cleanup import RUNNER_DOCKER_LABEL, RUNNER_DOCKER_LABEL_VALUE
from daml_agent_benchmark.egress_audit import parse_squid_access_log_text
from daml_agent_benchmark.workspace_audit import diff_tree_hashes, hash_tree

CONTAINER_WORK_DIR = "/workspace"
_DIFF_WRAPPER_PATH = "/opt/codex_diff_wrapper.py"
_PASSTHROUGH_ENV_NAMES = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_API_TYPE",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "CODEX_HOME",
)
# The OpenAI credentials among the names above. They go into the container only when
# the configured provider key is OPENAI_API_KEY.
_OPENAI_SECRET_ENV_NAMES = ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY")
_EGRESS_PROXY_ALIAS = "egress-proxy"
_TASK_NETWORK_CREATE_ATTEMPTS = 25
_SECRET_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_]*(?:KEY|TOKEN|SECRET|PASSWORD))=(.+)$", re.DOTALL)
_OPENAI_KEY_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}")


def _redact_secrets(text: str, secret_env_names: tuple[str, ...] = ()) -> str:
    """Mask secret values in a command argument before it is logged."""
    name = text.split("=", 1)[0]
    if "=" in text and name in secret_env_names:
        return f"{name}=<redacted>"
    match = _SECRET_ENV_ASSIGNMENT_RE.match(text)
    if match is not None:
        return f"{match.group(1)}=<redacted>"
    return _OPENAI_KEY_RE.sub("<redacted>", text)


def passthrough_env_names(secret_env_names: tuple[str, ...]) -> tuple[str, ...]:
    """The host environment variables that go into the task container.

    The secrets the runner names (the provider's API key and any MCP tokens) replace
    OpenAI's credentials, so a container for another provider gets no OpenAI key.
    With no names given, or with OPENAI_API_KEY as the provider key, the list is the
    OpenAI one.
    """
    if not secret_env_names or secret_env_names[0] == "OPENAI_API_KEY":
        base = _PASSTHROUGH_ENV_NAMES
    else:
        base = tuple(n for n in _PASSTHROUGH_ENV_NAMES if n not in _OPENAI_SECRET_ENV_NAMES)
    return tuple(dict.fromkeys([*base, *secret_env_names]))


class WrapperError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = int(exit_code)


@dataclass
class RuntimeState:
    container_id: str = ""
    task_network: str = ""
    task_subnet: str = ""
    proxy_attached_to_task_network: bool = False
    access_log_offset: int = 0
    workspace_hashes_before: dict[str, str] | None = None


class _ContainerCodexWrapper:
    def __init__(self, argv: list[str]):
        if not argv:
            raise WrapperError("Usage: codex_in_container.py <codex args...>", exit_code=2)

        self.script_dir = Path(__file__).resolve().parent
        self.project_root_default = self.script_dir.parent

        self.docker_bin = os.environ.get("CONTAINER_AGENT_EVAL_DOCKER_BIN", "docker")
        self.project_root = os.environ.get("CONTAINER_AGENT_EVAL_PROJECT_ROOT", str(self.project_root_default))
        self.image = os.environ.get("CONTAINER_AGENT_EVAL_IMAGE", "daml-agent-eval:latest")
        self.pids_limit = os.environ.get("CONTAINER_AGENT_EVAL_PIDS_LIMIT", "1024")
        self.container_uid = os.environ.get("CONTAINER_AGENT_EVAL_CONTAINER_UID", "")
        self.container_gid = os.environ.get("CONTAINER_AGENT_EVAL_CONTAINER_GID", "")
        self.proxy_container = os.environ.get("CONTAINER_AGENT_EVAL_EGRESS_PROXY_CONTAINER", "")
        self.proxy_access_log = os.environ.get("CONTAINER_AGENT_EVAL_EGRESS_ACCESS_LOG", "")
        self.copyback_rel_paths_raw = os.environ.get("CONTAINER_AGENT_EVAL_COPYBACK_REL_PATHS", "")
        self.secret_env_names = tuple(
            n for n in os.environ.get("CONTAINER_AGENT_EVAL_SECRET_ENV_NAMES", "").splitlines() if n
        )
        self.egress_allowed_domains = os.environ.get(
            "CONTAINER_AGENT_EVAL_EGRESS_ALLOWED_DOMAINS", ",".join(CONTAINER_EGRESS_ALLOWED_DOMAINS)
        )
        self.args = list(argv)
        self.work_dir = self.project_root
        self.needs_stdin = any(arg == "app-server" for arg in self.args)

        self.state = RuntimeState()

        self._parse_work_dir_from_args()
        self._validate_initial_state()
        self._log(
            "initialized "
            f"work_dir={self.work_dir!r} needs_stdin={self.needs_stdin} "
            f"copyback_paths_configured={bool(self.copyback_rel_paths_raw)}"
        )

    def _parse_work_dir_from_args(self) -> None:
        i = 0
        while i < len(self.args):
            arg = self.args[i]
            if arg in {"-C", "--cd"}:
                if i + 1 < len(self.args):
                    self.work_dir = self.args[i + 1]
                    self.args[i + 1] = CONTAINER_WORK_DIR
                i += 1
                continue
            if arg.startswith("--cd="):
                self.work_dir = arg.split("=", 1)[1]
                self.args[i] = f"--cd={CONTAINER_WORK_DIR}"
            i += 1

    def _validate_initial_state(self) -> None:
        if not shutil.which(self.docker_bin):
            raise WrapperError(f"docker binary not found: {self.docker_bin}", exit_code=127)
        if not Path(self.work_dir).is_dir():
            raise WrapperError(f"working directory does not exist: {self.work_dir}", exit_code=2)
        if not self.copyback_rel_paths_raw.strip():
            raise WrapperError(
                "CONTAINER_AGENT_EVAL_COPYBACK_REL_PATHS is not set. Refusing to run: without an "
                "explicit impl-file list there is nothing safe to copy back (full-workspace "
                "copyback is forbidden — it would let agent edits to test files overwrite the "
                "host's copy).",
                exit_code=2,
            )
        if not (self.proxy_container and self.proxy_access_log):
            raise WrapperError(
                "CONTAINER_AGENT_EVAL_EGRESS_PROXY_CONTAINER and CONTAINER_AGENT_EVAL_EGRESS_ACCESS_LOG must be set: "
                "the run's proxy is started by the runner.",
                exit_code=2,
            )

    def _format_cmd(self, cmd: list[str]) -> str:
        # Redact secrets: these formatted commands land verbatim in per-task logs
        # (e.g. the `docker create --env OPENAI_API_KEY=...` line).
        return " ".join(shlex.quote(_redact_secrets(part, self.secret_env_names)) for part in cmd)

    def _log(self, message: str) -> None:
        print(f"[wrapper {time.time():.3f}] {message}", file=sys.stderr, flush=True)

    def _run_quiet(self, cmd: list[str], *, step: str = "") -> int:
        step_prefix = f"{step} " if step else ""
        self._log(f"{step_prefix}start: {self._format_cmd(cmd)}")
        started = time.time()
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        elapsed = time.time() - started
        self._log(f"{step_prefix}end rc={proc.returncode} elapsed={elapsed:.2f}s")
        return int(proc.returncode)

    def _run_checked(
        self, cmd: list[str], *, capture_output: bool = False, text: bool = False, step: str = ""
    ) -> subprocess.CompletedProcess:
        step_prefix = f"{step} " if step else ""
        self._log(f"{step_prefix}start: {self._format_cmd(cmd)}")
        started = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=capture_output, text=text, check=True)
        except subprocess.CalledProcessError as exc:
            elapsed = time.time() - started
            self._log(f"{step_prefix}failed rc={exc.returncode} elapsed={elapsed:.2f}s")
            raise
        elapsed = time.time() - started
        self._log(f"{step_prefix}end rc={proc.returncode} elapsed={elapsed:.2f}s")
        return proc

    # ------------------------------------------------------------------
    # Per-task network and proxy attachment
    # ------------------------------------------------------------------

    def _random_task_subnet(self) -> str:
        pool = ipaddress.ip_network(CONTAINER_TASK_NETWORK_POOL, strict=False)
        prefix_len = CONTAINER_TASK_NETWORK_PREFIX_LEN
        subnet_count = 2 ** (prefix_len - pool.prefixlen)
        index = random.randrange(subnet_count)
        subnet_size = 2 ** (32 - prefix_len)
        network_address = pool.network_address + index * subnet_size
        return str(ipaddress.ip_network((network_address, prefix_len)))

    def _create_task_network(self) -> None:
        """Create this task's private internal network with an explicit, unique subnet.

        Explicit subnets keep the number of concurrent task networks independent of
        Docker's small default address pool and make the client address in the proxy
        log unique among the tasks running at any moment. The isolated gateway mode
        removes the bridge gateway route, so the container cannot reach the Docker
        host either; Docker engines without that option get the plain internal network.
        """
        suffix = f"{int(time.time())}-{os.getpid()}-{random.randint(0, 999_999)}"
        network_name = f"daml-agent-task-{suffix}"
        gateway_opts = ["--opt", "com.docker.network.bridge.gateway_mode_ipv4=isolated"]
        last_error = ""
        for _attempt in range(_TASK_NETWORK_CREATE_ATTEMPTS):
            subnet = self._random_task_subnet()
            cmd = [
                self.docker_bin,
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                *gateway_opts,
                "--subnet",
                subnet,
                "--label",
                f"{RUNNER_DOCKER_LABEL}={RUNNER_DOCKER_LABEL_VALUE}",
                network_name,
            ]
            self._log(f"egress.network_create start: {self._format_cmd(cmd)}")
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                self.state.task_network = network_name
                self.state.task_subnet = subnet
                self._log(f"egress.network_create end rc=0 network={network_name} subnet={subnet}")
                print(
                    f"[task-network] name={network_name} subnet={subnet} gateway_isolated={bool(gateway_opts)}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            last_error = (proc.stderr or proc.stdout or "").strip()
            self._log(f"egress.network_create failed rc={proc.returncode}: {last_error[-300:]}")
            lowered = last_error.lower()
            if gateway_opts and "gateway_mode" in lowered:
                gateway_opts = []
                continue
            if "overlap" not in lowered and "already exists" not in lowered:
                break
        raise WrapperError(f"could not create a per-task docker network: {last_error}")

    def _attach_proxy_to_task_network(self) -> None:
        self._run_checked(
            [
                self.docker_bin,
                "network",
                "connect",
                "--alias",
                _EGRESS_PROXY_ALIAS,
                self.state.task_network,
                self.proxy_container,
            ],
            step="egress.network_connect_proxy",
        )
        self.state.proxy_attached_to_task_network = True

    def _setup_task_network(self) -> None:
        self._create_task_network()
        self._attach_proxy_to_task_network()
        # Only log lines written from here on can belong to this task. Together with the
        # subnet filter this keeps attribution exact even when a later task in the same
        # run draws a subnet an earlier, finished task had used.
        if Path(self.proxy_access_log).is_file():
            self.state.access_log_offset = Path(self.proxy_access_log).stat().st_size
        print(
            "[egress] proxy attached: "
            f"network={self.state.task_network}, subnet={self.state.task_subnet}, "
            f"proxy={self.proxy_container}, allowed={self.egress_allowed_domains}",
            file=sys.stderr,
            flush=True,
        )

    def _emit_egress_events(self) -> None:
        """Report this task's proxied requests, attributed by its unique client subnet."""
        access_log = self.proxy_access_log
        if not Path(access_log).is_file() or not self.state.task_subnet:
            self._log("egress.audit skip: no access log or task subnet")
            return
        try:
            with open(access_log, "rb") as f:
                f.seek(self.state.access_log_offset)
                text = f.read().decode("utf-8", errors="replace")
        except OSError as exc:
            self._log(f"egress.audit skip: cannot read access log: {exc}")
            return
        events = parse_squid_access_log_text(text, client_subnet=self.state.task_subnet)
        for event in events:
            print(f"[egress-event] {json.dumps(event, ensure_ascii=False)}", file=sys.stderr, flush=True)
        self._log(f"egress.audit done events={len(events)}")

    def _cleanup_network(self) -> None:
        if self.state.task_network and self.state.proxy_attached_to_task_network:
            self._run_quiet(
                [self.docker_bin, "network", "disconnect", "-f", self.state.task_network, self.proxy_container],
                step="cleanup.network_disconnect_proxy",
            )
        if self.state.task_network:
            self._run_quiet([self.docker_bin, "network", "rm", self.state.task_network], step="cleanup.rm_network")

    # ------------------------------------------------------------------
    # Workspace audit and copyback
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_copyback_relative_path(raw_path: str) -> str | None:
        rel_path = str(raw_path or "")
        while rel_path.startswith("./"):
            rel_path = rel_path[2:]
        rel_path = rel_path.lstrip("/")
        if not rel_path:
            return None
        if rel_path == ".." or rel_path.startswith("../") or "/../" in rel_path or rel_path.endswith("/.."):
            return None
        return rel_path

    def _emit_workspace_changes(self) -> None:
        """Copy the finished workspace out to a scratch dir and report what changed vs. the input."""
        before = self.state.workspace_hashes_before
        if before is None or not self.state.container_id:
            self._log("workspace.audit skip: no baseline hashes or container")
            return
        export_dir = tempfile.mkdtemp(prefix="daml-agent-workspace-audit-")
        try:
            rc = self._run_quiet(
                [self.docker_bin, "cp", f"{self.state.container_id}:{CONTAINER_WORK_DIR}/.", export_dir],
                step="workspace.audit_export",
            )
            if rc != 0:
                print("[workspace-audit] export failed; workspace changes unknown", file=sys.stderr, flush=True)
                return
            after = hash_tree(export_dir)
        finally:
            shutil.rmtree(export_dir, ignore_errors=True)
        changes = diff_tree_hashes(before, after)
        for change in changes:
            print(f"[workspace-change] {json.dumps(change, ensure_ascii=False)}", file=sys.stderr, flush=True)
        print(f"[workspace-audit] complete changes={len(changes)}", file=sys.stderr, flush=True)

    def _cleanup_container(self) -> None:
        container_id = self.state.container_id
        if not container_id:
            self._log("cleanup.container skip: no container_id")
            return
        self._log(f"cleanup.container begin container_id={container_id}")
        self._run_quiet([self.docker_bin, "stop", "-t", "5", container_id], step="cleanup.stop")

        if self.copyback_rel_paths_raw:
            self._log("cleanup.copyback mode=impl_only")
            copied_count = 0
            for raw_rel_path in self.copyback_rel_paths_raw.splitlines():
                if not raw_rel_path:
                    continue
                rel_path = self._sanitize_copyback_relative_path(raw_rel_path)
                if rel_path is None:
                    print(f"[copyback] skipping unsafe relative path: {raw_rel_path}", file=sys.stderr, flush=True)
                    continue
                host_target = str(Path(self.work_dir) / rel_path)
                Path(host_target).parent.mkdir(parents=True, exist_ok=True)
                rc = self._run_quiet(
                    [
                        self.docker_bin,
                        "cp",
                        f"{container_id}:{CONTAINER_WORK_DIR}/{rel_path}",
                        host_target,
                    ],
                    step=f"cleanup.copyback_file rel={rel_path}",
                )
                if rc == 0:
                    copied_count += 1
            self._log(f"cleanup.copyback impl_only_done copied_ok={copied_count}")
        else:
            # No full-workspace fallback, ever: copying the whole workspace back would
            # let an agent's edits to the TEST file or daml.yaml overwrite the host
            # copy and be graded as ground truth (self-doctored tests). Impl-only
            # copyback is the anti-tamper boundary; startup validation guarantees the
            # list is configured, so this branch is unreachable in practice.
            self._log("cleanup.copyback SKIPPED: no copyback paths configured (full-workspace copyback is forbidden)")

        try:
            self._emit_workspace_changes()
        except Exception as exc:
            print(f"[workspace-audit] failed: {exc}", file=sys.stderr, flush=True)

        self._run_quiet([self.docker_bin, "rm", "-f", container_id], step="cleanup.rm_container")
        self._log("cleanup.container done")

    def cleanup(self) -> None:
        self._log("cleanup begin")
        self._cleanup_container()
        try:
            self._emit_egress_events()
        except Exception as exc:
            print(f"[egress-audit] failed: {exc}", file=sys.stderr, flush=True)
        self._cleanup_network()
        self._log("cleanup end")

    def _validated_user(self) -> str:
        uid = self.container_uid or str(os.getuid())
        gid = self.container_gid or str(os.getgid())
        if not uid.isdigit() or not gid.isdigit():
            raise WrapperError(f"invalid container uid/gid: {uid}:{gid}")
        return f"{uid}:{gid}"

    def run(self) -> int:
        self._log("run begin")
        self._setup_task_network()

        # Daml SDKs/dpm live in a host-side store, mounted read-only: agents need the
        # tools but must not be able to modify the store shared across tasks (a
        # writable mount would let one task poison the SDKs that later tasks use).
        from daml_agent_benchmark.sdk_store import sdk_store_mount_args

        docker_args: list[str] = [
            "create",
            "--platform",
            "linux/amd64",
            *sdk_store_mount_args(read_only=True),
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges",
            "--label",
            f"{RUNNER_DOCKER_LABEL}={RUNNER_DOCKER_LABEL_VALUE}",
            "--pids-limit",
            self.pids_limit,
            "--tmpfs",
            "/tmp:exec,mode=1777",
            "--tmpfs",
            "/var/tmp:exec,mode=1777",
            "--env",
            "HOME=/tmp/container-home",
            "--env",
            "DAML_HOME=/opt/daml",
        ]
        if self.needs_stdin:
            docker_args.append("-i")

        docker_args.extend(["--user", self._validated_user()])

        docker_args.extend(["--network", self.state.task_network])

        proxy_url = f"http://{_EGRESS_PROXY_ALIAS}:{CONTAINER_EGRESS_PROXY_PORT}"
        docker_args.extend(
            [
                "--env",
                f"HTTP_PROXY={proxy_url}",
                "--env",
                f"HTTPS_PROXY={proxy_url}",
                "--env",
                f"ALL_PROXY={proxy_url}",
                "--env",
                f"http_proxy={proxy_url}",
                "--env",
                f"https_proxy={proxy_url}",
                "--env",
                f"all_proxy={proxy_url}",
                "--env",
                "NO_PROXY=localhost,127.0.0.1",
                "--env",
                "no_proxy=localhost,127.0.0.1",
            ]
        )

        for env_name in passthrough_env_names(self.secret_env_names):
            value = os.environ.get(env_name, "")
            if value:
                docker_args.extend(["--env", f"{env_name}={value}"])

        # Some repositories don't hardcode values in their daml.yaml files but
        # write placeholders like `sdk-version: ${DAML_VERSION}`, expecting the values
        # to come from environment variables that their .envrc file defines. On a
        # developer machine the tool `direnv` loads that .envrc automatically, but
        # direnv can't do its job inside the container (it activates via shell hooks,
        # and the agent runs bare commands). The harness therefore captures the .envrc
        # environment on the host — with real direnv, before the agent starts — and
        # hands it to us as JSON; we pass each variable to the container via `--env`.
        # These take precedence over same-named ENV defaults baked into the image
        # (e.g. the image's canton-specific DAML_VERSION).
        extra_env_json = os.environ.get("CONTAINER_AGENT_EVAL_EXTRA_ENV_JSON", "")
        if extra_env_json:
            for env_name, value in json.loads(extra_env_json).items():
                docker_args.extend(["--env", f"{env_name}={value}"])

        docker_args.extend(
            [
                "--workdir",
                CONTAINER_WORK_DIR,
                self.image,
                "python3",
                _DIFF_WRAPPER_PATH,
                "codex",
            ]
        )
        docker_args.extend(self.args)

        create_cmd = [self.docker_bin, *docker_args]
        create_proc = self._run_checked(create_cmd, capture_output=True, text=True, step="container.create")
        self.state.container_id = (create_proc.stdout or "").strip()
        if not self.state.container_id:
            raise WrapperError("docker create returned empty container id")
        self._log(f"container created id={self.state.container_id}")
        # Machine-readable container id for host-side sync of impl files.
        # Bigger picture: this allows host files to stay current during the run even if final cleanup copyback fails.
        print(f"CONTAINER_ID={self.state.container_id}", file=sys.stderr, flush=True)

        started = time.time()
        self.state.workspace_hashes_before = hash_tree(self.work_dir)
        self._log(
            f"workspace.audit baseline files={len(self.state.workspace_hashes_before)} elapsed={time.time() - started:.2f}s"
        )

        self._run_checked(
            [
                self.docker_bin,
                "cp",
                f"{self.work_dir}/.",
                f"{self.state.container_id}:{CONTAINER_WORK_DIR}",
            ],
            step="container.copy_workspace_in",
        )
        self._run_checked(
            [
                self.docker_bin,
                "cp",
                str(self.script_dir / "docker" / "codex_diff_wrapper.py"),
                f"{self.state.container_id}:{_DIFF_WRAPPER_PATH}",
            ],
            step="container.copy_diff_wrapper",
        )

        start_cmd = [self.docker_bin, "start", "-a"]
        if self.needs_stdin:
            start_cmd.append("-i")
        start_cmd.append(self.state.container_id)

        # Attach stdio so caller can stream JSON lines / JSON-RPC over this wrapper process.
        self._log(f"container.start begin cmd={self._format_cmd(start_cmd)}")
        started = time.time()
        proc = subprocess.run(start_cmd, check=False)
        self._log(f"container.start end rc={proc.returncode} elapsed={time.time() - started:.2f}s")
        return int(proc.returncode)


def _install_signal_handlers() -> None:
    def handle_signal(_signum, _frame):
        print(f"[wrapper {time.time():.3f}] signal received: {_signum}", file=sys.stderr, flush=True)
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def main() -> int:
    _install_signal_handlers()
    try:
        wrapper = _ContainerCodexWrapper(sys.argv[1:])
    except WrapperError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return exc.exit_code

    exit_code = 1
    try:
        exit_code = wrapper.run()
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            exit_code = code
        else:
            exit_code = 1
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            stderr_text = exc.stderr.strip()
            if stderr_text:
                print(stderr_text, file=sys.stderr, flush=True)
        exit_code = int(exc.returncode or 1)
    except WrapperError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        exit_code = exc.exit_code
    finally:
        wrapper.cleanup()

    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
