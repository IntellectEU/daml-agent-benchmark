"""Per-run integrity scan of a prepared repository copy.

Runs after repository-copy preparation and before the implementation files are blanked,
on every task of every run. It answers two questions about the tree the agent is
about to see:

- Does any file other than a target carry a target's ground-truth content? This
  covers plain copies (a handler materialising a symlinked source as a file),
  containment inside a larger file, copies whose layout or comments differ, and
  archives of any format that bundle the target's source. A run of a target's lines
  found elsewhere is reported under `partial_matches` instead: these libraries
  duplicate code between sibling modules, so it informs how a score reads rather
  than invalidating the task.
- Is anything present that must never be in a copy: a `.git` directory or a
  symlink that resolves outside the copy?

Symlink aliases of a target (same realpath) are correct wiring and are not
reported. While reading every file the scan also computes a content digest of the
copy, which callers use as a cache key for work that depends only on the
copy's content (for example the ground-truth control build).
"""

from __future__ import annotations

import hashlib
import os
import re
import tarfile
import time
import zipfile
from pathlib import Path

_MIN_TARGET_CHARS = 40
# Reformatting defeats an exact search, so each target also gets a comment-free,
# whitespace-collapsed form, and a set of windows over its significant lines to catch
# a copy of part of it. The thresholds keep boilerplate from matching: a window has to
# carry real content before its presence elsewhere means anything.
_MIN_NORMALISED_CHARS = 120
_WINDOW_LINES = 8
_WINDOW_STRIDE = 4
_MIN_WINDOW_CHARS = 120
_MIN_LINE_CHARS = 12
# Daml declarations are shared by design: an interface package and its implementation
# name the same fields, and every service repeats `controller`/`signatory`/`template`.
# A window therefore has to contain lines that do something before its presence
# elsewhere means a copy; otherwise idiomatic structure reads as a leak.
_MIN_CODE_LINES_PER_WINDOW = 2
_CODE_LINE_RE = re.compile(r"(<-|=[^=]|\bdo\b|\bcreate\b|\bexercise\b|\bfetch\b|\bassert|\bif\b|\bcase\b|\blet\b)")
# An archive holding a whole target, however formatted, is the answer in compiled form
# and is deleted. Everything else the scan finds is for a person to judge.
_PRUNE_KINDS = frozenset({"archive_entry_content", "archive_entry_content_reformatted"})
_MAX_ARCHIVE_ENTRY_BYTES = 8 * 1024 * 1024
# A single compressed file (a log, a tarball) is searched up to this decompressed size.
_MAX_COMPRESSED_STREAM_BYTES = 64 * 1024 * 1024
# A DAR bundles a module's source at its module path; the compiled forms of the same
# module are just as much the answer, so the name check covers them too.
MODULE_SUFFIXES = (".daml", ".dalf", ".hi", ".hie")
ZIP_MAGIC = b"PK\x03\x04"
GZIP_MAGIC = b"\x1f\x8b"


class RepoCopyIntegrityError(RuntimeError):
    pass


def _read_targets(target_files: list[str]) -> dict[str, bytes]:
    """Each target's bytes, read once; everything the scan knows about a target derives
    from these. Raises OSError for a target that cannot be read."""
    return {target: Path(target).read_bytes() for target in target_files}


def _target_bodies(raw: dict[str, bytes]) -> dict[str, bytes]:
    """The targets long enough to fingerprint, stripped."""
    bodies = {target: data.strip() for target, data in raw.items()}
    return {target: body for target, body in bodies.items() if len(body) >= _MIN_TARGET_CHARS}


_MODULE_DECL_RE = re.compile(r"^\s*module\s+([A-Za-z0-9_.]+)", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"\{-.*?-\}", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--.*$", re.MULTILINE)


def _target_module_paths(raw: dict[str, bytes]) -> set[str]:
    """Module paths implied by each target's `module A.B.C` declaration, e.g. `A/B/C`.

    A DAR bundles its modules under exactly this path, so matching on it identifies
    the target module itself; matching on the bare file name would trip on every
    dependency that has a module with the same last component. Comments are stripped
    first: a commented-out declaration would otherwise name the wrong module.
    """
    paths: set[str] = set()
    for data in raw.values():
        text = data.decode("utf-8", errors="replace")
        text = _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))
        match = _MODULE_DECL_RE.search(text)
        if match is not None:
            paths.add(match.group(1).replace(".", "/"))
    return paths


def _entry_is_target_module(entry_name: str, target_module_paths: set[str]) -> bool:
    """Whether an archive entry is a target module in source or compiled form."""
    for suffix in MODULE_SUFFIXES:
        if not entry_name.endswith(suffix):
            continue
        stem = entry_name[: -len(suffix)]
        if any(stem == module_path or stem.endswith("/" + module_path) for module_path in target_module_paths):
            return True
    return False


