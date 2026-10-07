"""Driving codex over its app-server protocol.

Codex runs as a long-lived process speaking JSON-RPC on stdin and stdout. `AppServerRunner`
starts it inside the task container, advances the protocol, records every notification,
keeps the copy in step with the files the agent writes in the container, and stops it
when the task finishes or runs out of time.
"""

from __future__ import annotations

from daml_agent_benchmark.task_run.jsonrpc import JsonRpcStdioClient
from daml_agent_benchmark.task_run.run_log import TaskRunLogger

import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from daml_agent_benchmark.codex_in_container import CONTAINER_WORK_DIR
from daml_agent_benchmark.config import (
    ExperimentConfig,
    egress_allowed_hosts,
    egress_allowed_suffixes,
    secret_env_names,
)
from daml_agent_benchmark.pricing import RequestsCost, attempt_events, request_token_usages, requests_cost
from daml_agent_benchmark.records import AttemptResult, EgressSummary, RuntimeIdentity, TokenUsage, WorkspaceAudit
from daml_agent_benchmark.constants import (
    CODEX_APP_SERVER_EXPERIMENTAL_API,
    CODEX_APPROVAL_POLICY,
    CODEX_HEARTBEAT_INTERVAL_SECONDS,
    CODEX_POST_COMPLETION_WAIT_SECONDS,
    CODEX_SANDBOX,
    CODEX_SHOW_HEARTBEAT,
    CODEX_TIMEOUT_INTERRUPT_GRACE_SECONDS,
    WRAPPER_PATH,
)
from daml_agent_benchmark.task_run.impl_sync import ImplSyncWatcher
from daml_agent_benchmark.repos.repo_copy import strip_ansi_codes
from daml_agent_benchmark.run_files import now_utc_iso
from daml_agent_benchmark.task_run.env import is_container_codex_runner, resolve_container_export_timeout_seconds
from daml_agent_benchmark.task_run.events import (
    CODEX_EVENT_SCHEMA_VERSION,
    WRAPPER_CONTAINER_ID_RE,
    build_runtime_identity,
    canonicalize_app_server_notification,
    extract_allowed_mcp_tool_calls,
    extract_final_message,
    extract_forbidden_tool_calls,
    extract_out_of_workspace_file_changes,
    extract_sub_agents,
    extract_suspicious_commands,
    extract_usage,
    extract_wrapper_report,
    suspicious_command_terms,
    usage_by_thread,
    usage_from_app_server_token_usage,
)
from daml_agent_benchmark.task_run.inputs import sanitize_copyback_rel_paths


@dataclass
class AppServerProtocolState:
    state: str = "await_initialize"
    initialize_req_id: int | None = None
    thread_start_req_id: int | None = None
    turn_start_req_id: int | None = None
    interrupt_req_id: int | None = None
    thread_id: str | None = None
    turn_id: str | None = None


@dataclass
class AppServerTimingState:
    """When this attempt started and what the loop has waited for since.

    Every field is a `monotonic()` reading or an interval derived from one, because they
    decide when a task times out. The wall clock can jump, forwards or backwards, and a
    timeout measured against it stops meaning what it says. The record's own timestamps
    come from `now_utc()` elsewhere.
    """

    start: float
    last_heartbeat: float = 0.0
    completion_seen: bool = False
    completion_seen_at: float = 0.0
    timed_out: bool = False
    interrupt_grace_deadline: float = 0.0


def thread_start_params(config: ExperimentConfig, cwd: str) -> dict[str, object]:
    """What codex's `thread/start` is given for one task.

    `sandbox` is codex's parameter for how it confines the agent, and it is sent as
    `danger-full-access`, meaning codex confines nothing. The container is the confinement:
    it reaches no network but the proxy and mounts nothing from the host. Codex confining
    the agent as well would stop a Daml build from running inside it.

    `approvalPolicy` says when the agent may act without asking a human. `ephemeral` keeps
    codex from carrying thread state into the next task. The reasoning effort is set here
    as well as in the config file, so codex echoes the effective value in its response and
    the run can check what it ran with.
    """
    params: dict[str, object] = {
        "cwd": cwd,
        "approvalPolicy": CODEX_APPROVAL_POLICY,
        "sandbox": CODEX_SANDBOX,
        "model": config.codex_model,
        "ephemeral": True,
    }
    if config.codex_app_server_effort:
        params["config"] = {"model_reasoning_effort": config.codex_app_server_effort}
    return params


