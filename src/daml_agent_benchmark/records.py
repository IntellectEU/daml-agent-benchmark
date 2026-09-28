"""What a run writes down, and how it is read back.

Three records, one per level:

- `RunResult`, in `run.json`. It stores its identity and its tasks. Every run-level
  number is computed from the tasks, none is stored.
- `TaskResult`, in `tasks/<task>.json`. One task from its repository copy to its grade,
  with the findings that decide whether the grade can be trusted.
- `AttemptResult`, nested in the task record. What the agent did on the attempt that
  produced the result, carrying the audit trail of any attempt the provider cut short.

The agent's stdout, stderr and event stream are large, so they go to sidecar files next
to the task record and are read only on request. The proxy log of a run is a sidecar
of the run record in the same way.

A record made on one machine has to be readable on another, so no absolute path goes into
one. A file in a source repository is written as `<repository>/<path inside it>`, and a
repository copy relative to the copies root. Values other code derives from a record are
methods, never fields.
"""

from __future__ import annotations

import json
import types
import typing
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, NewType

from daml_agent_benchmark.egress_audit import summarize_egress_events
from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.tasklist_catalog import repo_relative_id
from daml_agent_benchmark.workspace_audit import ChangeKind, classify_workspace_changes


# A file in a source repository, as `<repository>/<path inside it>`.
RepoPath = NewType("RepoPath", str)
# A repository copy, relative to the root all copies go under.
CopyPath = NewType("CopyPath", str)


def repo_relative(path: str | Path) -> RepoPath:
    return RepoPath(repo_relative_id(path))


def resolve_repo_relative(value: RepoPath | str) -> Path:
    return locations.sources_root / value


def copies_relative(path: str | Path) -> CopyPath:
    return CopyPath(str(Path(path).resolve().relative_to(locations.repo_copies_dir.resolve())))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --- Reading and writing --------------------------------------------------------------


def _encode(value: Any) -> Any:
    """One value of a record, as JSON.

    A `Path` is refused: it is the one type that would serialise plausibly and wrongly,
    since stringifying it puts an absolute path back into the record.
    """
    if isinstance(value, Record):
        return value.to_record()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, Path):
        raise TypeError("a record holds relative strings, not paths")
    return value


def _decode(hint: Any, value: Any) -> Any:
    """The typed value a record field holds, from its JSON form."""
    if value is None:
        return None
    origin = typing.get_origin(hint)
    if origin is types.UnionType or origin is typing.Union:
        options = [a for a in typing.get_args(hint) if a is not type(None)]
        return _decode(options[0], value) if len(options) == 1 else value
    if origin is list:
        (item,) = typing.get_args(hint)
        return [_decode(item, v) for v in value]
    if origin is dict:
        _, item = typing.get_args(hint)
        return {k: _decode(item, v) for k, v in value.items()}
    if isinstance(hint, type):
        if issubclass(hint, Record):
            return hint.from_record(value)
        if issubclass(hint, StrEnum):
            return hint(value)
        if hint is datetime:
            return datetime.fromisoformat(value)
    return value


