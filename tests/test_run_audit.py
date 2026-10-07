"""Host-side classification of what a codex run did: forbidden tools, egress, workspace edits, identity."""

import json
from dataclasses import replace
from types import SimpleNamespace

from daml_agent_benchmark.task_run import ground_truth as ground_truth_module
from daml_agent_benchmark.codex_config import CODEX_TASK_CONFIG, task_codex_config_toml
from daml_agent_benchmark.config import DEFAULTS
from daml_agent_benchmark.constants import CODEX_APPROVAL_POLICY, CODEX_SANDBOX
from daml_agent_benchmark.records import (
    AttemptResult,
    EgressSummary,
    FileChange,
    ForbiddenTool,
    ForbiddenToolCall,
    ItemEvent,
    Grade,
    RepoCopyIntegrity,
    RuntimeIdentity,
    Severity,
    SubAgent,
    SuspiciousCommand,
    TaskFlag,
    TokenUsage,
    WorkspaceAudit,
)
from daml_agent_benchmark.task_run.app_server import thread_start_params
from daml_agent_benchmark.task_run.attempt import carry_prior_attempt_audits
from daml_agent_benchmark.task_run.events import (
    build_runtime_identity,
    canonicalize_app_server_notification,
    extract_final_message,
    extract_forbidden_tool_calls,
    extract_out_of_workspace_file_changes,
    extract_sub_agents,
    extract_suspicious_commands,
    extract_usage,
    extract_wrapper_report,
    redact_secret_values,
    usage_by_thread,
)
from daml_agent_benchmark.task_run.outcome import classify_attempt
from daml_agent_benchmark.workspace_audit import (
    ChangeKind,
    classify_workspace_changes,
    diff_tree_hashes,
    hash_tree,
    no_ignore,
    remove_added_files,
)


def test_hash_tree_ignores_codex_state_but_not_its_config(tmp_path) -> None:
    root = tmp_path / "ws"
    (root / ".codex_home" / "sessions" / "2026").mkdir(parents=True)
    (root / ".codex_home" / "sessions" / "2026" / "rollout.jsonl").write_text("{}", encoding="utf-8")
    (root / ".codex_home" / "history.jsonl").write_text("{}", encoding="utf-8")
    (root / ".codex_home" / "config.toml").write_text('web_search = "disabled"\n', encoding="utf-8")
    (root / ".daml" / "dist").mkdir(parents=True)
    (root / ".daml" / "dist" / "x.dar").write_bytes(b"dar")
    (root / "daml").mkdir()
    (root / "daml" / "Impl.daml").write_text("module Impl where\n", encoding="utf-8")
    assert sorted(hash_tree(root)) == [".codex_home/config.toml", "daml/Impl.daml"]
    assert len(hash_tree(root, ignore=no_ignore)) == 5

    audit = WorkspaceAudit.from_changes(
        [{"path": ".codex_home/config.toml", "kind": "modified"}], ["daml/Impl.daml"], ["daml/Test.daml"]
    )
    assert audit.codex_home_changes == [FileChange(path=".codex_home/config.toml", kind=ChangeKind.MODIFIED)]
    assert audit.tamper_suspected() is False


def test_remove_added_files_undoes_build_outputs_and_reports_modified(tmp_path) -> None:
    root = tmp_path / "repo_copy"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "Impl.daml").write_text("module Impl where\n", encoding="utf-8")
    (root / "pkg" / "daml.yaml").write_text("sdk-version: 2.9.0\n", encoding="utf-8")
    before = hash_tree(root, ignore=no_ignore)

    (root / "pkg" / ".daml" / "dist").mkdir(parents=True)
    (root / "pkg" / ".daml" / "dist" / "pkg-1.0.0.dar").write_bytes(b"dar")
    (root / "pkg" / ".daml" / "interfaces" / "Impl.hie").parent.mkdir(parents=True)
    (root / "pkg" / ".daml" / "interfaces" / "Impl.hie").write_bytes(b"hie")
    (root / "pkg" / "daml.yaml").write_text("sdk-version: 2.9.1\n", encoding="utf-8")

    outcome = remove_added_files(root, before)
    assert outcome["removed"] == ["pkg/.daml/dist/pkg-1.0.0.dar", "pkg/.daml/interfaces/Impl.hie"]
    assert outcome["modified"] == ["pkg/daml.yaml"]
    assert not (root / "pkg" / ".daml").exists()
    assert (root / "pkg" / "daml.yaml").read_text(encoding="utf-8") == "sdk-version: 2.9.1\n"