class AppServerRunner:
    """Run one Codex app-server task end-to-end.

    In plain terms, this class is the per-task runtime controller. It starts the app-server
    process, sends the JSON-RPC calls needed to start a turn, reads all events, handles
    timeout/interrupt behavior, and returns the attempt's `AttemptResult`.

    It also keeps implementation files on the host's copy in sync with the container while
    the task is still running (via hash polling + copy). The point is to avoid losing model
    output if final wrapper cleanup copyback fails, and to make impl-file state visible
    before process exit.

    It does not choose tasks, build repository copies, or run syntax/build/test scoring.
    """

    def __init__(
        self,
        *,
        config: ExperimentConfig,
        codex_bin: str,
        codex_env: dict,
        repo_copy_root: str,
        prompt: str,
        timeout_seconds: int,
        copyback_rel_paths: list[str],
        protected_rel_paths: list[str],
        log_prefix: str = "",
        task_log_path: Path | None = None,
        live_stdout_events_path: Path | None = None,
    ):
        """Initialize one task-scoped app-server runner with runtime state."""
        self.config = config
        self.codex_bin = codex_bin
        self.codex_env = codex_env
        self.repo_copy_root = repo_copy_root
        self.prompt = prompt
        self.timeout_seconds = timeout_seconds
        self.allowed_impl_rel_paths = set(sanitize_copyback_rel_paths(copyback_rel_paths))
        self.protected_rel_paths = protected_rel_paths
        self.allowed_impl_rel_paths_sorted = sorted(self.allowed_impl_rel_paths)

        self.show_heartbeat = CODEX_SHOW_HEARTBEAT
        self.heartbeat_interval_seconds = CODEX_HEARTBEAT_INTERVAL_SECONDS
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("codex_heartbeat_interval_seconds must be > 0")
        if CODEX_TIMEOUT_INTERRUPT_GRACE_SECONDS < 1.0:
            raise ValueError(
                f"CODEX_TIMEOUT_INTERRUPT_GRACE_SECONDS must be >= 1.0, got {CODEX_TIMEOUT_INTERRUPT_GRACE_SECONDS}"
            )
        self.interrupt_grace_seconds = CODEX_TIMEOUT_INTERRUPT_GRACE_SECONDS
        self.post_completion_wait_seconds = max(0.0, CODEX_POST_COMPLETION_WAIT_SECONDS)
        self.container_export_timeout_seconds = resolve_container_export_timeout_seconds(timeout_seconds)

        self.cmd = [codex_bin, "-C", repo_copy_root, "app-server", "--listen", "stdio://"]
        self.logger = TaskRunLogger(
            log_prefix=log_prefix,
            task_log_path=task_log_path,
            live_stdout_events_path=live_stdout_events_path,
            secrets=[codex_env[name] for name in secret_env_names(config)],
        )

        if not is_container_codex_runner(codex_bin):
            raise RuntimeError(
                "The app-server transport requires the container codex runner "
                f"({WRAPPER_PATH}). Resolved codex_bin={codex_bin!r}."
            )
        self.thread_start_cwd = CONTAINER_WORK_DIR
        self.protocol = AppServerProtocolState()
        self.timing = AppServerTimingState(start=monotonic())
        self.responses: dict[int, dict] = {}
        self.latest_usage_by_turn: dict[str, dict] = {}

        self.stdout_lines: list[str] = []
        self.stdout_events: list[dict] = []
        self.stderr_lines: list[str] = []
        self.client: JsonRpcStdioClient | None = None
        self.container_id: str | None = None
        # Mirroring the container's implementation files into the copy is its own job.
        self.impl_sync = ImplSyncWatcher(
            repo_copy_root=repo_copy_root,
            allowed_impl_rel_paths_sorted=self.allowed_impl_rel_paths_sorted,
            logger=self.logger,
            is_exiting=self._container_wrapper_is_exiting,
        )
        self.returncode = 0
        self.container_export_timed_out = False
        self.runtime_identity: RuntimeIdentity | None = None

    def run(self) -> AttemptResult:
        """Execute exactly one Codex app-server turn for one task's repository copy.

        This does not orchestrate multiple tasks. The runner runs each task through
        `run_task(...)`, sequentially or in a thread pool.
        Returns the attempt's result: stdout, stderr, events, usage and timing.
        """
        self._log_launch_context()
        self.client = JsonRpcStdioClient(
            cmd=self.cmd,
            env=self.codex_env,
            cwd=self.repo_copy_root,
        )
        try:
            self.protocol.initialize_req_id = self.client.send_request(
                "initialize",
                {
                    "clientInfo": {"name": "daml-agent-eval", "version": "0.1"},
                    "capabilities": {"experimentalApi": CODEX_APP_SERVER_EXPERIMENTAL_API},
                },
            )
            self._event_loop()
            self.impl_sync.copy_all()
            self._shutdown_process()
            self.returncode = self.client.wait()
            self._drain_remaining_output_after_exit()
            return self._build_result()
        finally:
            self.impl_sync.stop()
            if self.client is not None:
                self.client.close()
            self.logger.close()

    def _log_launch_context(self) -> None:
        """Emit startup diagnostics for this task run."""
        self.logger.log(f"launching codex via: {self.codex_bin}")
        self.logger.log(f"timeout: {self.timeout_seconds}s")
        self.logger.log("transport: app_server")
        self.logger.log(f"[codex] container export timeout: {self.container_export_timeout_seconds:.1f}s")

    def _event_loop(self) -> None:
        """Run the main polling loop until completion, failure, or timeout exit."""
        while True:
            now = monotonic()
            elapsed = now - self.timing.start
            if self.show_heartbeat and elapsed - self.timing.last_heartbeat >= self.heartbeat_interval_seconds:
                self.logger.log(f"[codex] still running... {int(elapsed)}s")
                self.timing.last_heartbeat = elapsed

            assert self.client is not None
            if self.client.poll() is not None:
                self._drain_remaining_output_after_exit()
                if not self.timing.completion_seen:
                    self.logger.log("[codex] app-server process exited before turn completion")
                break

            self._drain_ready_output(timeout=0.2)

            if not self._advance_protocol_state():
                break
            if self._handle_timeout(elapsed):
                break
            if (
                self.timing.completion_seen
                and (monotonic() - self.timing.completion_seen_at) >= self.post_completion_wait_seconds
            ):
                break

    def _container_wrapper_is_exiting(self) -> bool:
        """Best-effort signal that container-side sync calls may fail during shutdown."""
        if self.impl_sync.stopping:
            return True
        if self.timing.timed_out:
            return True
        if self.client is not None and self.client.poll() is not None:
            return True
        return False

    def _handle_stderr_line(self, line: str) -> None:
        """Record and optionally surface one stderr line from app-server."""
        self.stderr_lines.append(line)
        stripped = strip_ansi_codes(line).strip()
        if "container.start begin" in stripped:
            self.impl_sync.start(self.container_id)
        container_id_match = WRAPPER_CONTAINER_ID_RE.fullmatch(stripped)
        if container_id_match is not None:
            container_id = container_id_match.group(1)
            if self.container_id is not None and self.container_id != container_id:
                raise RuntimeError(
                    f"Wrapper emitted conflicting container ids. old={self.container_id} new={container_id}"
                )
            self.container_id = container_id
            self.logger.log(f"[codex] container id captured for incremental sync: {container_id}")
        if stripped:
            self.logger.log(f"[codex-stderr] {stripped}")

    def _handle_stdout_line(self, line: str) -> None:
        """Process one stdout line: response, server request, or notification."""
        self.stdout_lines.append(line)
        stripped = line.strip()
        self.logger.log(f"[codex-json] {stripped}")
        if not stripped.startswith("{"):
            return
        try:
            msg = json.loads(stripped)
        except json.JSONDecodeError:
            return
        # The in-container diff wrapper attaches before/after diffs to file-change
        # notifications; strip them off the JSON-RPC message before protocol handling.
        wrapper_diffs = msg.pop("_file_change_diffs", None)

        if "id" in msg and "method" not in msg:
            msg_id = int(msg.get("id"))
            self.responses[msg_id] = msg
            if self.protocol.interrupt_req_id is not None and msg_id == self.protocol.interrupt_req_id:
                if isinstance(msg.get("error"), dict):
                    self.logger.log(f"[codex] turn/interrupt error: {msg['error']}")
                else:
                    self.logger.log("[codex] turn/interrupt acknowledged")
            return

        if "id" in msg and "method" in msg:
            req_id = msg.get("id")
            req_method = str(msg.get("method") or "")
            self.logger.log(f"[codex] unhandled server request: {req_method}")
            assert self.client is not None
            self.client.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unhandled server request: {req_method}"},
                }
            )
            return

        method = str(msg.get("method") or "")
        params_raw = msg.get("params")
        params = params_raw if isinstance(params_raw, dict) else {}
        self._record_notification(method, params, file_change_diffs=wrapper_diffs)

    def _drain_ready_output(self, timeout: float) -> None:
        """Consume whatever the wrapper has written to stdout/stderr so far."""
        assert self.client is not None
        for stream, line in self.client.read_ready_lines(timeout=timeout):
            if stream == "stderr":
                self._handle_stderr_line(line)
            else:
                self._handle_stdout_line(line)

    def _wait_for_exit(self, timeout_seconds: float) -> bool:
        """Wait for the wrapper to exit, still consuming its output; True when it exited.

        The wrapper's cleanup runs after it receives SIGTERM and reports the egress
        and workspace audits on stderr. Waiting without reading would lose that
        report, and a full pipe buffer would stall the cleanup itself.
        """
        assert self.client is not None
        deadline = monotonic() + timeout_seconds
        while self.client.poll() is None and monotonic() < deadline:
            self._drain_ready_output(timeout=0.1)
        return self.client.poll() is not None

    def _drain_remaining_output_after_exit(self) -> None:
        """Drain buffered stdout and stderr after process exit to avoid losing late output."""
        self._drain_remaining_stdout_after_exit()
        assert self.client is not None
        for line in self.client.read_remaining_stderr_lines():
            self._handle_stderr_line(line + "\n")

    def _drain_remaining_stdout_after_exit(self) -> None:
        """Drain buffered stdout after process exit to avoid losing late events."""
        assert self.client is not None
        for line in self.client.read_remaining_stdout_lines():
            self.stdout_lines.append(line + "\n")
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                msg = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            wrapper_diffs = msg.pop("_file_change_diffs", None)
            method = str(msg.get("method") or "")
            params_raw = msg.get("params")
            params = params_raw if isinstance(params_raw, dict) else {}
            self._record_notification(method, params, file_change_diffs=wrapper_diffs)

    def _record_notification(self, method: str, params: dict, *, file_change_diffs: list | None) -> None:
        """Canonicalize a server notification and append it to event/output streams."""
        if method in {"thread/tokenUsageUpdated", "thread/tokenUsage/updated"}:
            token_usage = usage_from_app_server_token_usage(params.get("tokenUsage"))
            usage_turn_id = str(params.get("turnId") or "")
            if token_usage is not None and usage_turn_id:
                self.latest_usage_by_turn[usage_turn_id] = token_usage

        canonical_event = canonicalize_app_server_notification(
            method=method,
            params=params,
            latest_usage_by_turn=self.latest_usage_by_turn,
        )
        if canonical_event is None:
            return

        event_record = {
            "line_no": len(self.stdout_lines),
            "captured_at_utc": now_utc_iso(),
            "schema_version": CODEX_EVENT_SCHEMA_VERSION,
            "source_transport": "app_server",
            "event": canonical_event,
            "raw_event": {"method": method, "params": params},
        }
        if file_change_diffs:
            event_record["file_change_diffs"] = file_change_diffs
        self.stdout_events.append(event_record)
        self.logger.write_live_event(event_record)

        self._update_completion(canonical_event)

    def _update_completion(self, canonical_event: dict) -> None:
        """Mark turn completion timing when terminal event matches tracked turn id."""
        if canonical_event.get("type") not in {"turn.completed", "turn.failed"}:
            return
        turn = canonical_event.get("turn")
        completed_turn_id = str(turn.get("id") or "") if isinstance(turn, dict) else ""
        if self.protocol.turn_id and completed_turn_id == self.protocol.turn_id:
            self.timing.completion_seen = True
            self.timing.completion_seen_at = monotonic()
            if self.timing.timed_out and turn.get("status") == "completed":
                # The deadline fired while codex was already finishing; the interrupt
                # found no active turn. The agent got its full budget, so this is a
                # normal completion, not a timeout.
                self.timing.timed_out = False
                self.logger.log("[codex] turn completed as the deadline fired; not recorded as a timeout")

    def _advance_protocol_state(self) -> bool:
        """Advance initialize/thread-start/turn-start JSON-RPC FSM; False means stop."""
        assert self.client is not None
        if (
            self.protocol.state == "await_initialize"
            and self.protocol.initialize_req_id is not None
            and self.protocol.initialize_req_id in self.responses
        ):
            init_resp = self.responses.pop(self.protocol.initialize_req_id)
            if isinstance(init_resp.get("error"), dict):
                self.logger.log(f"[codex] initialize failed: {init_resp['error']}")
                return False
            self.client.send_notification("initialized", {})
            thread_params = thread_start_params(self.config, self.thread_start_cwd)
            self.protocol.thread_start_req_id = self.client.send_request("thread/start", thread_params)
            self.protocol.state = "await_thread_start"

        if (
            self.protocol.state == "await_thread_start"
            and self.protocol.thread_start_req_id is not None
            and self.protocol.thread_start_req_id in self.responses
        ):
            thread_resp = self.responses.pop(self.protocol.thread_start_req_id)
            if isinstance(thread_resp.get("error"), dict):
                self.logger.log(f"[codex] thread/start failed: {thread_resp['error']}")
                return False
            thread_result = thread_resp.get("result")
            thread = thread_result.get("thread") if isinstance(thread_result, dict) else None
            self.protocol.thread_id = str(thread.get("id") or "") if isinstance(thread, dict) else ""
            if not self.protocol.thread_id:
                self.logger.log("[codex] thread/start returned no thread id")
                return False
            self.runtime_identity = build_runtime_identity(thread_result, self.config)
            self.logger.log(
                "[codex] runtime identity: "
                f"model={self.runtime_identity.model_reported!r} "
                f"effort={self.runtime_identity.reasoning_effort_reported!r} "
                f"cli={self.runtime_identity.cli_version!r}"
            )
            mismatch = self.runtime_identity.mismatch([])
            if mismatch is not None:
                self.logger.log(f"[codex] refusing to start turn: {mismatch}")
                return False
            turn_params = {
                "threadId": self.protocol.thread_id,
                "input": [{"type": "text", "text": self.prompt}],
            }
            if self.config.codex_app_server_effort:
                turn_params["effort"] = self.config.codex_app_server_effort
            self.protocol.turn_start_req_id = self.client.send_request("turn/start", turn_params)
            self.protocol.state = "await_turn_start"

        if (
            self.protocol.state == "await_turn_start"
            and self.protocol.turn_start_req_id is not None
            and self.protocol.turn_start_req_id in self.responses
        ):
            turn_resp = self.responses.pop(self.protocol.turn_start_req_id)
            if isinstance(turn_resp.get("error"), dict):
                self.logger.log(f"[codex] turn/start failed: {turn_resp['error']}")
                return False
            turn_result = turn_resp.get("result")
            turn = turn_result.get("turn") if isinstance(turn_result, dict) else None
            self.protocol.turn_id = str(turn.get("id") or "") if isinstance(turn, dict) else ""
            if not self.protocol.turn_id:
                self.logger.log("[codex] turn/start returned no turn id")
                return False
            self.protocol.state = "running"
        return True

    def _handle_timeout(self, elapsed: float) -> bool:
        """Apply timeout policy: in-band interrupt first, then process termination."""
        if self.timing.completion_seen or elapsed < self.timeout_seconds:
            return False
        assert self.client is not None
        if self.protocol.interrupt_req_id is None:
            self.timing.timed_out = True
            if self.protocol.thread_id and self.protocol.turn_id:
                self.logger.log("[codex] timeout reached; sending in-band turn/interrupt")
                self.protocol.interrupt_req_id = self.client.send_request(
                    "turn/interrupt",
                    {"threadId": self.protocol.thread_id, "turnId": self.protocol.turn_id},
                )
                self.timing.interrupt_grace_deadline = monotonic() + self.interrupt_grace_seconds
                return False

            self.logger.log("[codex] timeout reached before turn started; terminating app-server process")
            self._terminate_then_kill()
            return True

        if monotonic() < self.timing.interrupt_grace_deadline:
            return False
        self.logger.log("[codex] interrupt grace expired; terminating app-server process")
        self._terminate_then_kill()
        return True

    def _export_window_seconds(self) -> float:
        return self.container_export_timeout_seconds

    def _terminate_then_kill(self) -> None:
        """Ask the wrapper to stop and let it finish its cleanup; kill it only past the export window.

        SIGTERM triggers the wrapper's cleanup: stopping the container, copying the
        implementation files back and reporting the egress and workspace audits.
        Cutting that short would discard the audit of exactly the runs (timeouts)
        that most need one.
        """
        assert self.client is not None
        try:
            self.client.terminate()
        except Exception as exc:
            self.logger.log(f"[codex] terminate_then_kill: terminate() raised: {exc}")
        window = self._export_window_seconds()
        if not self._wait_for_exit(window):
            self.logger.log(f"[codex] wrapper still running {window:.1f}s after terminate; forcing kill")
            self.container_export_timed_out = True
            self.client.kill()

    def _shutdown_process(self) -> None:
        """Graceful shutdown at run end; in container mode allow long export window."""
        assert self.client is not None
        if self.client.poll() is not None:
            self.logger.log("[codex] shutdown_process: process already exited")
            return
        self._terminate_then_kill()

    def _build_result(self) -> AttemptResult:
        """What the agent did, assembled from the run's artifacts once the process has ended."""
        stdout_text = "".join(self.stdout_lines)
        stderr_text = "".join(self.stderr_lines)
        wall_seconds = monotonic() - self.timing.start
        parsed_events = [entry["event"] for entry in self.stdout_events if isinstance(entry.get("event"), dict)]
        usage, usage_complete = extract_usage(parsed_events)
        egress_events, workspace_changes = extract_wrapper_report(stderr_text)
        cost = usage_cost(self.config.codex_model, usage, self.stdout_events)
        allowed_mcp_servers = {server.name for server in self.config.mcp_servers}
        return AttemptResult(
            command=self.cmd,
            model=self.config.codex_model,
            approval_policy=CODEX_APPROVAL_POLICY,
            event_schema_version=CODEX_EVENT_SCHEMA_VERSION,
            final_message=extract_final_message(parsed_events, self.protocol.thread_id or None),
            sub_agents=extract_sub_agents(parsed_events, self.config),
            returncode=124 if self.timing.timed_out else self.returncode,
            wall_seconds=wall_seconds,
            timed_out=self.timing.timed_out,
            export_timed_out=self.container_export_timed_out,
            quota_limit_detected=False,
            quota_retry_count=0,
            usage=usage,
            usage_by_thread=usage_by_thread(parsed_events),
            usage_complete=usage_complete,
            usd_cost=cost.total if cost is not None else None,
            input_usd=cost.input if cost is not None else None,
            cached_input_usd=cost.cached_input if cost is not None else None,
            output_usd=cost.output if cost is not None else None,
            stdout=stdout_text,
            stderr=stderr_text,
            stdout_events=self.stdout_events,
            forbidden_tool_calls=extract_forbidden_tool_calls(parsed_events, allowed_mcp_servers),
            allowed_mcp_tool_calls=extract_allowed_mcp_tool_calls(parsed_events, allowed_mcp_servers),
            suspicious_commands=extract_suspicious_commands(parsed_events, suspicious_command_terms(self.config)),
            # In container mode file_change paths are container-local, so the boundary is /workspace.
            out_of_workspace_writes=extract_out_of_workspace_file_changes(parsed_events, CONTAINER_WORK_DIR),
            egress=EgressSummary.from_events(
                egress_events, egress_allowed_suffixes(self.config), egress_allowed_hosts(self.config)
            ),
            runtime_identity=self.runtime_identity,
            workspace_audit=WorkspaceAudit.from_changes(
                workspace_changes, self.allowed_impl_rel_paths_sorted, self.protected_rel_paths
            ),
        )


