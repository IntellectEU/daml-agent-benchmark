"""The canonical event schema, and what the audit reads out of it.

Codex reports what it did as a stream of notifications. They are canonicalised into one
shape here, and the functions below pull out of that stream everything a run record keeps:
the agent's final message, its token usage per thread, the tools it was not allowed to use,
the commands worth a second look, and the files it touched outside its copy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from daml_agent_benchmark.config import ExperimentConfig, secret_env_names
from daml_agent_benchmark.records import (
    AllowedMcpToolCall,
    FileChange,
    ForbiddenTool,
    ForbiddenToolCall,
    ItemEvent,
    RuntimeIdentity,
    SubAgent,
    SuspiciousCommand,
    ThreadUsage,
    TokenUsage,
)
from daml_agent_benchmark.workspace_audit import ChangeKind

_ITEM_EVENT_BY_METHOD = {str(event).replace(".", "/"): event for event in ItemEvent}
_EGRESS_EVENT_LINE_RE = re.compile(r"^\[egress-event\]\s+(\{.*\})\s*$", re.MULTILINE)
_WORKSPACE_CHANGE_LINE_RE = re.compile(r"^\[workspace-change\]\s+(\{.*\})\s*$", re.MULTILINE)
_WORKSPACE_AUDIT_COMPLETE_RE = re.compile(r"^\[workspace-audit\]\s+complete\b", re.MULTILINE)
WRAPPER_CONTAINER_ID_RE = re.compile(r"^CONTAINER_ID=([0-9a-f]{12,64})$")
CODEX_EVENT_SCHEMA_VERSION = "codex_canonical_v1"
_CANONICAL_ITEM_TYPE_MAP = {
    "agentMessage": "agent_message",
    "assistantMessage": "assistant_message",
    "userMessage": "user_message",
    "commandExecution": "command_execution",
    "fileChange": "file_change",
    "todoList": "todo_list",
    "webSearch": "web_search",
    "mcpToolCall": "mcp_tool_call",
    "dynamicToolCall": "dynamic_tool_call",
    "collabAgentToolCall": "collab_agent_tool_call",
    "subAgentActivity": "sub_agent_activity",
    "imageGeneration": "image_generation",
    "imageView": "image_view",
}
_FORBIDDEN_ITEM_TYPES = frozenset(ForbiddenTool)
# Shell commands that read the process environment or talk to the OpenAI API
# directly. `suspicious_command_terms` adds the configured provider's key name and
# host. The API key has to be in codex's own environment, and every process in the
# container shares one uid, so an agent that goes looking for it can find it. These
# patterns make such an attempt visible in the audit.
_SUSPICIOUS_COMMAND_RE = re.compile(
    r"/proc/(?:self|\d+)/environ|\bprintenv\b|(?<![\w.-])env\b(?!\.)|OPENAI_API_KEY|CODEX_API_KEY|api\.openai\.com|\bcurl\b.*openai",
    re.IGNORECASE,
)


def suspicious_command_terms(config: ExperimentConfig) -> list[str]:
    """What else marks a command as suspicious for this experiment: its secrets' names and the provider's host."""
    return [*secret_env_names(config), urlsplit(config.provider_base_url).hostname or ""]


def extract_suspicious_commands(events: list[dict], extra_terms: Sequence[str] = ()) -> list[SuspiciousCommand]:
    """Commands the agent ran that reference the process environment, the API key or the model API.

    `extra_terms` are matched literally, after the fixed patterns. A command is taken from
    the first event that carries it, so one interrupted by the timeout is recorded even
    though it never reached `item.completed`.
    """
    pattern = _SUSPICIOUS_COMMAND_RE
    terms = [re.escape(term) for term in extra_terms if term]
    if terms:
        pattern = re.compile(f"{_SUSPICIOUS_COMMAND_RE.pattern}|{'|'.join(terms)}", re.IGNORECASE)
    seen: set[str] = set()
    findings: list[SuspiciousCommand] = []
    for event in events:
        if event.get("type") not in {"item.started", "item.completed"}:
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = item.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        command = str(command or "")
        match = pattern.search(command)
        if match is None:
            continue
        item_id = str(item.get("id") or "")
        if item_id and item_id in seen:
            continue
        seen.add(item_id)
        findings.append(SuspiciousCommand(id=item_id or None, command=clip_text(command, 500), matched=match.group(0)))
    return findings


def _is_allowed_mcp_call(item: dict, allowed_mcp_servers: Collection[str]) -> bool:
    return item.get("type") == ForbiddenTool.MCP_TOOL_CALL and item.get("server") in allowed_mcp_servers


def extract_forbidden_tool_calls(
    events: list[dict], allowed_mcp_servers: Collection[str] = ()
) -> list[ForbiddenToolCall]:
    """Collect items of forbidden types from canonical events, one record per item id.

    An MCP call to one of `allowed_mcp_servers` is not forbidden; `extract_allowed_mcp_tool_calls`
    records it.
    """
    seen: set[str] = set()
    calls: list[ForbiddenToolCall] = []
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type not in _FORBIDDEN_ITEM_TYPES or _is_allowed_mcp_call(item, allowed_mcp_servers):
            continue
        item_id = str(item.get("id") or "")
        key = f"{item_type}:{item_id}" if item_id else f"{item_type}:{len(calls)}"
        if key in seen:
            continue
        seen.add(key)
        calls.append(
            ForbiddenToolCall(
                type=ForbiddenTool(item_type),
                id=item_id or None,
                query=item.get("query"),
                server=item.get("server"),
                tool=item.get("tool") or item.get("name"),
                event_type=ItemEvent(event["type"]),
            )
        )
    return calls


def extract_allowed_mcp_tool_calls(events: list[dict], allowed_mcp_servers: Collection[str]) -> list[AllowedMcpToolCall]:
    """The agent's calls to the MCP servers the experiment allows, one record per item id."""
    seen: set[str] = set()
    calls: list[AllowedMcpToolCall] = []
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict) or not _is_allowed_mcp_call(item, allowed_mcp_servers):
            continue
        item_id = str(item.get("id") or "")
        key = item_id or str(len(calls))
        if key in seen:
            continue
        seen.add(key)
        calls.append(
            AllowedMcpToolCall(
                id=item_id or None,
                server=str(item["server"]),
                tool=item.get("tool"),
                event_type=ItemEvent(event["type"]),
            )
        )
    return calls


