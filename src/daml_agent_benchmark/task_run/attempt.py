"""What the agent does at one task, however many attempts that takes.

One attempt is the inputs it is given, the process it speaks to and the events it produces.
A task can take several: the provider rate-limits a run, and the attempt that was cut short
is discarded but its audit trail is not. Choosing tasks and grading what they produced
happen in `task.py`, which calls this.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from time import sleep

from daml_agent_benchmark.config import ExperimentConfig
from daml_agent_benchmark.constants import CODEX_QUOTA_RETRY_MAX_RETRIES
from daml_agent_benchmark.records import AttemptResult, EgressSummary
from daml_agent_benchmark.task_run.app_server import run_app_server_attempt
from daml_agent_benchmark.task_run.env import prepare_codex_invocation
from daml_agent_benchmark.task_run.events import redact_secret_values
from daml_agent_benchmark.task_run.inputs import clear_implementation_files, prepare_task_codex_home
from daml_agent_benchmark.task_run.outcome import compute_quota_retry_delay_seconds, rate_limited_for_retry


def run_attempt(
    config: ExperimentConfig,
    repo_copy_root: str,
    prompt: str,
    timeout_seconds: int,
    copyback_rel_paths: list[str],
    test_rel_path: str,
    codex_home_override: str,
    log_prefix: str = "",
    task_log_path: Path | None = None,
    live_stdout_events_path: Path | None = None,
) -> AttemptResult:
    """Run one codex attempt and return what the agent did."""
    codex_bin, codex_env = prepare_codex_invocation(
        config, copyback_rel_paths, repo_copy_root, codex_home_override
    )
    return run_app_server_attempt(
        config=config,
        codex_bin=codex_bin,
        codex_env=codex_env,
        repo_copy_root=repo_copy_root,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        copyback_rel_paths=copyback_rel_paths,
        test_rel_path=test_rel_path,
        log_prefix=log_prefix,
        task_log_path=task_log_path,
        live_stdout_events_path=live_stdout_events_path,
    )


def carry_prior_attempt_audits(attempt: AttemptResult, prior: list[AttemptResult]) -> AttemptResult:
    """Fold the audit trail of rate-limited attempts into the one that produced the result.

    An attempt the provider cut short still ran, so what it did to the network and which
    tools and MCP servers it reached for belong to this task's audit trail even though its result is
    discarded. The egress summary is recomputed over every attempt's events.
    """
    if not prior:
        return attempt
    events = [event for earlier in prior for event in earlier.egress.events] + attempt.egress.events
    return replace(
        attempt,
        egress=EgressSummary.from_events(events, attempt.egress.allowed_domain_suffixes, attempt.egress.allowed_hosts),
        forbidden_tool_calls=[c for earlier in prior for c in earlier.forbidden_tool_calls] + attempt.forbidden_tool_calls,
        allowed_mcp_tool_calls=[c for earlier in prior for c in earlier.allowed_mcp_tool_calls]
        + attempt.allowed_mcp_tool_calls,
        suspicious_commands=[c for earlier in prior for c in earlier.suspicious_commands] + attempt.suspicious_commands,
    )


def redacted(attempt: AttemptResult, secrets: list[str]) -> AttemptResult:
    """The attempt with the secrets scrubbed from its output.

    The API key travels in the container environment, so it can surface in captured
    stderr (a `docker create --env` line, or a command the agent ran that printed its
    environment). It must never reach the stored record.
    """
    events_json = redact_secret_values(json.dumps(attempt.stdout_events, ensure_ascii=False), secrets)
    return replace(
        attempt,
        stdout=redact_secret_values(attempt.stdout, secrets),
        stderr=redact_secret_values(attempt.stderr, secrets),
        stdout_events=json.loads(events_json),
    )


def attempt_with_quota_retries(
    config: ExperimentConfig,
    *,
    task_id: str,
    repo_copy_root: str,
    repo_copy_impl_files: list[str],
    prompt: str,
    test_rel_path: str,
    log_prefix: str,
    task_log_path: Path | None,
    live_stdout_events_path: Path | None,
) -> AttemptResult:
    """The agent's attempt at this task, retried while the provider is rate-limiting it.

    A retry starts from scratch: the implementation files are blanked again and the agent
    gets a fresh home, so it cannot read what the cut-short attempt left behind. What the
    discarded attempts did to the network and which tools they reached for is carried into
    the attempt that is kept, because they ran.

    Retries stop at `CODEX_QUOTA_RETRY_MAX_RETRIES`, and the returned attempt says whether a
    rate limit was ever seen and how many retries it cost.
    """
    quota_retry_count = 0
    prior_attempts: list[AttemptResult] = []
    attempt: AttemptResult | None = None
    while True:
        if attempt is not None:
            print(f"{log_prefix}retrying attempt {quota_retry_count + 1} after rate-limit response", flush=True)
            prior_attempts.append(attempt)
            clear_implementation_files(repo_copy_impl_files)
        attempt = run_attempt(
            config=config,
            repo_copy_root=repo_copy_root,
            prompt=prompt,
            timeout_seconds=config.max_task_runtime_seconds,
            copyback_rel_paths=[os.path.relpath(p, repo_copy_root).replace("\\", "/") for p in repo_copy_impl_files],
            test_rel_path=test_rel_path,
            codex_home_override=prepare_task_codex_home(config, repo_copy_root),
            log_prefix=log_prefix,
            task_log_path=task_log_path,
            live_stdout_events_path=live_stdout_events_path,
        )
        rate_limited = rate_limited_for_retry(attempt)
        if not rate_limited:
            break
        if quota_retry_count >= CODEX_QUOTA_RETRY_MAX_RETRIES:
            print(
                f"{log_prefix}rate-limit detected; retries exhausted (max_retries={CODEX_QUOTA_RETRY_MAX_RETRIES})",
                flush=True,
            )
            break
        retry_number = quota_retry_count + 1
        wait_seconds = compute_quota_retry_delay_seconds(task_id, retry_number)
        print(
            f"{log_prefix}rate-limit detected; waiting {wait_seconds:.1f}s "
            f"before retry {retry_number}/{CODEX_QUOTA_RETRY_MAX_RETRIES}",
            flush=True,
        )
        sleep(wait_seconds)
        quota_retry_count += 1

    return replace(
        carry_prior_attempt_audits(attempt, prior_attempts),
        quota_limit_detected=rate_limited,
        quota_retry_count=quota_retry_count,
    )