def usage_cost(model: str, usage: TokenUsage | None, event_records: list[dict]) -> RequestsCost | None:
    """What the attempt cost, or None when it cannot be priced.

    Each request of every thread is priced on its own, and the costs are added up. The
    cost is None when usage is unknown, when the model has no price, or when the events
    report tokens without any per-request usage to price them by.
    """
    if usage is None:
        return None
    requests = request_token_usages(attempt_events(event_records))
    if not requests and (usage.input_tokens or usage.output_tokens):
        return None
    return requests_cost(model, requests)


def run_app_server_attempt(
    *,
    config: ExperimentConfig,
    codex_bin: str,
    codex_env: dict,
    repo_copy_root: str,
    prompt: str,
    timeout_seconds: int,
    copyback_rel_paths: list[str],
    protected_rel_paths: list[str],
    log_prefix: str = "",
    task_log_path: Path | None = None,
    live_stdout_events_path: Path | None = None,
) -> AttemptResult:
    return AppServerRunner(
        config=config,
        codex_bin=codex_bin,
        codex_env=codex_env,
        repo_copy_root=repo_copy_root,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        copyback_rel_paths=copyback_rel_paths,
        protected_rel_paths=protected_rel_paths,
        log_prefix=log_prefix,
        task_log_path=task_log_path,
        live_stdout_events_path=live_stdout_events_path,
    ).run()
