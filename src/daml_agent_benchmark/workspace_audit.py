"""Detect and classify files the agent changed in its workspace.

Grading never looks at the agent's copy of anything except the implementation
files (only those are copied back to the host's copy), so an agent that edits
the test file or a `daml.yaml` cannot influence its score. It can still make its
own transcript misleading: an agent that weakens the test, sees it pass in the
container and reports success looks, to a reader of the trace, like an ordinary
failure. The container wrapper therefore hashes the workspace before the run and
again after it, and the orchestrator classifies every difference.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

# Directories whose contents change during any normal run: build output, direnv
# state and the per-container dpm home. Changes in them carry no tamper signal, so
# the wrapper does not report them at all.
_WORKSPACE_CHANGE_IGNORED_DIR_NAMES = frozenset({".daml", ".direnv-codex", "node_modules", ".dpm", ".dpm-cache"})

# Inside the task codex home only codex's own session state is left out of the
# hash; everything else there is reported, in its own bucket, for the trace audit.
_CODEX_HOME_DIR_NAME = ".codex_home"
_CODEX_HOME_IGNORED_SUBDIRS = frozenset({"sessions", "log", "logs", "shell_snapshots", "tmp", "archived_sessions"})
_CODEX_HOME_IGNORED_FILE_PREFIXES = ("history", "state_", "memories", "lock")

# Suffixes whose non-target modification would change what a build or test means.
_SOURCE_SUFFIXES = frozenset(
    {".daml", ".yaml", ".yml", ".envrc", ".toml", ".cabal", ".json", ".sh", ".mk", ".nix", ".dar"}
)
# Directories that hold what `daml build` writes. A DAR appearing there is the agent
# compiling its own work; a DAR appearing anywhere else replaces a dependency.
_BUILD_OUTPUT_DIR_NAMES = frozenset({"build", "dist", "target"})


def _default_ignore(rel_dir: str, name: str, is_dir: bool) -> bool:
    """Whether to skip an entry at `rel_dir/name` in a workspace hash."""
    if is_dir and name in _WORKSPACE_CHANGE_IGNORED_DIR_NAMES:
        return True
    if rel_dir == _CODEX_HOME_DIR_NAME:
        if is_dir:
            return name in _CODEX_HOME_IGNORED_SUBDIRS
        return name.startswith(_CODEX_HOME_IGNORED_FILE_PREFIXES) or ".sqlite" in name
    return False


def no_ignore(rel_dir: str, name: str, is_dir: bool) -> bool:
    return False


def hash_tree(root: str | Path, *, ignore: Callable[[str, str, bool], bool] = _default_ignore) -> dict[str, str]:
    """Return {relative path: sha256} for every regular file under root, minus ignored entries."""
    root_path = Path(root)
    hashes: dict[str, str] = {}
    for current_dir, dirnames, filenames in os.walk(root_path, followlinks=False):
        rel_dir = Path(current_dir).relative_to(root_path).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [name for name in dirnames if not ignore(rel_dir, name, True)]
        for name in filenames:
            if ignore(rel_dir, name, False):
                continue
            path = Path(current_dir) / name
            if path.is_symlink() or not path.is_file():
                continue
            rel_path = path.relative_to(root_path).as_posix()
            hashes[rel_path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


class ChangeKind(StrEnum):
    """What happened to one file the agent changed.

    The workspace audit derives the kind from two hashes of the workspace, so it always
    knows which of the first three this is. Codex names the same three its own way in its
    event stream, and an unrecognised name there becomes `UNKNOWN`. The path is what
    carries the security signal, so a name we do not know must not cost us the record.
    """

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    UNKNOWN = "unknown"


def diff_tree_hashes(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    """List added, modified and deleted paths between two hash_tree() results."""
    changes: list[dict] = []
    for rel_path in sorted(set(before) | set(after)):
        if rel_path not in before:
            changes.append({"path": rel_path, "kind": ChangeKind.ADDED})
        elif rel_path not in after:
            changes.append({"path": rel_path, "kind": ChangeKind.DELETED})
        elif before[rel_path] != after[rel_path]:
            changes.append({"path": rel_path, "kind": ChangeKind.MODIFIED})
    return changes


def remove_added_files(root: str | Path, before: dict[str, str]) -> dict[str, list[str]]:
    """Delete every file under root that was not in `before`; prune the directories this empties.

    Undoes the side effects of a build that ran inside a copy before the agent
    sees it. Files that existed before but changed are reported, not touched: their
    original content is gone, so the caller has to decide what that means.
    """
    root_path = Path(root)
    after = hash_tree(root_path, ignore=no_ignore)
    removed: list[str] = []
    modified: list[str] = []
    for change in diff_tree_hashes(before, after):
        if change["kind"] == ChangeKind.MODIFIED:
            modified.append(change["path"])
            continue
        if change["kind"] != ChangeKind.ADDED:
            continue
        path = root_path / change["path"]
        path.unlink(missing_ok=True)
        removed.append(change["path"])
        parent = path.parent
        while parent != root_path and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return {"removed": removed, "modified": modified}


def classify_workspace_changes(changes: list[dict], impl_rel_paths: list[str], test_rel_path: str) -> dict:
    """Split workspace changes into expected implementation edits and everything else.

    `source_changes_outside_targets` is the tamper signal: a changed source-like
    file that is not one of the implementation files the task asked for. The test
    file itself is listed separately because it is the highest-value target. Files
    under a build-output directory are the agent's own build products and are
    listed as `build_outputs`, not as tampering.
    """
    impl_set = {Path(p).as_posix() for p in impl_rel_paths}
    test_posix = Path(test_rel_path).as_posix()
    impl_changes: list[dict] = []
    test_file_changes: list[dict] = []
    source_changes: list[dict] = []
    build_outputs: list[dict] = []
    codex_home_changes: list[dict] = []
    other_changes: list[dict] = []
    for change in changes:
        rel_path = Path(change["path"]).as_posix()
        if rel_path in impl_set:
            impl_changes.append(change)
        elif rel_path == test_posix:
            test_file_changes.append(change)
        elif rel_path.startswith(_CODEX_HOME_DIR_NAME + "/"):
            # Codex rewrites its config.toml and materialises bundled skills at
            # startup, so these edits cannot be told apart from the agent's by hash.
            # They are reported for the trace reader but are not the tamper signal.
            codex_home_changes.append(change)
        elif _BUILD_OUTPUT_DIR_NAMES & set(Path(rel_path).parts[:-1]):
            build_outputs.append(change)
        elif Path(rel_path).suffix in _SOURCE_SUFFIXES or Path(rel_path).name in _SOURCE_SUFFIXES:
            source_changes.append(change)
        else:
            other_changes.append(change)
    return {
        "impl_changes": impl_changes,
        "test_file_changes": test_file_changes,
        "source_changes_outside_targets": source_changes,
        "build_outputs": build_outputs,
        "codex_home_changes": codex_home_changes,
        "other_changes": other_changes,
        "tamper_suspected": bool(test_file_changes or source_changes),
    }