def _canonical_item_type(item_type: object) -> str:
    value = str(item_type or "")
    return _CANONICAL_ITEM_TYPE_MAP.get(value, value)


def _canonical_item(item_raw: object) -> dict | None:
    if not isinstance(item_raw, dict):
        return None
    item = dict(item_raw)
    item_type = _canonical_item_type(item.get("type"))
    item["type"] = item_type
    if "exitCode" in item and "exit_code" not in item:
        item["exit_code"] = item.get("exitCode")
    if "aggregatedOutput" in item and "aggregated_output" not in item:
        item["aggregated_output"] = item.get("aggregatedOutput")
    if "memoryCitation" in item and "memory_citation" not in item:
        item["memory_citation"] = item.get("memoryCitation")
    if "textElements" in item and "text_elements" not in item:
        item["text_elements"] = item.get("textElements")
    return item


def _canonical_turn(turn_raw: object) -> dict | None:
    if not isinstance(turn_raw, dict):
        return None
    turn = dict(turn_raw)
    if "modelContextWindow" in turn and "model_context_window" not in turn:
        turn["model_context_window"] = turn.get("modelContextWindow")
    return turn


def usage_from_app_server_token_usage(token_usage: object) -> dict | None:
    if not isinstance(token_usage, dict):
        return None
    total = token_usage.get("total")
    if not isinstance(total, dict):
        total = token_usage
    return {
        "input_tokens": int(total.get("inputTokens", total.get("input_tokens", 0)) or 0),
        "output_tokens": int(total.get("outputTokens", total.get("output_tokens", 0)) or 0),
        "cached_input_tokens": int(total.get("cachedInputTokens", total.get("cached_input_tokens", 0)) or 0),
    }