def module_paths_of_targets(target_files: list[str]) -> set[str]:
    """The module paths of the targets, as a DAR names their entries."""
    return _target_module_paths(_read_targets([str(t) for t in target_files]))


def fingerprints_of_targets(target_files: list[str]) -> dict[str, dict]:
    """The content fingerprints of the targets long enough to have one."""
    return _target_fingerprints(_target_bodies(_read_targets([str(t) for t in target_files])))


def holds_whole_target(data: bytes, fingerprints: dict[str, dict]) -> bool:
    """Whether `data` holds a whole target, exact or reformatted, as the DAR prune requires."""
    return any(f["kind"] in _PRUNE_KINDS for f in _content_findings(data, fingerprints, "archive_entry_", partial=False))


_WHITESPACE_RE = re.compile(r"\s+")


def _normalise_source(data: bytes) -> str:
    """Comment-free, whitespace-collapsed text, so layout changes cannot hide a copy."""
    text = data.decode("utf-8", errors="replace")
    text = _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _significant_lines(data: bytes) -> list[str]:
    """The target's lines, comment-free and whitespace-collapsed, minus the trivial ones."""
    text = data.decode("utf-8", errors="replace")
    text = _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))
    lines = (_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    return [line for line in lines if len(line) >= _MIN_LINE_CHARS]


def _window_is_distinctive(lines: list[str]) -> bool:
    return sum(1 for line in lines if _CODE_LINE_RE.search(line)) >= _MIN_CODE_LINES_PER_WINDOW


def _target_fingerprints(bodies: dict[str, bytes]) -> dict[str, dict]:
    """Per target: its exact bytes, its normalised text, and windows over its lines."""
    fingerprints: dict[str, dict] = {}
    for target, body in bodies.items():
        normalised = _normalise_source(body)
        lines = _significant_lines(body)
        windows = []
        for start in range(0, max(len(lines) - _WINDOW_LINES + 1, 0), _WINDOW_STRIDE):
            window_lines = lines[start : start + _WINDOW_LINES]
            window = " ".join(window_lines)
            if len(window) >= _MIN_WINDOW_CHARS and _window_is_distinctive(window_lines):
                windows.append(window)
        fingerprints[target] = {
            "body": body,
            "normalised": normalised if len(normalised) >= _MIN_NORMALISED_CHARS else None,
            "windows": windows,
        }
    return fingerprints


def _content_findings(data: bytes, fingerprints: dict[str, dict], prefix: str, *, partial: bool = True) -> list[dict]:
    """Ways `data` can carry a target's ground truth, from exact copy to reformatted part.

    The exact check runs first and stands alone; the reformatted checks only run when
    it misses, so an ordinary copy is reported once rather than three times. Without
    `partial`, parts of a target are not searched.
    """
    findings: list[dict] = []
    normalised: str | None = None
    # The windows are built from significant lines only, so they are searched in a
    # haystack built the same way; searching the full text would never match, because
    # it still carries the short lines the windows drop.
    windowed: str | None = None
    for target, fingerprint in fingerprints.items():
        if fingerprint["body"] in data:
            findings.append({"kind": f"{prefix}content", "target": target})
            continue
        if normalised is None:
            normalised = _normalise_source(data)
        if fingerprint["normalised"] is not None and fingerprint["normalised"] in normalised:
            findings.append({"kind": f"{prefix}content_reformatted", "target": target})
            continue
        if not partial or not fingerprint["windows"]:
            continue
        if windowed is None:
            windowed = " ".join(_significant_lines(data))
        matched = next((w for w in fingerprint["windows"] if w in windowed), None)
        if matched is not None:
            findings.append({"kind": f"{prefix}content_partial", "target": target, "excerpt": matched[:120]})
    return findings


def _scan_archive_members(
    members: list[tuple[str, int, object]],
    read: object,
    fingerprints: dict[str, dict],
    target_module_paths: set[str],
    depth: int,
) -> list[dict]:
    findings: list[dict] = []
    for entry_name, size, handle in members:
        if _entry_is_target_module(entry_name, target_module_paths):
            findings.append({"kind": "archive_entry_name", "archive_entry": entry_name})
        if size > _MAX_ARCHIVE_ENTRY_BYTES:
            continue
        data = read(handle)  # type: ignore[operator]
        if data is None:
            continue
        for finding in _content_findings(data, fingerprints, "archive_entry_"):
            findings.append({**finding, "archive_entry": entry_name})
        # A DAR inside a DAR hides its entries from a single pass.
        if depth < 1 and data[:4] == ZIP_MAGIC:
            for nested in _scan_archive_bytes(data, fingerprints, target_module_paths, depth + 1):
                findings.append({**nested, "archive_entry": f"{entry_name}!{nested['archive_entry']}"})
    return findings


def _scan_archive_bytes(
    data: bytes, fingerprints: dict[str, dict], target_module_paths: set[str], depth: int
) -> list[dict]:
    import io

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [(info.filename, info.file_size, info) for info in archive.infolist()]
            return _scan_archive_members(members, archive.read, fingerprints, target_module_paths, depth)
    except (zipfile.BadZipFile, OSError):
        return []


def _scan_archive(path: Path, fingerprints: dict[str, dict], target_module_paths: set[str]) -> list[dict]:
    """Findings for one archive: entries naming a target module, or containing its source.

    Dispatches on the file's magic bytes rather than its name, so a renamed or
    differently packaged archive (`.jar`, `.zip`, `.tar.gz`, `Foo.DAR`) is opened too.
    An archive that cannot be read is itself a finding: an unreadable container is an
    unverifiable one.
    """
    try:
        with open(path, "rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        return [{"kind": "unreadable", "detail": str(exc)}]

    if magic[:4] == ZIP_MAGIC:
        try:
            with zipfile.ZipFile(path) as archive:
                members = [(info.filename, info.file_size, info) for info in archive.infolist()]
                return _scan_archive_members(members, archive.read, fingerprints, target_module_paths, 0)
        except (zipfile.BadZipFile, OSError) as exc:
            return [{"kind": "unreadable_archive", "detail": str(exc)}]

    if magic[:2] == GZIP_MAGIC or path.name.lower().endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
        try:
            with tarfile.open(path) as archive:
                members = [(m.name, m.size, m) for m in archive.getmembers() if m.isfile()]

                def read_tar(member: object) -> bytes | None:
                    stream = archive.extractfile(member)  # type: ignore[arg-type]
                    return stream.read() if stream is not None else None

                return _scan_archive_members(members, read_tar, fingerprints, target_module_paths, 0)
        except (tarfile.TarError, OSError):
            # Gzip without a tar inside is a single compressed file, which the
            # source repositories are full of (Canton logs). Search what it holds: a
            # gzipped copy of a target would be invisible in its compressed bytes.
            return _scan_compressed_stream(path, fingerprints)
    return []


def _scan_compressed_stream(path: Path, fingerprints: dict[str, dict]) -> list[dict]:
    import gzip

    try:
        with gzip.open(path, "rb") as stream:
            data = stream.read(_MAX_COMPRESSED_STREAM_BYTES)
    except (OSError, EOFError) as exc:
        return [{"kind": "unreadable_archive", "detail": str(exc)}]
    return [{**f, "archive_entry": path.name} for f in _content_findings(data, fingerprints, "compressed_")]


def scan_repo_copy_integrity(repo_copy_dir: str | Path, target_files: list[str]) -> dict:
    """Scan every file under `repo_copy_dir` for ground-truth leaks and forbidden entries."""
    started = time.monotonic()
    repo_copy_root = Path(repo_copy_dir).resolve()
    offending: list[dict] = []
    # A run of a target's lines found elsewhere is reported but does not fail the task:
    # these source repositories duplicate implementation code between sibling modules,
    # which we cannot remove and did not cause. It still changes how a score should be
    # read, so it travels with the task as a warning.
    partial_matches: list[dict] = []
    try:
        raw_targets = _read_targets(target_files)
    except OSError as exc:
        # A target we cannot read cannot be fingerprinted, so nothing can be verified.
        raw_targets = {}
        offending.append({"path": "<target>", "kind": "target_unreadable", "detail": str(exc)})
    bodies = _target_bodies(raw_targets)
    fingerprints = _target_fingerprints(bodies)
    target_module_paths = _target_module_paths(raw_targets)
    target_realpaths = {os.path.realpath(target) for target in target_files}
    git_entries: list[str] = []
    symlinks_outside: list[dict] = []
    file_hashes: list[tuple[str, str]] = []
    files_scanned = 0
    bytes_scanned = 0

    def on_walk_error(exc: OSError) -> None:
        # A directory the walk cannot enter could hold anything, so it fails the scan
        # rather than silently contributing nothing.
        offending.append({"path": str(getattr(exc, "filename", "?")), "kind": "unreadable_dir", "detail": str(exc)})

    for current_dir, dirnames, filenames in os.walk(repo_copy_root, followlinks=False, onerror=on_walk_error):
        if ".git" in dirnames:
            git_entries.append(str(Path(current_dir, ".git").relative_to(repo_copy_root)))
            dirnames.remove(".git")
        for name in list(dirnames):
            dir_path = Path(current_dir) / name
            if dir_path.is_symlink():
                real = Path(os.path.realpath(dir_path))
                if not real.is_relative_to(repo_copy_root):
                    symlinks_outside.append({"path": str(dir_path.relative_to(repo_copy_root)), "resolves_to": str(real)})
        for name in filenames:
            path = Path(current_dir) / name
            rel_path = str(path.relative_to(repo_copy_root))
            if name == ".git":
                git_entries.append(rel_path)
            if path.is_symlink():
                real = Path(os.path.realpath(path))
                if not real.is_relative_to(repo_copy_root):
                    symlinks_outside.append({"path": rel_path, "resolves_to": str(real)})
                    continue
            if os.path.realpath(path) in target_realpaths:
                continue
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                offending.append({"path": rel_path, "kind": "unreadable", "detail": str(exc)})
                continue
            files_scanned += 1
            bytes_scanned += len(data)
            file_hashes.append((rel_path, hashlib.sha256(data).hexdigest()))
            for finding in _content_findings(data, fingerprints, ""):
                target_list = partial_matches if finding["kind"].endswith("_partial") else offending
                target_list.append({"path": rel_path, **finding})
            for finding in _scan_archive(path, fingerprints, target_module_paths):
                target_list = partial_matches if finding["kind"].endswith("_partial") else offending
                target_list.append({"path": rel_path, **finding})

    # The targets are excluded from the leak scan but are part of the content the
    # digest must describe: a ground-truth change alone must change the digest.
    for target, data in raw_targets.items():
        target_path = Path(target).resolve()
        rel = str(target_path.relative_to(repo_copy_root)) if target_path.is_relative_to(repo_copy_root) else target
        file_hashes.append((rel, hashlib.sha256(data).hexdigest()))

    digest = hashlib.sha256()
    for rel_path, file_hash in sorted(file_hashes):
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")

    # A target too short to fingerprint is not a clean result, it is an unverifiable
    # one: nothing would have detected a copy of it.
    skipped_short = sorted(set(raw_targets) - set(bodies))
    return {
        "ok": not offending and not git_entries and not symlinks_outside and not skipped_short,
        "offending": offending,
        "partial_matches": partial_matches,
        "git_entries": git_entries,
        "symlinks_outside_repo_copy": symlinks_outside,
        "targets_checked": sorted(bodies),
        "targets_skipped_too_short": skipped_short,
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "repo_copy_digest": digest.hexdigest(),
    }


def prune_dars_containing_targets(repo_copy_dir: str | Path, target_files: list[str]) -> list[dict]:
    """Delete prebuilt DARs in the copy that bundle a target module's source.

    Build leftovers are gitignored, so whether a `.lib`, `.dars` or `build` directory
    holds a compiled copy of the package under test depends on what was built on this
    machine, not on the repository. Any such DAR hands the agent the answer, so it is
    removed here rather than in each repo's handler, and `scan_repo_copy_integrity`
    verifies afterwards that none is left.

    A task whose build genuinely needs a prebuilt copy of the package under test was
    never measuring the agent; removing the archive makes its build fail, which the
    ground-truth control reports instead of hiding.

    Only an archive that actually contains a target's current content is deleted. An
    archive that merely has an entry at the target's module path is a different
    version of that module, which the scan then fails loudly: deleting a vendored
    prior release would silently break a build that legitimately depends on it, so
    that case is a decision for a person, not for this function. A partial match is
    left to the scan for the same reason.
    """
    repo_copy_root = Path(repo_copy_dir)
    raw_targets = _read_targets([str(t) for t in target_files])
    fingerprints = _target_fingerprints(_target_bodies(raw_targets))
    module_paths = _target_module_paths(raw_targets)
    if not fingerprints:
        return []
    removed: list[dict] = []
    for archive_path in sorted(repo_copy_root.rglob("*.dar")):
        if not archive_path.is_file() or archive_path.is_symlink():
            continue
        findings = [f for f in _scan_archive(archive_path, fingerprints, module_paths) if f["kind"] in _PRUNE_KINDS]
        if not findings:
            continue
        archive_path.unlink()
        removed.append(
            {
                "path": str(archive_path.relative_to(repo_copy_root)),
                "entries": sorted({str(finding["archive_entry"]) for finding in findings})[:5],
            }
        )
    return removed


def describe_integrity_failure(report: dict) -> str:
    problems: list[str] = []
    if report["offending"]:
        problems.append(f"ground-truth content or unreadable entries outside the targets: {report['offending'][:20]}")
    if report["targets_skipped_too_short"]:
        problems.append(
            f"targets too short to fingerprint, so a copy of them would go undetected: {report['targets_skipped_too_short']}"
        )
    if report["git_entries"]:
        problems.append(f".git entries present: {report['git_entries']}")
    if report["symlinks_outside_repo_copy"]:
        problems.append(f"symlinks resolve outside the copy: {report['symlinks_outside_repo_copy']}")
    return "; ".join(problems)


def assert_repo_copy_integrity(report: dict) -> None:
    if not report["ok"]:
        raise RepoCopyIntegrityError(describe_integrity_failure(report))