def _item_event(method: str, item: dict) -> dict:
    return canonicalize_app_server_notification(method, {"item": item}, {})


def test_web_search_item_is_canonicalized_and_flagged() -> None:
    events = [
        _item_event("item/started", {"type": "webSearch", "id": "ws_1", "query": "template DateClock"}),
        _item_event("item/completed", {"type": "webSearch", "id": "ws_1", "query": "template DateClock"}),
        _item_event("item/completed", {"type": "commandExecution", "id": "c_1", "command": "daml build"}),
        _item_event("item/completed", {"type": "mcpToolCall", "id": "m_1", "server": "helper", "tool": "write_impl"}),
    ]
    calls = extract_forbidden_tool_calls(events)
    assert [c.type for c in calls] == ["web_search", "mcp_tool_call"]
    assert calls[0].query == "template DateClock"
    assert calls[1].server == "helper"
    assert calls[1].tool == "write_impl"


def test_out_of_workspace_uses_container_root() -> None:
    events = [
        _item_event(
            "item/completed",
            {
                "type": "fileChange",
                "id": "f_1",
                "changes": [
                    {"path": "/workspace/daml/Impl.daml", "kind": {"type": "update", "move_path": None}},
                    {"path": "/etc/passwd", "kind": {"type": "add", "move_path": None}},
                ],
            },
        )
    ]
    # Codex reports a kind as an object and names the kinds its own way; the record keeps
    # the same three words the workspace audit uses.
    assert extract_out_of_workspace_file_changes(events, "/workspace") == [FileChange(path="/etc/passwd", kind=ChangeKind.ADDED)]


def test_wrapper_report_is_read_from_stderr() -> None:
    stderr = "\n".join(
        [
            "[wrapper 1.0] cleanup begin",
            "[egress-event] "
            + json.dumps(
                {
                    "client": "10.219.0.2",
                    "host": "api.openai.com",
                    "domain": "api.openai.com",
                    "blocked": False,
                    "method": "CONNECT",
                }
            ),
            "[egress-event] "
            + json.dumps(
                {
                    "client": "10.219.0.2",
                    "host": "github.com",
                    "domain": "github.com",
                    "blocked": True,
                    "method": "CONNECT",
                }
            ),
            "[workspace-change] " + json.dumps({"path": "daml/Impl.daml", "kind": "modified"}),
            "[workspace-change] " + json.dumps({"path": "daml/Test.daml", "kind": "modified"}),
            "[workspace-audit] complete changes=2",
        ]
    )
    egress_events, workspace_changes = extract_wrapper_report(stderr)
    egress = EgressSummary.from_events(egress_events, ["openai.com"], [])
    assert egress.domains_observed == ["api.openai.com", "github.com"]
    assert egress.blocked_domains == ["github.com"]
    assert egress.blocked_attempts_detected() is True
    assert egress.non_allowed_access_detected() is False
    assert workspace_changes == [
        {"path": "daml/Impl.daml", "kind": "modified"},
        {"path": "daml/Test.daml", "kind": "modified"},
    ]


def test_workspace_audit_incomplete_is_none() -> None:
    egress_events, workspace_changes = extract_wrapper_report('[workspace-change] {"path": "x", "kind": "added"}\n')
    assert workspace_changes is None
    assert egress_events == []
    assert WorkspaceAudit.from_changes(None, [], ["daml/Test.daml"]).available is False


def test_suspicious_commands_are_reported() -> None:
    def cmd(item_id: str, command: str, method: str = "item/completed") -> dict:
        return _item_event(method, {"type": "commandExecution", "id": item_id, "command": command})

    events = [
        cmd("c1", "daml build"),
        cmd("c2", "cat /proc/1/environ | tr '\\0' '\\n'", "item/started"),
        cmd("c2", "cat /proc/1/environ | tr '\\0' '\\n'"),
        cmd("c3", "env | grep -i key"),
        cmd("c4", "curl -s https://api.openai.com/v1/responses -H 'Authorization: Bearer x'"),
        cmd("c5", "cat .envrc && ls environment/"),
        cmd("c6", "echo $OPENAI_API_KEY"),
        # Interrupted by the timeout: started, never completed.
        cmd("c7", "printenv CODEX_API_KEY", "item/started"),
    ]
    findings = extract_suspicious_commands(events)
    assert [f.id for f in findings] == ["c2", "c3", "c4", "c6", "c7"]
    assert findings[0].matched == "/proc/1/environ"