def canonicalize_app_server_notification(
    method: str,
    params: dict,
    latest_usage_by_turn: dict[str, dict],
) -> dict | None:
    if method == "thread/started":
        return {"type": "thread.started", "thread": params.get("thread")}
    if method == "thread/status/changed":
        return {
            "type": "thread.status.changed",
            "thread_id": params.get("threadId"),
            "status": params.get("status"),
        }
    if method == "turn/started":
        return {
            "type": "turn.started",
            "thread_id": params.get("threadId"),
            "turn": _canonical_turn(params.get("turn")),
        }
    if method == "turn/completed":
        turn = _canonical_turn(params.get("turn")) or {}
        turn_status = str(turn.get("status") or "")
        event_type = "turn.failed" if turn_status == "failed" else "turn.completed"
        event = {
            "type": event_type,
            "thread_id": params.get("threadId"),
            "turn": turn,
        }
        turn_id = str(turn.get("id") or "")
        if turn_id and turn_id in latest_usage_by_turn:
            event["usage"] = dict(latest_usage_by_turn[turn_id])
        return event
    if method in {"thread/tokenUsageUpdated", "thread/tokenUsage/updated"}:
        event = {
            "type": "thread.token_usage.updated",
            "thread_id": params.get("threadId"),
            "turn_id": params.get("turnId"),
            "token_usage": params.get("tokenUsage"),
        }
        usage = usage_from_app_server_token_usage(params.get("tokenUsage"))
        if usage is not None:
            event["usage"] = usage
        return event
    if method == "error":
        return {
            "type": "error",
            "error": params.get("error"),
            "thread_id": params.get("threadId"),
            "turn_id": params.get("turnId"),
            "will_retry": params.get("willRetry"),
        }
    item_event = _ITEM_EVENT_BY_METHOD.get(method)
    if item_event is not None:
        # Sub-agent threads report their items on the same connection; the thread id is
        # what tells the parent's work from a helper's.
        return {
            # The event stream on disk stays plain JSON; the enum is the record's type.
            "type": item_event.value,
            "item": _canonical_item(params.get("item")),
            "thread_id": params.get("threadId"),
            "turn_id": params.get("turnId"),
        }
    # Keep unknown notifications visible (debuggable) under canonical schema.
    return {"type": method.replace("/", "."), "params": params}


def _extract_json_lines(pattern: re.Pattern, text: str) -> list[dict]:
    records: list[dict] = []
    for match in pattern.finditer(text or ""):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _extract_egress_events_from_stderr(stderr_text: str) -> list[dict]:
    """Egress events the wrapper attributed to this task from the proxy access log."""
    return _extract_json_lines(_EGRESS_EVENT_LINE_RE, stderr_text)


def _extract_workspace_changes_from_stderr(stderr_text: str) -> list[dict] | None:
    """Workspace changes reported by the wrapper; None when the audit did not complete."""
    if not _WORKSPACE_AUDIT_COMPLETE_RE.search(stderr_text or ""):
        return None
    return _extract_json_lines(_WORKSPACE_CHANGE_LINE_RE, stderr_text)


def extract_wrapper_report(stderr_text: str) -> tuple[list[dict], list[dict] | None]:
    """What `codex_in_container` reported on stderr after the run.

    Two things: the proxied requests it attributed to this task, and the files the agent
    changed in its workspace. The changes are None when the audit did not complete, which
    is not the same as a task that changed nothing.
    """
    return _extract_egress_events_from_stderr(stderr_text), _extract_workspace_changes_from_stderr(stderr_text)


