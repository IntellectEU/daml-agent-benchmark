"""Reading an attempt's outcome.

An attempt can end in more ways than pass or fail: the model's provider can rate-limit it,
the container can fall over, the agent can stop without writing anything. The caller needs
to tell those apart to decide between retrying, failing the task, and failing the run.
"""

from __future__ import annotations

import hashlib
import json

from daml_agent_benchmark.constants import (
    CODEX_QUOTA_RETRY_BACKOFF_MULTIPLIER,
    CODEX_QUOTA_RETRY_BASE_DELAY_SECONDS,
    CODEX_QUOTA_RETRY_JITTER_SECONDS,
)
from daml_agent_benchmark.records import AttemptResult, Finding, RepoCopyIntegrity, TaskFlag


def _failed_turn_error(attempt: AttemptResult) -> str | None:
    """The error of the turn that failed, when one did."""
    for wrapped in attempt.stdout_events or []:
        event = wrapped.get("event")
        if isinstance(event, dict) and event.get("type") == "turn.failed":
            return ((event.get("turn") or {}).get("error") or {}).get("message") or "unknown error"
    return None


# Provider errors that refuse the account rather than the request: no retry helps, and the
# agent did not cause them.
_ACCOUNT_REFUSAL_MARKERS = ("no credits remaining", "insufficient_quota", "exceeded your current quota")


def infra_failure(attempt: AttemptResult) -> str | None:
    """Why the harness, not the model, failed the attempt; None when it did not.

    The test is whether the model was ever reached. An attempt that spent no input tokens
    and exited non-zero never got that far, so whatever it says about the task is the
    harness talking: a missing credential, a provider misconfiguration, a wrapper that
    could not start. An attempt that did reach the model owns its own outcome, however
    badly it ended, with one exception: a turn that failed because the provider refused
    the account, such as credit running out part-way through the task.
    """
    turn_error = _failed_turn_error(attempt)
    if turn_error is not None and any(marker in turn_error.lower() for marker in _ACCOUNT_REFUSAL_MARKERS):
        return f"The provider refused the account during the agent's turn: {turn_error}"
    if attempt.usage is not None and attempt.usage.input_tokens > 0:
        return None
    if turn_error is not None:
        return f"The agent's turn failed before any request: {turn_error}"
    if attempt.returncode == 0:
        return None
    tail = next((line for line in reversed((attempt.stderr or "").splitlines()) if line.strip()), "")
    return f"The agent exited {attempt.returncode} without reaching the model. {tail.strip()}".strip()


def rate_limited_for_retry(attempt: AttemptResult) -> bool:
    """Detect transient rate-limit errors that are worth retrying with backoff.

    Permanent quota failures (e.g. insufficient credits) are intentionally excluded.
    """
    markers = (
        "rate limit exceeded",
        "rate_limit_exceeded",
        "too many requests",
        "http 429",
        "status code: 429",
    )

    def has_marker(text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in markers)

    if has_marker(attempt.stdout or "") or has_marker(attempt.stderr or ""):
        return True
    for wrapped in attempt.stdout_events or []:
        event = wrapped.get("event")
        if isinstance(event, dict) and has_marker(json.dumps(event, ensure_ascii=True)):
            return True
    return False


def compute_quota_retry_delay_seconds(task_id: str, retry_number: int) -> float:
    """Calculate retry delay with deterministic jitter so parallel workers don't all retry
    at the same instant (which would just cause another rate-limit hit)."""
    base_delay = CODEX_QUOTA_RETRY_BASE_DELAY_SECONDS
    jitter = CODEX_QUOTA_RETRY_JITTER_SECONDS
    multiplier = CODEX_QUOTA_RETRY_BACKOFF_MULTIPLIER
    deterministic_key = f"{task_id}:{retry_number}"
    seed = int(hashlib.sha256(deterministic_key.encode("utf-8")).hexdigest()[:12], 16)
    jitter_unit = (seed % 1_000_000) / 999_999.0
    jitter_offset = (-jitter) + (2.0 * jitter * jitter_unit)
    raw_delay = (base_delay * (multiplier ** (retry_number - 1))) + jitter_offset
    return max(0.0, raw_delay)