def test_classify_workspace_changes_flags_test_and_source_edits() -> None:
    before = {"daml/Impl.daml": "a", "daml/Test.daml": "b", "daml.yaml": "c", "README.md": "d", "keep.txt": "e"}
    after = {"daml/Impl.daml": "a2", "daml/Test.daml": "b2", "daml.yaml": "c", "README.md": "d2", "new/Extra.daml": "f"}
    changes = diff_tree_hashes(before, after)
    audit = classify_workspace_changes(changes, ["daml/Impl.daml"], ["daml/Test.daml"])
    assert [c["path"] for c in audit["target_file_changes"]] == ["daml/Impl.daml"]
    assert [c["path"] for c in audit["protected_file_changes"]] == ["daml/Test.daml"]
    assert [c["path"] for c in audit["source_changes_outside_targets"]] == ["new/Extra.daml"]
    assert [c["path"] for c in audit["other_changes"]] == ["README.md", "keep.txt"]
    assert audit["tamper_suspected"] is True


def test_classify_workspace_changes_build_outputs_are_not_tampering() -> None:
    changes = [
        {"path": "build/example-impl-0.0.1.dar", "kind": "added"},
        {"path": "package/.daml/dist/x.dar", "kind": "added"},
        {"path": "lib/vendored-dep-1.0.0.dar", "kind": "modified"},
    ]
    audit = classify_workspace_changes(changes, ["daml/Impl.daml"], ["daml/Test.daml"])
    assert [c["path"] for c in audit["build_outputs"]] == [
        "build/example-impl-0.0.1.dar",
        "package/.daml/dist/x.dar",
    ]
    # A DAR replaced under a dependency directory is still the tamper signal.
    assert [c["path"] for c in audit["source_changes_outside_targets"]] == ["lib/vendored-dep-1.0.0.dar"]
    assert audit["tamper_suspected"] is True


def test_classify_workspace_changes_impl_only_is_clean() -> None:
    audit = classify_workspace_changes(
        [{"path": "daml/Impl.daml", "kind": "modified"}], ["daml/Impl.daml"], ["daml/Test.daml"]
    )
    assert audit["tamper_suspected"] is False


def test_runtime_identity_verification() -> None:
    config = DEFAULTS.merge(SimpleNamespace(codex_model="gpt-6-luna", codex_app_server_effort="medium"))
    ok = build_runtime_identity(
        {
            "model": "gpt-6-luna",
            "reasoningEffort": "medium",
            "modelProvider": "openai",
            "thread": {"cliVersion": "0.142.5"},
        },
        config,
    )
    assert ok.model_verified and ok.reasoning_effort_verified
    assert ok.cli_version == "0.142.5"
    assert ok.mismatch([]) is None

    wrong_model = build_runtime_identity({"model": "gpt-5.4-mini", "reasoningEffort": "medium", "thread": {}}, config)
    assert "model mismatch" in wrong_model.mismatch([])

    wrong_effort = build_runtime_identity({"model": "gpt-6-luna", "reasoningEffort": None, "thread": {}}, config)
    assert "reasoning effort mismatch" in wrong_effort.mismatch([])

    no_effort_configured = DEFAULTS.merge(SimpleNamespace(codex_model="gpt-6-luna"))
    assert (
        build_runtime_identity({"model": "gpt-6-luna", "reasoningEffort": None, "thread": {}}, no_effort_configured).mismatch(
            []
        )
        is None
    )


def test_task_codex_config_toml_round_trips() -> None:
    import tomllib

    assert tomllib.loads(task_codex_config_toml(DEFAULTS)) == CODEX_TASK_CONFIG