class Record:
    """A frozen dataclass that reads and writes itself as JSON."""

    def to_record(self) -> dict:
        return {f.name: _encode(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_record(cls, record: dict):
        hints = typing.get_type_hints(cls)
        known = {f.name for f in fields(cls)}
        unknown = set(record) - known
        if unknown:
            raise ValueError(f"{cls.__name__} record has unknown fields {sorted(unknown)}")
        return cls(**{k: _decode(hints[k], v) for k, v in record.items()})


def write_record_json(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# --- Enumerations ---------------------------------------------------------------------


class RunStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class LiveState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"


class Severity(StrEnum):
    """What a finding means for the grade.

    - `SECURITY`: something the isolation makes impossible happened, so the result cannot
      be trusted.
    - `INFRA`: the harness failed rather than the model, so the result says nothing about
      the model.
    - `WARNING`: recorded for the trace audit; the task is graded normally.

    A security or infra finding keeps the task out of the run's pass rates.
    """

    SECURITY = "security"
    INFRA = "infra"
    WARNING = "warning"


class TaskFlag(StrEnum):
    OUT_OF_WORKSPACE_WRITES = "out_of_workspace_writes"
    FORBIDDEN_TOOL_CALLS = "forbidden_tool_calls"
    NON_ALLOWED_EGRESS = "non_allowed_egress"

    INFRA_FAILURE = "infra_failure"
    RUNTIME_IDENTITY_MISMATCH = "runtime_identity_mismatch"
    EXPORT_TIMED_OUT = "export_timed_out"
    REPO_COPY_INTEGRITY_FAILURE = "repo_copy_integrity_failure"
    GROUND_TRUTH_CONTROL_FAILURE = "ground_truth_control_failure"

    SUSPICIOUS_COMMANDS = "suspicious_commands"
    BLOCKED_EGRESS = "blocked_egress"
    WORKSPACE_AUDIT_MISSING = "workspace_audit_missing"
    TEST_FILE_CHANGED = "test_file_changed"
    NON_TARGET_SOURCE_CHANGES = "non_target_source_changes"
    REPO_COPY_PARTIAL_MATCH = "repo_copy_partial_match"
    USAGE_UNKNOWN = "usage_unknown"
    COST_UNKNOWN = "cost_unknown"
    USED_SUB_AGENTS = "used_sub_agents"

    @property
    def severity(self) -> Severity:
        return _SEVERITY[self]


_SEVERITY = {
    TaskFlag.OUT_OF_WORKSPACE_WRITES: Severity.SECURITY,
    TaskFlag.FORBIDDEN_TOOL_CALLS: Severity.SECURITY,
    TaskFlag.NON_ALLOWED_EGRESS: Severity.SECURITY,
    TaskFlag.INFRA_FAILURE: Severity.INFRA,
    TaskFlag.RUNTIME_IDENTITY_MISMATCH: Severity.INFRA,
    TaskFlag.EXPORT_TIMED_OUT: Severity.INFRA,
    TaskFlag.REPO_COPY_INTEGRITY_FAILURE: Severity.INFRA,
    TaskFlag.GROUND_TRUTH_CONTROL_FAILURE: Severity.INFRA,
    TaskFlag.SUSPICIOUS_COMMANDS: Severity.WARNING,
    TaskFlag.BLOCKED_EGRESS: Severity.WARNING,
    TaskFlag.WORKSPACE_AUDIT_MISSING: Severity.WARNING,
    TaskFlag.TEST_FILE_CHANGED: Severity.WARNING,
    TaskFlag.NON_TARGET_SOURCE_CHANGES: Severity.WARNING,
    TaskFlag.REPO_COPY_PARTIAL_MATCH: Severity.WARNING,
    TaskFlag.USAGE_UNKNOWN: Severity.WARNING,
    TaskFlag.COST_UNKNOWN: Severity.WARNING,
    TaskFlag.USED_SUB_AGENTS: Severity.WARNING,
}


@dataclass(frozen=True)
class Finding(Record):
    """One thing the audit noticed about a task, and what it saw."""

    flag: TaskFlag  # what was noticed, and through it how severely it is taken
    detail: str  # what was actually seen, in words, for a reader of the record


# --- Values shared between levels -----------------------------------------------------


@dataclass(frozen=True)
class TokenUsage(Record):
    """Tokens spent. Cached input is also counted on its own because it is priced apart."""

    input_tokens: int  # all input, the cached tokens included
    output_tokens: int  # what the model produced
    cached_input_tokens: int  # the part of the input the provider served from its cache, charged less

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
        )


TokenUsage.ZERO = TokenUsage(0, 0, 0)


@dataclass(frozen=True)
class EgressSummary(Record):
    """What the proxy saw: every request, and the hosts split by how the allowlist treated them.

    `non_allowed_domains` must always be empty. A host outside the allowlist that the
    proxy served is an allowlist bug, not agent behaviour. `blocked_domains` is what the
    agent attempted and the proxy refused.
    """

    events: list[dict]  # one entry per request the proxy logged for this container
    domains_observed: list[str]  # every domain those requests addressed
    blocked_domains: list[str]  # the ones the proxy refused
    non_allowed_domains: list[str]  # served despite sitting outside the allowlist; always empty
    allowed_domain_suffixes: list[str]  # the allowed domains this summary was read against; each covers its subdomains
    allowed_hosts: list[str]  # the allowed exact hosts, which cover no subdomains

    def blocked_attempts_detected(self) -> bool:
        return bool(self.blocked_domains)

    def non_allowed_access_detected(self) -> bool:
        return bool(self.non_allowed_domains)

    @classmethod
    def from_events(cls, events: list[dict], allowed_suffixes: list[str], allowed_hosts: list[str]) -> EgressSummary:
        suffixes = list(allowed_suffixes)
        hosts = list(allowed_hosts)
        summary = summarize_egress_events(events, suffixes, hosts)
        return cls(
            events=list(events),
            domains_observed=summary["egress_domains_observed"],
            blocked_domains=summary["egress_blocked_domains"],
            non_allowed_domains=summary["egress_non_allowed_domains"],
            allowed_domain_suffixes=suffixes,
            allowed_hosts=hosts,
        )


# --- Values of one attempt ------------------------------------------------------------


@dataclass(frozen=True)
class ThreadUsage(Record):
    """The latest cumulative usage one agent thread reported, and whether its turn ended."""

    usage: TokenUsage  # the latest cumulative count this thread reported
    usage_reported: bool  # the thread reported a count at all
    turn_completed: bool  # its turn ended, so the count above is final rather than partial


@dataclass(frozen=True)
class SubAgent(Record):
    """A helper thread the agent spawned, with the model and effort the agent gave it."""

    thread_id: str  # the thread codex gave it, which is how its tokens are attributed
    prompt: str  # what the parent asked it to do, clipped
    model: str | None  # the model codex reported running it on
    reasoning_effort: str | None  # the effort codex reported running it at
    model_verified: bool  # that model is the one the run configured
    reasoning_effort_verified: bool  # likewise for the effort


@dataclass(frozen=True)
class RuntimeIdentity(Record):
    """Whether the agent that ran is the agent the run was configured for."""

    model_expected: str  # what the run asked codex for
    model_reported: str | None  # what codex said it ran
    model_verified: bool  # the two agree
    reasoning_effort_expected: str | None  # what the run asked for, when it asked at all
    reasoning_effort_reported: str | None  # what codex said it used
    reasoning_effort_verified: bool  # the two agree, or the run never asked
    model_provider: str | None  # the provider entry codex resolved the model through
    cli_version: str | None  # the codex build that ran

    def mismatch(self, sub_agents: list[SubAgent]) -> str | None:
        if not self.model_verified:
            return f"model mismatch: configured {self.model_expected!r}, codex reported {self.model_reported!r}"
        if not self.reasoning_effort_verified:
            return (
                f"reasoning effort mismatch: configured {self.reasoning_effort_expected!r}, "
                f"codex reported {self.reasoning_effort_reported!r}"
            )
        for sub_agent in sub_agents:
            if not sub_agent.model_verified:
                return f"sub-agent {sub_agent.thread_id} ran model {sub_agent.model!r}"
            if not sub_agent.reasoning_effort_verified:
                return f"sub-agent {sub_agent.thread_id} ran with reasoning effort {sub_agent.reasoning_effort!r}"
        return None


@dataclass(frozen=True)
class FileChange(Record):
    """One file the agent changed, and what happened to it."""

    path: str  # workspace-relative from the audit; container-absolute for a write outside it
    kind: ChangeKind  # what happened to it


@dataclass(frozen=True)
class WorkspaceAudit(Record):
    """Every file the agent changed in its container, sorted by what the change means.

    Grading reads only the implementation files, so none of this can change a score. It
    can make a transcript misleading: an agent that weakens the test and reports success
    looks like an ordinary failure to a reader. `available` is False when the container
    wrapper never delivered its report; an absent report never reads as clean.
    """

    available: bool  # the wrapper delivered its report; False means the buckets say nothing
    # The target files, which is the work the agent was asked to do.
    impl_changes: list[FileChange]
    # The test, which grading takes from the pristine host copy whatever the agent did to it.
    test_file_changes: list[FileChange]
    # Other source files: not graded, but they change what a build or test in the container meant.
    source_changes_outside_targets: list[FileChange]
    # What `daml build` writes. The agent compiling its own work is not tampering.
    build_outputs: list[FileChange]
    # Codex's own config and state directory, which it rewrites at startup.
    codex_home_changes: list[FileChange]
    other_changes: list[FileChange]  # everything else

    def tamper_suspected(self) -> bool:
        return bool(self.test_file_changes or self.source_changes_outside_targets)

    @classmethod
    def from_changes(cls, changes: list[dict] | None, impl_rel_paths: list[str], test_rel_path: str) -> WorkspaceAudit:
        names = (
            "impl_changes",
            "test_file_changes",
            "source_changes_outside_targets",
            "build_outputs",
            "codex_home_changes",
            "other_changes",
        )
        if changes is None:
            return cls(available=False, **{name: [] for name in names})
        buckets = classify_workspace_changes(changes, impl_rel_paths, test_rel_path)
        return cls(
            available=True,
            **{name: [FileChange(path=c["path"], kind=ChangeKind(c["kind"])) for c in buckets[name]] for name in names},
        )


class ItemEvent(StrEnum):
    """Where in an item's life codex reported it.

    These are the only three notifications that carry an item, so every audit record
    naming the event it came from names one of them.
    """

    STARTED = "item.started"
    UPDATED = "item.updated"
    COMPLETED = "item.completed"


class ForbiddenTool(StrEnum):
    """An item type an agent must never produce in this benchmark.

    The first three reach outside the recorded trace. Web search fetches content the
    egress allowlist never saw, and MCP or dynamic tools run work this harness cannot
    account for. The one exception is an MCP call to a server the experiment allows in
    `mcp_servers`: that call is recorded as an `AllowedMcpToolCall` instead.

    `SUB_AGENT_ACTIVITY` is there for a different reason. It is the item type of codex's
    `multi_agent_v2`, which the task config turns off because it withholds the parent
    turn's completion while helper threads are alive. Ordinary sub-agents
    (`collab_agent_tool_call`) are allowed: they run in the same container under the same
    config and are accounted per thread.

    Any of these appearing means the config hardening did not take, and a task whose
    hardening did not take cannot be trusted whatever the item itself did.
    """

    WEB_SEARCH = "web_search"
    MCP_TOOL_CALL = "mcp_tool_call"
    DYNAMIC_TOOL_CALL = "dynamic_tool_call"
    SUB_AGENT_ACTIVITY = "sub_agent_activity"


@dataclass(frozen=True)
class ForbiddenToolCall(Record):
    """An agent action of a kind the task forbids."""

    type: ForbiddenTool  # which forbidden kind of item this was
    id: str | None  # codex's item id, so several events about one call collapse to one record
    query: str | None  # the search terms, for a web search
    server: str | None  # the server, for an MCP call
    tool: str | None  # the tool it named
    event_type: ItemEvent | None  # the first event that reported it, nearly always `item.started`


@dataclass(frozen=True)
class AllowedMcpToolCall(Record):
    """A call the agent made to an MCP server the experiment allows."""

    id: str | None  # codex's item id, so several events about one call collapse to one record
    server: str  # the allowed server, by the name the experiment gave it
    tool: str | None  # the tool it named
    event_type: ItemEvent | None  # the first event that reported it, nearly always `item.started`


@dataclass(frozen=True)
class SuspiciousCommand(Record):
    """A shell command that read the process environment or addressed the model API directly."""

    id: str | None  # codex's item id for the command
    command: str  # the command line, clipped to 500 characters
    matched: str  # the part that matched, which is why the command is here at all


@dataclass(frozen=True)
class AttemptResult(Record):
    """What the agent did on the attempt that produced the result.

    When the provider rate-limited earlier attempts, their `egress`, `forbidden_tool_calls`
    and `suspicious_commands` are folded into this one: an attempt that was cut short still
    ran. Everything else is the final attempt's.

    `stdout`, `stderr` and `stdout_events` are written to sidecar files next to the task
    record and are None when the record was read without them.
    """

    command: list[str]  # the codex command line this attempt ran
    model: str  # the model the run asked codex for
    approval_policy: str  # codex's rule for when it may act without asking a human
    event_schema_version: str  # the canonicalisation `stdout_events` was written with
    final_message: str | None  # the agent's closing message on the main thread
    sub_agents: list[SubAgent]  # helper threads the agent spawned, each identity-checked
    returncode: int  # exit status of the codex process
    wall_seconds: float  # how long the attempt took
    timed_out: bool  # the task's runtime cap stopped the agent
    export_timed_out: bool  # copying the files back out of the container did not finish
    quota_limit_detected: bool  # the provider rate-limited at least one attempt of this task
    quota_retry_count: int  # attempts given up to rate limits before this one
    usage: TokenUsage | None  # tokens over every thread; None when no thread reported any
    usage_by_thread: dict[str, ThreadUsage]  # the same per thread, which is how sub-agents are accounted
    usage_complete: bool  # every thread reported a final count, so `usage` is not a lower bound
    usd_cost: float | None  # `usage` priced for `model`, request by request; None when no price is known for it
    input_usd: float | None  # the part of `usd_cost` for uncached input
    cached_input_usd: float | None  # the part of `usd_cost` for cached input
    output_usd: float | None  # the part of `usd_cost` for output
    stdout: str | None  # everything codex printed
    stderr: str | None  # codex's own log, and the container wrapper's report inside it
    stdout_events: list[dict] | None  # the canonical event stream the audit reads
    forbidden_tool_calls: list[ForbiddenToolCall]  # tools the config hardening should have removed
    allowed_mcp_tool_calls: list[AllowedMcpToolCall]  # calls to allowed MCP servers
    suspicious_commands: list[SuspiciousCommand]  # commands that went looking for the environment or the API key
    out_of_workspace_writes: list[FileChange]  # files the agent changed outside its repository copy
    egress: EgressSummary  # what the proxy saw this attempt's container ask for
    runtime_identity: RuntimeIdentity | None  # whether codex ran the model and effort the run asked for
    workspace_audit: WorkspaceAudit  # every file changed in the container, sorted by what the change means

    def runtime_identity_mismatch(self) -> str | None:
        return self.runtime_identity.mismatch(self.sub_agents) if self.runtime_identity else None

    def without_output(self) -> AttemptResult:
        return replace(self, stdout=None, stderr=None, stdout_events=None)


# --- Values of one task ---------------------------------------------------------------


@dataclass(frozen=True)
class Grade(Record):
    """The three-stage verdict on what the agent wrote: syntax, build, tests.

    Each stage runs only if the previous one passed. `test_results` maps every script the
    test runner reported to whether it passed; `tests_passed` is the runner's exit status.
    """

    syntax_passed: bool  # the file parses
    compile_passed: bool  # the package builds
    tests_passed: bool  # the test runner exited zero
    test_results: dict[str, bool]  # every script the runner named, and whether it passed
    syntax_error: str | None  # what the parser said, when it failed
    compile_error: str | None  # what the build said
    tests_error: str | None  # what the test runner said

    def tests_passed_count(self) -> int:
        return sum(1 for passed in self.test_results.values() if passed)

    def tests_total(self) -> int:
        return len(self.test_results)

    def every_script_passed(self) -> bool:
        return self.tests_passed and bool(self.test_results) and all(self.test_results.values())


@dataclass(frozen=True)
class GroundTruthControl(Record):
    """Whether the unblanked copy builds and passes its own tests in this environment."""

    grade: Grade  # the same grading, run on the copy with the ground truth still in it
    cached: bool  # this came from the cache rather than a build in this run
    image_id: str  # the eval image it ran under, which is part of the cache key
    evaluated_at_utc: datetime  # when the build and test actually ran
    build_outputs_removed: int | None  # files its build left behind that the task then deleted

    def passed(self) -> bool:
        """Every test script passed with the ground truth still in the copy.

        A stricter bar than a task's grade. A control that exited zero having run no
        script at all has not shown that this environment can build and test the task.
        """
        return self.grade.every_script_passed()


@dataclass(frozen=True)
class PrunedArchive(Record):
    """A prebuilt archive deleted from the copy because it contained a target's compiled content."""

    path: str  # the `.dar`, relative to the repository copy's root
    entries: list[str]  # the entries in it that matched a target, at most five of them


@dataclass(frozen=True)
class RepoCopyIntegrity(Record):
    """The scan of the copy before the agent sees it: nothing may reveal what it is asked to write."""

    ok: bool  # nothing below disqualifies the copy; partial matches alone do not
    offending: list[dict]  # a target's content found somewhere else in the copy
    partial_matches: list[dict]  # part of a target found elsewhere, which these libraries do legitimately
    git_entries: list[str]  # git objects left in the copy, where the history holds the answer
    symlinks_outside_repo_copy: list[dict]  # links pointing out of the copy, which would reach past it
    targets_checked: list[str]  # the targets long enough to fingerprint, which is what was searched for
    targets_skipped_too_short: list[str]  # targets too short to fingerprint; a copy of one would go unseen
    files_scanned: int  # how much of the copy the scan covered
    bytes_scanned: int
    elapsed_seconds: float  # what the scan cost, which is why it is worth watching
    repo_copy_digest: str  # content hash of the copy, and the ground-truth control's cache key
    pruned_archives: list[PrunedArchive]  # prebuilt archives deleted because they held a target's compiled form

    @classmethod
    def from_scan(cls, report: dict, pruned_archives: list[dict]) -> RepoCopyIntegrity:
        return cls(
            **{f.name: report[f.name] for f in fields(cls) if f.name != "pruned_archives"},
            pruned_archives=[PrunedArchive(path=p["path"], entries=list(p["entries"])) for p in pruned_archives],
        )


@dataclass(frozen=True)
class ImplFileSnapshot(Record):
    """One implementation file before the task blanked it and after the agent wrote it."""

    impl_file: RepoPath  # the file in its source repository
    path_in_copy: str  # where it sat in the copy the agent worked in
    original: str | None  # the ground truth, as it was before the task blanked the file
    generated: str | None  # what the agent left in its place
    captured_at_utc: datetime  # when the pair was taken


@dataclass(frozen=True)
class TaskResult(Record):
    """One task, from its repository copy to its grade.

    `grade` and `attempts` are None until the task has run: the live snapshot of a queued
    or running task is this same record. `repo_copy` is None when the task failed before
    a copy was made. The copy is gone from disk by the time the record is read.
    """

    task_id: str  # the test file's path in its source repository, which is what names the task
    task_safe_name: str  # the same path flattened to one filename-safe word
    test_file: RepoPath  # the test the grade runs, taken from the pristine host copy
    impl_files: list[RepoPath]  # the files blanked for the agent to write, and the only ones graded
    live_state: LiveState  # where the task stands while the run is going
    queued_at_utc: datetime | None  # when the task entered the queue
    started_at_utc: datetime | None  # when the agent started
    finished_at_utc: datetime | None  # when the task reached its final state
    live_updated_at_utc: datetime | None  # when the live snapshot was last written
    repo_copy: CopyPath | None  # the copy the agent worked in, relative to the copies root
    findings: list[Finding]  # what the audit found, which is what decides whether the grade counts
    grade: Grade | None  # what the agent's files did: syntax, compile, tests
    ground_truth_control: GroundTruthControl | None  # the same grading run against the original files
    repo_copy_integrity: RepoCopyIntegrity | None  # the scan showing the copy did not contain the answer
    impl_file_snapshots: list[ImplFileSnapshot]  # each implementation file before the task and after the agent
    attempts: AttemptResult | None  # what the agent did, with any cut-short attempt's audit folded in

    def flags(self) -> list[TaskFlag]:
        return [finding.flag for finding in self.findings]

    def has(self, flag: TaskFlag) -> bool:
        return any(finding.flag is flag for finding in self.findings)

    def untrusted(self) -> bool:
        return any(finding.flag.severity is not Severity.WARNING for finding in self.findings)

    def gradable(self) -> bool:
        return self.grade is not None and not self.untrusted()

    def completed(self, now: datetime | None = None) -> TaskResult:
        moment = now or now_utc()
        return replace(self, live_state=LiveState.COMPLETED, live_updated_at_utc=moment, finished_at_utc=moment)

    def without_output(self) -> TaskResult:
        return replace(self, attempts=self.attempts.without_output()) if self.attempts else self


# --- The run --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult(Record):
    """One run: its identity and its tasks. Everything else is computed from the tasks.

    `egress_events` is the proxy's whole access log for the run. Each task's `egress` is
    the slice the container wrapper attributed to it, so the difference between the two
    is traffic no task accounted for.
    """

    run_id: str  # the run's own directory name
    run_name: str  # the experiment preset's name, which is what a reader recognises it by
    status: RunStatus  # whether it finished, and how
    created_at_utc: datetime  # when the run started
    finished_at_utc: datetime | None  # when it stopped; None while it is still going
    tasks: list[TaskResult]  # every task; the record on disk lists their files instead
    egress_events: list[dict] | None  # the proxy's whole access log for the run
    egress_allowed_domain_suffixes: list[str]  # the domains the run's proxy allowed, each with its subdomains
    egress_allowed_hosts: list[str]  # the exact hosts the run's proxy allowed, which are the MCP servers' hosts

    def gradable(self) -> list[TaskResult]:
        return [t for t in self.tasks if t.gradable()]

    def untrusted(self) -> list[TaskResult]:
        return [t for t in self.tasks if t.untrusted()]

    def syntax_passed(self) -> int:
        return sum(1 for t in self.gradable() if t.grade.syntax_passed)

    def compile_passed(self) -> int:
        return sum(1 for t in self.gradable() if t.grade.compile_passed)

    def tests_passed(self) -> int:
        return sum(1 for t in self.gradable() if t.grade.tests_passed)

    def syntax_pass_rate(self) -> float:
        return self.syntax_passed() / len(self.gradable()) if self.gradable() else 0.0

    def compile_pass_rate(self) -> float:
        return self.compile_passed() / len(self.gradable()) if self.gradable() else 0.0

    def tests_pass_rate(self) -> float:
        return self.tests_passed() / len(self.gradable()) if self.gradable() else 0.0

    def attempted(self) -> list[AttemptResult]:
        return [t.attempts for t in self.tasks if t.attempts is not None]

    def usage(self) -> TokenUsage:
        return sum((a.usage for a in self.attempted() if a.usage is not None), TokenUsage.ZERO)

    def usage_complete(self) -> bool:
        return all(a.usage is not None and a.usage_complete for a in self.attempted())

    def total_usd_cost(self) -> float | None:
        """The run's cost, or None when a task's cost is unknown for a model that could be priced."""
        if any(t.has(TaskFlag.COST_UNKNOWN) for t in self.tasks):
            return None
        return sum(a.usd_cost for a in self.attempted() if a.usd_cost is not None)

    def agent_wall_seconds(self) -> float:
        return sum(a.wall_seconds for a in self.attempted())

    def wall_seconds(self) -> float | None:
        """How long the run's tasks were in flight: the union of each task's queued-to-finished span.

        For a run done in one go this is its wall time. A run merged from several, or resumed
        after a stop, leaves out the gaps in which none of its tasks was in flight. None while
        the run is still going.
        """
        if self.finished_at_utc is None:
            return None
        spans = sorted(
            (t.queued_at_utc or t.started_at_utc, t.finished_at_utc)
            for t in self.tasks
            if (t.queued_at_utc or t.started_at_utc) and t.finished_at_utc
        )
        if not spans:
            return (self.finished_at_utc - self.created_at_utc).total_seconds()
        total = 0.0
        start, end = spans[0]
        for span_start, span_end in spans[1:]:
            if span_start > end:
                total += (end - start).total_seconds()
                start, end = span_start, span_end
            else:
                end = max(end, span_end)
        return total + (end - start).total_seconds()

    def run_egress(self) -> EgressSummary:
        return EgressSummary.from_events(
            self.egress_events or [], self.egress_allowed_domain_suffixes, self.egress_allowed_hosts
        )

    def unattributed_egress(self) -> list[dict]:
        """Proxy log lines no task's attempt claimed: a wrapper that never reported, or traffic that was not a task."""
        claimed = {json.dumps(e, sort_keys=True) for a in self.attempted() for e in a.egress.events}
        return [e for e in (self.egress_events or []) if json.dumps(e, sort_keys=True) not in claimed]

    def flagged(self) -> dict[TaskFlag, list[str]]:
        index: dict[TaskFlag, list[str]] = {}
        for task in self.tasks:
            for flag in task.flags():
                index.setdefault(flag, []).append(task.task_id)
        return {flag: sorted(set(ids)) for flag, ids in sorted(index.items(), key=lambda kv: kv[0].value)}

    def summary(self) -> dict:
        """The run-level numbers, for the console and the dashboard. Nothing here is stored."""
        return {
            "run_id": self.run_id,
            "run_name": self.run_name,
            "status": self.status.value,
            "created_at_utc": self.created_at_utc.isoformat(),
            "finished_at_utc": self.finished_at_utc.isoformat() if self.finished_at_utc else None,
            "num_tasks": len(self.tasks),
            "gradable_tasks": len(self.gradable()),
            "untrusted_tasks": len(self.untrusted()),
            "syntax_passed": self.syntax_passed(),
            "compile_passed": self.compile_passed(),
            "tests_passed": self.tests_passed(),
            "syntax_pass_rate": self.syntax_pass_rate(),
            "compile_pass_rate": self.compile_pass_rate(),
            "tests_pass_rate": self.tests_pass_rate(),
            "usage": self.usage().to_record(),
            "usage_complete": self.usage_complete(),
            "total_usd_cost": self.total_usd_cost(),
            "run_wall_seconds": self.wall_seconds(),
            "agent_wall_seconds": self.agent_wall_seconds(),
            "run_egress": {
                "domains_observed": self.run_egress().domains_observed,
                "blocked_domains": self.run_egress().blocked_domains,
                "non_allowed_domains": self.run_egress().non_allowed_domains,
                "request_count": len(self.egress_events or []),
                "unattributed_request_count": len(self.unattributed_egress()),
            },
            "flagged": {flag.value: ids for flag, ids in self.flagged().items()},
        }

    # The run record lists its tasks by path; each task is its own file.

    def to_record(self) -> dict:
        record = super().to_record()
        record["tasks"] = [str(task_record_path(Path("."), t.task_safe_name)) for t in self.tasks]
        del record["egress_events"]
        return record

    @classmethod
    def load(cls, run_dir: str | Path, *, tasks: bool = True, output: bool = False) -> RunResult:
        """Read a run back, finished or not.

        Task records are read unless `tasks` is False: every final record under `tasks/`,
        plus the live snapshot of any task that has no final record yet, so a run that is
        still going reads with its queued and running tasks in place. Stdout, stderr and
        the event stream are read only when `output` is True.
        """
        root = Path(run_dir)
        record = json.loads((root / "run.json").read_text(encoding="utf-8"))
        record.pop("tasks")
        hints = typing.get_type_hints(cls)
        loaded: list[TaskResult] = []
        if tasks:

            def by_stem(dir_name: str) -> dict[str, Path]:
                directory = root / dir_name
                return {p.stem: p for p in sorted(directory.glob("*.json"))} if directory.is_dir() else {}

            final = by_stem("tasks")
            live = by_stem("tasks_live")
            loaded = [read_task_result(p, output=output) for p in final.values()]
            loaded += [read_task_result(p) for stem, p in live.items() if stem not in final]
        egress_path = run_egress_path(root)
        egress = json.loads(egress_path.read_text(encoding="utf-8")) if egress_path.exists() else None
        return cls(
            **{k: _decode(hints[k], v) for k, v in record.items()},
            tasks=loaded,
            egress_events=egress,
        )


# --- Files ----------------------------------------------------------------------------


def run_egress_path(run_dir: Path) -> Path:
    return run_dir / "egress_events.json"


def task_file_name(task_id: str) -> str:
    """A task id as a file name. A task id is a path, and its separators cannot be in one."""
    return task_id.replace("\\", "__").replace("/", "__")


def task_record_path(run_dir: Path, task_safe_name: str) -> Path:
    return run_dir / "tasks" / f"{task_safe_name}.json"


def task_live_record_path(run_dir: Path, task_safe_name: str) -> Path:
    return run_dir / "tasks_live" / f"{task_safe_name}.json"


def task_live_events_path(run_dir: Path, task_safe_name: str) -> Path:
    """Where the driver streams a task's events while it is still running."""
    return run_dir / "tasks_live" / f"{task_safe_name}.stdout.jsonl"


def _sidecar_paths(record_path: Path) -> dict[str, Path]:
    stem = record_path.with_suffix("")
    return {
        "stdout": stem.with_name(stem.name + ".stdout.txt"),
        "stderr": stem.with_name(stem.name + ".stderr.txt"),
        "stdout_events": stem.with_name(stem.name + ".events.jsonl"),
    }


def write_task_result(run_dir: Path, result: TaskResult) -> Path:
    """Write the task record and, when the attempt's output is present, its sidecars."""
    path = task_record_path(run_dir, result.task_safe_name)
    if result.attempts is not None:
        sidecars = _sidecar_paths(path)
        attempts = result.attempts
        path.parent.mkdir(parents=True, exist_ok=True)
        if attempts.stdout is not None:
            sidecars["stdout"].write_text(attempts.stdout, encoding="utf-8")
        if attempts.stderr is not None:
            sidecars["stderr"].write_text(attempts.stderr, encoding="utf-8")
        if attempts.stdout_events is not None:
            with sidecars["stdout_events"].open("w", encoding="utf-8") as handle:
                for event in attempts.stdout_events:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    write_record_json(path, result.without_output().to_record())
    return path


def write_task_live_result(run_dir: Path, result: TaskResult) -> Path:
    """The live snapshot the dashboard polls while the run is going: the record without output."""
    path = task_live_record_path(run_dir, result.task_safe_name)
    write_record_json(path, result.without_output().to_record())
    return path


def read_task_result(path: Path, *, output: bool = False) -> TaskResult:
    result = TaskResult.from_record(json.loads(path.read_text(encoding="utf-8")))
    if not output or result.attempts is None:
        return result
    sidecars = _sidecar_paths(path)
    stdout = sidecars["stdout"].read_text(encoding="utf-8") if sidecars["stdout"].exists() else None
    stderr = sidecars["stderr"].read_text(encoding="utf-8") if sidecars["stderr"].exists() else None
    events = None
    if sidecars["stdout_events"].exists():
        with sidecars["stdout_events"].open(encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
    return replace(result, attempts=replace(result.attempts, stdout=stdout, stderr=stderr, stdout_events=events))


def write_run_result(run_dir: Path, run: RunResult) -> Path:
    """Write the run record and, beside it, the proxy's access log for the whole run."""
    path = Path(run_dir) / "run.json"
    if run.egress_events is not None:
        write_record_json(run_egress_path(Path(run_dir)), run.egress_events)
    write_record_json(path, run.to_record())
    return path