def classify_attempt(attempt: AttemptResult, repo_copy_integrity: RepoCopyIntegrity) -> list[Finding]:
    """What the attempt's audit means for the task, as findings.

    A finding's flag carries its severity. Security findings are events the isolation
    makes impossible; infra findings are the harness failing rather than the model.
    Either keeps the task out of the pass rates, though the task is still graded so the
    record shows what the files did. Warnings are recorded for the trace audit and the
    task is graded normally. A missing workspace audit is itself a warning, so an absent
    report never reads as clean.
    """
    findings: list[Finding] = []

    def found(flag: TaskFlag, detail: str) -> None:
        findings.append(Finding(flag=flag, detail=detail))

    if attempt.out_of_workspace_writes:
        paths = [change.path for change in attempt.out_of_workspace_writes]
        found(TaskFlag.OUT_OF_WORKSPACE_WRITES, f"file changes outside the workspace: {paths[:10]}")
    if attempt.forbidden_tool_calls:
        kinds = sorted({call.type.value for call in attempt.forbidden_tool_calls})
        found(TaskFlag.FORBIDDEN_TOOL_CALLS, f"forbidden tool calls {kinds}")
    if attempt.egress.non_allowed_access_detected():
        found(TaskFlag.NON_ALLOWED_EGRESS, f"non-allowlisted hosts served by the proxy: {attempt.egress.non_allowed_domains}")

    mismatch = attempt.runtime_identity_mismatch()
    if mismatch is not None:
        found(TaskFlag.RUNTIME_IDENTITY_MISMATCH, mismatch)
    if attempt.export_timed_out:
        found(TaskFlag.EXPORT_TIMED_OUT, "container export timed out before the implementation files were copied back")
    failure = infra_failure(attempt)
    if failure is not None:
        found(TaskFlag.INFRA_FAILURE, failure)

    for partial in repo_copy_integrity.partial_matches:
        found(
            TaskFlag.REPO_COPY_PARTIAL_MATCH,
            f"part of a target's content exists elsewhere in the copy ({partial['path']}); "
            "these libraries duplicate code between modules, so the agent could read it",
        )
    if attempt.egress.blocked_attempts_detected():
        found(TaskFlag.BLOCKED_EGRESS, f"blocked egress attempts to {attempt.egress.blocked_domains}")
    if attempt.suspicious_commands:
        matched = sorted({command.matched for command in attempt.suspicious_commands})
        found(TaskFlag.SUSPICIOUS_COMMANDS, f"commands referencing the environment, API key or OpenAI API: {matched}")
    audit = attempt.workspace_audit
    if not audit.available:
        found(TaskFlag.WORKSPACE_AUDIT_MISSING, "workspace change audit did not complete; non-target edits unknown")
    else:
        if audit.protected_file_changes:
            changed = [change.path for change in audit.protected_file_changes]
            found(TaskFlag.PROTECTED_FILE_CHANGED, f"agent modified {changed}, which grading takes from the pristine host copy")
        if audit.source_changes_outside_targets:
            changed = [change.path for change in audit.source_changes_outside_targets]
            found(TaskFlag.NON_TARGET_SOURCE_CHANGES, f"agent changed non-target source files {changed} (not graded)")
    if attempt.sub_agents:
        found(TaskFlag.USED_SUB_AGENTS, f"{len(attempt.sub_agents)} sub-agent threads spawned")
    if attempt.usage is None or not attempt.usage_complete:
        found(TaskFlag.USAGE_UNKNOWN, "token usage was not fully reported, so usage and cost are lower bounds")
    elif attempt.usd_cost is None:
        found(TaskFlag.COST_UNKNOWN, f"no price known for model {attempt.model!r}")
    return findings