def test_redact_secret_values() -> None:
    assert redact_secret_values("key=sk-abc123 tail", ["sk-abc123"]) == "key=<redacted> tail"
    assert redact_secret_values("nothing", ["sk-x"]) == "nothing"


def _notify(method: str, params: dict) -> dict:
    return canonicalize_app_server_notification(method, params, {})


def _token_usage(thread_id: str, turn_id: str, inp: int, out: int, cached: int) -> dict:
    return _notify(
        "thread/tokenUsage/updated",
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "tokenUsage": {"total": {"inputTokens": inp, "outputTokens": out, "cachedInputTokens": cached}},
        },
    )


def _spawn(parent: str, child: str, model: str, effort: str) -> dict:
    return _notify(
        "item/completed",
        {
            "threadId": parent,
            "turnId": "t-main",
            "item": {
                "type": "collabAgentToolCall",
                "id": "call_1",
                "tool": "spawnAgent",
                "status": "completed",
                "senderThreadId": parent,
                "receiverThreadIds": [child],
                "prompt": "Draft the templates.",
                "model": model,
                "reasoningEffort": effort,
            },
        },
    )


def test_usage_sums_every_thread_and_is_incomplete_while_a_helper_still_runs() -> None:
    # Codex reports cumulative usage per thread; the parent's total excludes its helpers.
    events = [
        _notify("turn/started", {"threadId": "main", "turn": {"id": "t-main"}}),
        _token_usage("main", "t-main", 100, 10, 50),
        _notify("turn/started", {"threadId": "sub", "turn": {"id": "t-sub"}}),
        _token_usage("sub", "t-sub", 30, 3, 0),
        _token_usage("main", "t-main", 200, 20, 150),
        _notify("turn/completed", {"threadId": "main", "turn": {"id": "t-main", "status": "completed"}}),
    ]
    usage, complete = extract_usage(events)
    assert usage == TokenUsage(input_tokens=230, output_tokens=23, cached_input_tokens=150)
    assert complete is False, "the helper thread never ended its turn"
    threads = usage_by_thread(events)
    assert threads["main"].turn_completed and not threads["sub"].turn_completed

    events.append(_notify("turn/completed", {"threadId": "sub", "turn": {"id": "t-sub", "status": "completed"}}))
    assert extract_usage(events)[1] is True


def test_usage_is_unknown_when_no_turn_reported_it() -> None:
    assert extract_usage([_notify("turn/started", {"turn": {"id": "t"}})]) == (None, False)


def test_final_message_comes_from_the_main_thread() -> None:
    def message(thread_id: str, text: str) -> dict:
        return _notify(
            "item/completed", {"threadId": thread_id, "item": {"type": "agentMessage", "id": text, "text": text}}
        )

    events = [message("main", "parent summary"), message("sub", "helper still talking")]
    assert extract_final_message(events, "main") == "parent summary"
    assert extract_final_message(events) == "helper still talking"


def test_sub_agents_are_recorded_and_identity_checked() -> None:
    config = DEFAULTS.merge(SimpleNamespace(codex_model="gpt-5.4-mini", codex_app_server_effort="low"))
    same = extract_sub_agents([_spawn("main", "sub-1", "gpt-5.4-mini", "low")], config)
    assert same == [
        SubAgent(
            thread_id="sub-1",
            prompt="Draft the templates.",
            model="gpt-5.4-mini",
            reasoning_effort="low",
            model_verified=True,
            reasoning_effort_verified=True,
        )
    ]
    identity = build_runtime_identity({"model": "gpt-5.4-mini", "reasoningEffort": "low", "thread": {}}, config)
    assert identity.mismatch(same) is None

    other_model = extract_sub_agents([_spawn("main", "sub-2", "gpt-6-luna", "low")], config)
    assert "sub-agent sub-2 ran model 'gpt-6-luna'" in identity.mismatch(other_model)
    # A spawn is a tool call the agent is allowed to make.
    assert extract_forbidden_tool_calls([_spawn("main", "sub-3", "gpt-5.4-mini", "low")]) == []


_CLEAN_INTEGRITY = RepoCopyIntegrity(
    ok=True,
    offending=[],
    partial_matches=[],
    git_entries=[],
    symlinks_outside_repo_copy=[],
    targets_checked=["daml/Impl.daml"],
    targets_skipped_too_short=[],
    files_scanned=3,
    bytes_scanned=300,
    elapsed_seconds=0.01,
    repo_copy_digest="digest",
    pruned_archives=[],
)