def redact_secret_values(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return text


def build_runtime_identity(thread_start_result: dict, config: ExperimentConfig) -> RuntimeIdentity:
    """Compare the model and reasoning effort codex reports for the thread with the configured ones.

    The benchmark result is only meaningful for the model it claims to have run,
    so a mismatch is treated as an infrastructure error rather than tolerated.
    """
    thread = thread_start_result["thread"]
    reported_model = thread_start_result.get("model")
    reported_effort = thread_start_result.get("reasoningEffort")
    expected_effort = config.codex_app_server_effort
    return RuntimeIdentity(
        model_expected=config.codex_model,
        model_reported=reported_model,
        model_verified=reported_model == config.codex_model,
        reasoning_effort_expected=expected_effort,
        reasoning_effort_reported=reported_effort,
        reasoning_effort_verified=expected_effort is None or reported_effort == expected_effort,
        model_provider=thread_start_result.get("modelProvider"),
        cli_version=thread.get("cliVersion"),
    )


def extract_sub_agents(events: list[dict], config: ExperimentConfig) -> list[SubAgent]:
    """Sub-agent threads the agent spawned, with the model and effort codex gave each.

    A spawn is a completed `collab_agent_tool_call` with tool `spawnAgent`; codex runs
    the child at the parent's model and effort, which the item reports, so the
    identity check extends to every child.
    """
    expected_effort = config.codex_app_server_effort
    spawned: dict[str, SubAgent] = {}
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") != "collab_agent_tool_call" or item.get("tool") != "spawnAgent":
            continue
        for thread_id in item.get("receiverThreadIds") or []:
            model = item.get("model")
            effort = item.get("reasoningEffort")
            spawned[str(thread_id)] = SubAgent(
                thread_id=str(thread_id),
                prompt=clip_text(str(item.get("prompt") or ""), 500),
                model=model,
                reasoning_effort=effort,
                model_verified=model == config.codex_model,
                reasoning_effort_verified=expected_effort is None or effort == expected_effort,
            )
    return list(spawned.values())


def extract_out_of_workspace_file_changes(events: list[dict], repo_copy_root: str) -> list[FileChange]:
    """Detect if the agent wrote files outside its repository copy -- a security violation
    that means it tried to cheat or escape the task boundary."""
    violations = []
    for event in events:
        item = event.get("item") or {}
        if item.get("type") != "file_change":
            continue
        for change in item.get("changes") or []:
            changed_path = change.get("path")
            if not changed_path:
                continue
            if _is_path_within(changed_path, repo_copy_root):
                continue
            violations.append(FileChange(path=str(Path(changed_path)), kind=_change_kind(change.get("kind"))))
    return violations


# Codex reports a change's kind as an object, `{"type": "update", "move_path": null}`, and
# names the three kinds its own way. A record holding changes from both producers should
# not hold two vocabularies, so codex's names are translated here.
_CODEX_CHANGE_KINDS = {
    "add": ChangeKind.ADDED,
    "update": ChangeKind.MODIFIED,
    "delete": ChangeKind.DELETED,
}


def _change_kind(raw: object) -> ChangeKind:
    """What codex said it did to a file, in the vocabulary the workspace audit uses."""
    kind = raw.get("type") if isinstance(raw, dict) else raw
    return _CODEX_CHANGE_KINDS.get(str(kind), ChangeKind.UNKNOWN)


def usage_by_thread(events: list[dict]) -> dict[str, ThreadUsage]:
    """Latest cumulative token usage codex reported for each thread of the task.

    Sub-agents are separate threads whose tokens are not included in the parent's
    totals, so the task's usage is the sum over threads. A thread whose turn never
    ended (a helper still running when the parent finished) keeps its last report.
    """
    threads: dict[str, dict] = {}

    def entry(thread_id: str) -> dict:
        return threads.setdefault(
            thread_id,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "usage_reported": False,
                "turn_completed": False,
            },
        )

    for event in events:
        event_type = str(event.get("type") or "")
        thread_id = str(event.get("thread_id") or "")
        if not thread_id:
            continue
        if event_type == "thread.token_usage.updated":
            data = event.get("usage")
            if isinstance(data, dict):
                current = entry(thread_id)
                for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
                    current[key] = data[key]
                current["usage_reported"] = True
        elif event_type == "turn.started":
            entry(thread_id)
        elif event_type in {"turn.completed", "turn.failed"}:
            entry(thread_id)["turn_completed"] = True
    return {
        thread_id: ThreadUsage(
            usage=TokenUsage(data["input_tokens"], data["output_tokens"], data["cached_input_tokens"]),
            usage_reported=data["usage_reported"],
            turn_completed=data["turn_completed"],
        )
        for thread_id, data in threads.items()
    }


def extract_usage(events: list[dict]) -> tuple[TokenUsage | None, bool]:
    """Total token usage of a codex run and whether it is complete.

    The total is the sum of every thread's latest cumulative usage. It is complete when
    every thread that started a turn also reported usage and ended its turn.
    """
    threads = usage_by_thread(events)
    if not threads:
        return None, False
    total = sum((t.usage for t in threads.values()), TokenUsage.ZERO)
    return total, all(t.usage_reported and t.turn_completed for t in threads.values())


def extract_final_message(events: list[dict], thread_id: str | None = None) -> str | None:
    """The last agent message of the run: the agent's own summary of what it did.

    With sub-agents, helper threads post messages too and may still be talking after
    the parent finished, so the message is taken from the given thread only.
    """
    final_text = None
    for event in events:
        if event.get("type") != "item.completed":
            continue
        if thread_id and event.get("thread_id") and event.get("thread_id") != thread_id:
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            final_text = item.get("text")
    return final_text


def _is_path_within(path: str | Path, root: str | Path) -> bool:
    try:
        path_resolved = Path(path).resolve()
        root_resolved = Path(root).resolve()
        return path_resolved == root_resolved or root_resolved in path_resolved.parents
    except Exception:
        return False


def strip_ansi_codes(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def clip_text(text: str | None, max_chars: int) -> str:
    value = (text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "..."


def clip_text_preserve_newlines(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    omitted = len(text) - max_chars
    return f"{clipped}\n... [truncated {omitted} chars]"
