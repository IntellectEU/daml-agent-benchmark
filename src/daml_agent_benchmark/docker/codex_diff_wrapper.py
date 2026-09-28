#!/usr/bin/env python3
"""Runs codex inside the container and enriches file_change events with before/after diffs.

This is the container side. `codex_in_container.py` copies this file in and makes it the
container's command; codex is its child process, and everything codex writes passes
through here.

Codex runs as `codex app-server` and emits JSON-RPC notifications on stdout,
``{"method": "item/started", "params": {"item": {...}}}``.

For file-change items:
  - started: we snapshot the current file contents (the "before" state)
  - completed: we read the file again (the "after" state), compute a unified
    diff, and embed a ``_file_change_diffs`` key into the JSON message before
    forwarding it to stdout.

The host-side Python orchestrator checks for ``_file_change_diffs`` and uses it
directly instead of trying to read container-local paths from the host.

All non-JSON lines and non-file_change messages are forwarded unchanged.
"""

import difflib
import json
import subprocess
import sys
import threading


MAX_DIFF_CHARS = 40_000
MAX_SNAPSHOT_CHARS = 200_000
FILE_CHANGE_ITEM_TYPE = "fileChange"


def safe_read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def clip_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (clipped at {max_chars} chars)"


def compute_diff(before: str | None, after: str | None, path: str) -> str:
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    from_label = "/dev/null" if before is None else f"a/{path}"
    to_label = "/dev/null" if after is None else f"b/{path}"
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=from_label,
            tofile=to_label,
            n=3,
            lineterm="",
        )
    )
    diff_text = "\n".join(diff_lines)
    if diff_text:
        diff_text = clip_text(diff_text, MAX_DIFF_CHARS)
    return diff_text


def file_change_phase_and_item(message: dict) -> tuple[str | None, dict | None]:
    """Return ("started"|"completed", item) for a file-change notification."""
    method = str(message.get("method") or "")
    params = message.get("params")
    item = params.get("item") if isinstance(params, dict) else None
    phase = {"item/started": "started", "item/completed": "completed"}.get(method)
    if phase is None or not isinstance(item, dict):
        return None, None
    if str(item.get("type") or "") != FILE_CHANGE_ITEM_TYPE:
        return None, None
    return phase, item


def build_diffs_for_event(
    event: dict,
    file_content_cache: dict[str, str | None],
    pending_before: dict[str, dict[str, str | None]],
) -> list[dict] | None:
    """Return a list of diff records for file_change events, or None if not applicable."""
    phase, item = file_change_phase_and_item(event)
    if item is None:
        return None
    item_id = str(item.get("id") or "").strip()
    changes_raw = item.get("changes")
    changes = changes_raw if isinstance(changes_raw, list) else []

    if phase == "started":
        before_snapshots: dict[str, str | None] = {}
        for change in changes:
            if not isinstance(change, dict):
                continue
            change_path = str(change.get("path") or "").strip()
            if not change_path:
                continue
            content = safe_read_text(change_path)
            before_snapshots[change_path] = content
            file_content_cache[change_path] = content
        if item_id and before_snapshots:
            pending_before[item_id] = before_snapshots
        return None

    # completed
    before_snapshots = pending_before.pop(item_id, {})
    out: list[dict] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        change_path = str(change.get("path") or "").strip()
        if not change_path:
            continue
        # Codex gives the kind as an object, {"type": ..., "move_path": ...}.
        kind = change.get("kind")
        kind = str((kind.get("type") if isinstance(kind, dict) else kind) or "")
        before_content = before_snapshots.get(change_path, file_content_cache.get(change_path))
        after_content = safe_read_text(change_path)

        diff_text = compute_diff(before_content, after_content, change_path)

        out.append(
            {
                "path": change_path,
                "kind": kind,
                "before_missing": before_content is None,
                "after_missing": after_content is None,
                "before_snapshot": clip_text(before_content, MAX_SNAPSHOT_CHARS)
                if before_content is not None
                else None,
                "after_snapshot": clip_text(after_content, MAX_SNAPSHOT_CHARS) if after_content is not None else None,
                "diff_unified": diff_text or None,
            }
        )
        file_content_cache[change_path] = after_content
    return out if out else None


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: codex_diff_wrapper.py <codex-command> [args...]", file=sys.stderr)
        return 2

    # First code to run in the container: assemble the container-private DPM_HOME
    # from the read-only SDK store mount (see setup_dpm_home.sh for why).
    setup = subprocess.run(["bash", "/opt/setup_dpm_home.sh"], capture_output=True, text=True, check=False)
    if setup.returncode != 0:
        print(f"[diff-wrapper] dpm home setup failed: {setup.stderr or setup.stdout}", file=sys.stderr, flush=True)
        return 1

    proc = subprocess.Popen(
        sys.argv[1:],
        stdin=sys.stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    file_content_cache: dict[str, str | None] = {}
    pending_before: dict[str, dict[str, str | None]] = {}

    def forward_stderr():
        assert proc.stderr is not None
        for line in iter(proc.stderr.readline, ""):
            sys.stderr.write(line)
            sys.stderr.flush()

    stderr_thread = threading.Thread(target=forward_stderr, daemon=True)
    stderr_thread.start()

    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ""):
        stripped = line.strip()
        if not stripped.startswith("{"):
            sys.stdout.write(line)
            sys.stdout.flush()
            continue

        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            sys.stdout.write(line)
            sys.stdout.flush()
            continue

        diffs = build_diffs_for_event(event, file_content_cache, pending_before)
        if diffs is not None:
            event["_file_change_diffs"] = diffs

        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    returncode = proc.wait()
    stderr_thread.join(timeout=3)
    return returncode


if __name__ == "__main__":
    sys.exit(main())