_VERIFIED = RuntimeIdentity(
    model_expected="gpt-6-luna",
    model_reported="gpt-6-luna",
    model_verified=True,
    reasoning_effort_expected=None,
    reasoning_effort_reported=None,
    reasoning_effort_verified=True,
    model_provider="openai",
    cli_version="0.142.5",
)


def _attempt(**overrides) -> AttemptResult:
    """An attempt that ended without incident, unless a test says otherwise."""
    fields = dict(
        command=["codex"],
        model="gpt-6-luna",
        approval_policy="never",
        event_schema_version="codex_canonical_v1",
        final_message=None,
        sub_agents=[],
        returncode=143,
        wall_seconds=1.0,
        timed_out=False,
        export_timed_out=False,
        quota_limit_detected=False,
        quota_retry_count=0,
        usage=TokenUsage(1000, 50, 0),
        usage_by_thread={},
        usage_complete=True,
        usd_cost=0.01,
        input_usd=0.005,
        cached_input_usd=0.0,
        output_usd=0.005,
        stdout="",
        stderr="",
        stdout_events=[],
        forbidden_tool_calls=[],
        allowed_mcp_tool_calls=[],
        suspicious_commands=[],
        out_of_workspace_writes=[],
        egress=EgressSummary.from_events([], ["openai.com"], []),
        runtime_identity=_VERIFIED,
        workspace_audit=WorkspaceAudit.from_changes([], ["daml/Impl.daml"], ["daml/Test.daml"]),
    )
    fields.update(overrides)
    return AttemptResult(**fields)


def _egress(*hosts: tuple[str, bool]) -> EgressSummary:
    return EgressSummary.from_events(
        [{"host": host, "domain": host, "blocked": blocked} for host, blocked in hosts], ["openai.com"], []
    )


def test_classify_attempt_separates_security_infra_and_warnings() -> None:
    assert classify_attempt(_attempt(), _CLEAN_INTEGRITY) == []

    # Things the isolation makes impossible: not graded.
    violated = classify_attempt(
        _attempt(
            forbidden_tool_calls=[ForbiddenToolCall(ForbiddenTool.WEB_SEARCH, "ws_1", "q", None, None, ItemEvent.COMPLETED)],
            egress=_egress(("evil.example", False)),
        ),
        _CLEAN_INTEGRITY,
    )
    assert [f.flag for f in violated] == [TaskFlag.FORBIDDEN_TOOL_CALLS, TaskFlag.NON_ALLOWED_EGRESS]
    assert all(f.flag.severity is Severity.SECURITY for f in violated)
    assert "['web_search']" in violated[0].detail and "evil.example" in violated[1].detail

    # Harness failures: not graded, but not a violation.
    mismatch = classify_attempt(
        _attempt(runtime_identity=replace(_VERIFIED, model_reported="b", model_verified=False)),
        _CLEAN_INTEGRITY,
    )
    assert [f.flag for f in mismatch] == [TaskFlag.RUNTIME_IDENTITY_MISMATCH]
    assert mismatch[0].flag.severity is Severity.INFRA
    assert mismatch[0].detail.startswith("model mismatch")

    # Stopped attempts and ignored edits: graded, recorded as warnings.
    warned = classify_attempt(
        _attempt(
            egress=_egress(("example.com", True)),
            suspicious_commands=[SuspiciousCommand("c1", "cat /proc/1/environ", "/proc/1/environ")],
            workspace_audit=WorkspaceAudit.from_changes(
                [{"path": "daml/Test.daml", "kind": "modified"}], ["daml/Impl.daml"], ["daml/Test.daml"]
            ),
        ),
        _CLEAN_INTEGRITY,
    )
    assert [f.flag for f in warned] == [TaskFlag.BLOCKED_EGRESS, TaskFlag.SUSPICIOUS_COMMANDS, TaskFlag.PROTECTED_FILE_CHANGED]
    assert all(f.flag.severity is Severity.WARNING for f in warned)

    # No audit report is a warning, never a clean result.
    missing = classify_attempt(_attempt(workspace_audit=WorkspaceAudit.from_changes(None, [], ["t"])), _CLEAN_INTEGRITY)
    assert [f.flag for f in missing] == [TaskFlag.WORKSPACE_AUDIT_MISSING]

    # An attempt that ended cleanly but never reported its tally: a warning on the cost,
    # not a verdict on the grade.
    lost_tally = _attempt(returncode=0, usage=None, usage_complete=False)
    assert [f.flag for f in classify_attempt(lost_tally, _CLEAN_INTEGRITY)] == [TaskFlag.USAGE_UNKNOWN]
    assert [f.flag for f in classify_attempt(_attempt(usd_cost=None), _CLEAN_INTEGRITY)] == [TaskFlag.COST_UNKNOWN]


def test_export_timeout_is_an_infra_finding() -> None:
    findings = classify_attempt(_attempt(export_timed_out=True), _CLEAN_INTEGRITY)
    assert [f.flag for f in findings] == [TaskFlag.EXPORT_TIMED_OUT]
    assert findings[0].flag.severity is Severity.INFRA


def test_an_attempt_that_never_reached_the_model_is_an_infra_finding() -> None:
    """A wrapper that dies on startup must not read as a task the model failed."""
    crashed = _attempt(
        returncode=1,
        usage=None,
        usage_complete=False,
        stderr="Traceback (most recent call last):\n  ...\nModuleNotFoundError: No module named 'x'\n",
    )
    findings = classify_attempt(crashed, _CLEAN_INTEGRITY)
    assert [f.flag for f in findings] == [TaskFlag.INFRA_FAILURE, TaskFlag.USAGE_UNKNOWN]
    assert "ModuleNotFoundError" in findings[0].detail
    assert findings[0].flag.severity is Severity.INFRA

    # An attempt that reached the model owns its outcome, however badly it ended.
    assert classify_attempt(_attempt(returncode=1), _CLEAN_INTEGRITY) == []


def test_turn_failed_before_any_request_is_an_infra_finding() -> None:
    failed_turn = {"event": {"type": "turn.failed", "turn": {"error": {"message": "missing credential"}}}}
    findings = classify_attempt(_attempt(usage=None, usage_complete=False, stdout_events=[failed_turn]), _CLEAN_INTEGRITY)
    assert [f.flag for f in findings] == [TaskFlag.INFRA_FAILURE, TaskFlag.USAGE_UNKNOWN]
    assert "missing credential" in findings[0].detail


def test_credit_running_out_mid_turn_is_an_infra_finding() -> None:
    """Credit that runs out after the agent has started must not read as the agent's failure."""
    message = "stream disconnected before completion: You have no credits remaining. Add credits to continue."
    failed_turn = {"event": {"type": "turn.failed", "turn": {"error": {"message": message}}}}
    findings = classify_attempt(_attempt(returncode=143, stdout_events=[failed_turn]), _CLEAN_INTEGRITY)
    assert [f.flag for f in findings] == [TaskFlag.INFRA_FAILURE]
    assert "no credits remaining" in findings[0].detail

    # Any other failed turn after the model was reached stays the agent's own outcome.
    other = {"event": {"type": "turn.failed", "turn": {"error": {"message": "context window exceeded"}}}}
    assert classify_attempt(_attempt(returncode=1, stdout_events=[other]), _CLEAN_INTEGRITY) == []


def test_grade_counts_scripts_and_requires_every_one_for_the_control() -> None:
    passed = Grade(True, True, True, {"Test:main": True}, None, None, None)
    assert passed.every_script_passed() and passed.tests_passed_count() == 1 and passed.tests_total() == 1
    # One failing script, no scripts at all, or a failed test stage: not a pass.
    assert not Grade(True, True, True, {"a": True, "b": False}, None, None, None).every_script_passed()
    assert not Grade(True, True, True, {}, None, None, None).every_script_passed()
    assert not Grade(True, True, False, {"a": True}, None, None, "boom").every_script_passed()


def test_ground_truth_control_caches_passes_and_rebuilds_everything_else(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ground_truth_module, "_ground_truth_control_cache_path", lambda: tmp_path / "control_cache.json")
    graded: list[str] = []
    outcome = Grade(True, True, True, {"Test:main": True}, None, None, None)

    def fake_grade(test_file: str, impl_files: list[str], lint_files: list[str]) -> Grade:
        graded.append(test_file)
        return outcome

    # Stand in for grading in the container, and for asking Docker for the image's id.
    monkeypatch.setattr(ground_truth_module, "grade_in_environment", fake_grade)
    monkeypatch.setattr(ground_truth_module, "container_image_id", lambda image: "image-1")
    task = {
        "task_id": "repository/tic-tac-toe",
        "repo_copy_test_file": "/s/daml/Test.daml",
        "repo_copy_impl_files": ["/s/daml/Impl.daml"],
        "log_prefix": "",
    }

    first = ground_truth_module.run_ground_truth_control(repo_copy_digest="digest-1", **task)
    assert (first.passed(), first.cached) == (True, False)
    assert first.image_id == "image-1"
    assert first.grade.test_results == {"Test:main": True}

    # Same task, same content, same toolchain: replayed without a build.
    second = ground_truth_module.run_ground_truth_control(repo_copy_digest="digest-1", **task)
    assert (second.passed(), second.cached) == (True, True)
    assert graded == ["/s/daml/Test.daml"]

    # Edited copy: different key, so it is built again.
    third = ground_truth_module.run_ground_truth_control(repo_copy_digest="digest-2", **task)
    assert third.cached is False
    assert len(graded) == 2

    # A failure is never cached: it may be transient, so it must be re-established.
    outcome = replace(outcome, tests_passed=False)
    failed = ground_truth_module.run_ground_truth_control(repo_copy_digest="digest-3", **task)
    assert (failed.passed(), failed.cached) == (False, False)
    again = ground_truth_module.run_ground_truth_control(repo_copy_digest="digest-3", **task)
    assert (again.passed(), again.cached) == (False, False)
    assert len(graded) == 4


def test_rate_limited_attempts_keep_their_audit_trail() -> None:
    """A retried task carries the network and tool activity of the attempts it discarded."""
    final = _attempt(
        egress=_egress(("api.openai.com", False)),
        forbidden_tool_calls=[ForbiddenToolCall(ForbiddenTool.WEB_SEARCH, None, None, None, None, ItemEvent.STARTED)],
        suspicious_commands=[SuspiciousCommand("c2", "curl api.openai.com", "api.openai.com")],
    )
    prior = _attempt(
        egress=_egress(("pypi.org", True)),
        suspicious_commands=[SuspiciousCommand("c1", "pip install requests", "env")],
    )

    merged = carry_prior_attempt_audits(final, [prior])

    assert [event["host"] for event in merged.egress.events] == ["pypi.org", "api.openai.com"]
    assert [c.command for c in merged.suspicious_commands] == ["pip install requests", "curl api.openai.com"]
    assert [c.type for c in merged.forbidden_tool_calls] == ["web_search"]
    # The summary is recomputed over both attempts, not carried from the last one.
    assert merged.egress.blocked_domains == ["pypi.org"]
    assert merged.egress.blocked_attempts_detected() is True
    assert merged.egress.domains_observed == ["api.openai.com", "pypi.org"]
    # Nothing else about the attempt changes, and the original is untouched.
    assert merged.returncode == final.returncode
    assert [event["host"] for event in final.egress.events] == ["api.openai.com"]


def test_no_prior_attempts_returns_the_same_record() -> None:
    attempt = _attempt()
    assert carry_prior_attempt_audits(attempt, []) is attempt


def test_thread_start_params_carry_codexs_own_confinement_policy() -> None:
    """Codex is told to confine nothing, under its own `sandbox` parameter.

    The container is the confinement. If this parameter goes missing, codex confines the
    agent as well and a Daml build cannot run.
    """
    params = thread_start_params(DEFAULTS, "/workspace")
    assert params["sandbox"] == CODEX_SANDBOX
    assert params["approvalPolicy"] == CODEX_APPROVAL_POLICY
    assert (params["cwd"], params["ephemeral"]) == ("/workspace", True)
    assert "config" not in params

    with_effort = thread_start_params(DEFAULTS.merge(SimpleNamespace(codex_app_server_effort="high")), "/workspace")
    assert with_effort["config"] == {"model_reasoning_effort": "high"}
