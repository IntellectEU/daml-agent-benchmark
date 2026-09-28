"""Host-side management of the SDK store that benchmark containers mount.

Daml SDKs are large (~2GB each) and static, so they are kept once on the host —
NOT inside the Docker image — and mounted read-only into every agent and eval
container at /opt/daml and /opt/dpm. This keeps the image small (~3GB instead of
~30GB), makes rebuilds fast, and means adding a new SDK version is an
incremental download instead of a full image rebuild.

The store is populated by running bootstrap_sdk_store.sh inside the eval image
with the store mounted read-write and network enabled. The host only ever
deletes from it: the example projects every SDK ships for `daml new` are removed,
because the benchmark's tutorial tasks are those very examples and an agent could
read the finished answer from the mounted store. For the same reason any other task
answer in the store is removed. SDK jars bundle example DARs whose packages are some
tasks' answers, and a DAR that depends on such a package bundles it too. Copies of a
task's source, in an archive or as a plain file, go with their compiled forms.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import struct
import subprocess
import tarfile
import zipfile
from pathlib import Path

from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.repo_copy_integrity import (
    GZIP_MAGIC,
    MODULE_SUFFIXES,
    ZIP_MAGIC,
    fingerprints_of_targets,
    holds_whole_target,
    module_paths_of_targets,
)

SDK_STORE_ROOT = Path(os.environ.get("DAML_AGENT_SDK_STORE", str(Path.home() / "daml-sdk-store")))
_BOOTSTRAP_SCRIPTS_DIR = Path(__file__).resolve().parent / "docker" / "sdk_store_bootstrap"


def sdk_store_mount_args(*, read_only: bool = True) -> list[str]:
    """Docker -v flags that give a container access to the SDK store.

    Read-only (benchmark containers): daml mounts at its final home; the dpm store
    mounts at /opt/dpm-store, from which the container assembles a private
    /opt/dpm at startup (dpm opens manifests read-write — see setup_dpm_home.sh).
    Read-write (the bootstrap container, the store's only writer): both mount at
    their real homes so the install tools write the store directly.
    """
    if read_only:
        return [
            "-v", f"{SDK_STORE_ROOT / 'daml'}:/opt/daml:ro",
            "-v", f"{SDK_STORE_ROOT / 'dpm'}:/opt/dpm-store:ro",
        ]
    return [
        "-v", f"{SDK_STORE_ROOT / 'daml'}:/opt/daml",
        "-v", f"{SDK_STORE_ROOT / 'dpm'}:/opt/dpm",
    ]


def _missing_sdk_versions(sdk_versions: list[str]) -> list[str]:
    """Return the requested SDK versions not yet present in the store."""
    sdk_dir = SDK_STORE_ROOT / "daml" / "sdk"
    return [v for v in sdk_versions if not (sdk_dir / v).is_dir()]


def _store_is_bootstrapped() -> bool:
    """Whether the store has its base tooling (assistant + dpm) installed."""
    return (SDK_STORE_ROOT / "daml" / "bin" / "daml").exists() and (SDK_STORE_ROOT / "dpm" / "bin" / "dpm").exists()


def ensure_sdk_store(
    sdk_versions: list[str],
    *,
    image: str,
    env: dict[str, str],
    answer_files: list[str],
    docker_bin: str = "docker",
) -> None:
    """Make sure the store exists, has the given SDK versions, and holds no task answer.

    Installing does nothing when every version is already there. Otherwise it runs the
    bootstrap script in a Docker container with network access, the only writer of the
    store. `answer_files` are the files the selected tasks ask the agent to write. Any
    copy of them is then removed from the store.
    """
    missing = _missing_sdk_versions(sdk_versions)
    print(f"SDK store: required {', '.join(sdk_versions)}; missing {', '.join(missing) or 'none'}", flush=True)
    if not (_store_is_bootstrapped() and not missing):
        (SDK_STORE_ROOT / "daml").mkdir(parents=True, exist_ok=True)
        (SDK_STORE_ROOT / "dpm").mkdir(parents=True, exist_ok=True)
        print(
            f"Bootstrapping SDK store at {SDK_STORE_ROOT} (missing: {missing or 'base tooling'})",
            flush=True,
        )
        extra = locations.sdk_bootstrap_extra_dir
        extra_scripts_mount = ["-v", f"{extra}:/bootstrap-scripts-extra:ro"] if extra is not None else []
        cmd = [
            docker_bin,
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            *sdk_store_mount_args(read_only=False),
            "-v",
            f"{_BOOTSTRAP_SCRIPTS_DIR}:/bootstrap-scripts:ro",
            *extra_scripts_mount,
            *(arg for name, value in env.items() for arg in ("-e", f"{name}={value}")),
            "--entrypoint",
            "bash",
            image,
            "/bootstrap-scripts/bootstrap_sdk_store.sh",
            *sdk_versions,
        ]
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"SDK store bootstrap failed (rc={proc.returncode})")
    prune_project_templates()
    strip_answers_from_archives(answer_files)


def _project_template_dirs() -> list[Path]:
    """The example projects in the store: the `templates` directory of every classic SDK
    version, and the projects inside dpm's `daml-new` component. Only the projects are
    listed for dpm, not the component: dpm's SDK manifest declares `daml-new` as part of
    the SDK and refuses to build anything when the component directory is missing."""
    sdk_templates = (SDK_STORE_ROOT / "daml" / "sdk").glob("*/templates")
    dpm_projects = (SDK_STORE_ROOT / "dpm" / "cache" / "components" / "daml-new").glob("*/daml-new-dpm/resources/*")
    return sorted([*sdk_templates, *(d for d in dpm_projects if d.is_dir())])


def prune_project_templates() -> list[Path]:
    """Delete the example projects from the store and return what was removed.

    The benchmark's tutorial tasks are the SDK's own examples, so the finished answer
    would otherwise sit in the read-only mount every agent container gets. Runs after
    every install because a new SDK version brings its examples along.
    """
    removed = _project_template_dirs()
    for path in removed:
        shutil.rmtree(path)
        print(f"SDK store: removed example projects {path}", flush=True)
    return removed


# SDK jars bundle example DARs (canton's CantonExamples.dar), and DARs carry their modules'
# source. A jar or a DAR inside a jar, and a jar inside a downloaded tarball, are opened.
_MAX_ARCHIVE_NESTING = 2
# The scan keeps the text of these entries, so answers are matched by content from the cache.
_CACHED_SOURCE_SUFFIX = ".daml"
_ARCHIVE_SCAN_CACHE_NAME = "archive-module-entries.json"


def _read_magic(path: Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(262)


def _is_tar(magic: bytes) -> bool:
    return magic[:2] == GZIP_MAGIC or magic[257:262] == b"ustar"


def _source_text(name: str, data: bytes) -> str | None:
    return data.decode("utf-8", errors="replace") if name.endswith(_CACHED_SOURCE_SUFFIX) else None


def _zip_module_entries(archive: zipfile.ZipFile, depth: int) -> dict[str, str | None]:
    """The entries of a zip, and of the zips inside it, that are Daml modules or packages, with each source's text."""
    entries: dict[str, str | None] = {}
    for info in archive.infolist():
        if info.filename.endswith(_CACHED_SOURCE_SUFFIX):
            entries[info.filename] = _source_text(info.filename, archive.read(info))
        elif info.filename.endswith(MODULE_SUFFIXES):
            entries[info.filename] = None
        if depth >= _MAX_ARCHIVE_NESTING or info.is_dir() or info.file_size < 4:
            continue
        with archive.open(info) as entry:
            if entry.read(4) != ZIP_MAGIC:
                continue
        with zipfile.ZipFile(io.BytesIO(archive.read(info))) as nested:
            entries.update({f"{info.filename}!{name}": text for name, text in _zip_module_entries(nested, depth + 1).items()})
    return entries


def _tar_module_entries(path: Path) -> dict[str, str | None]:
    entries: dict[str, str | None] = {}
    try:
        with tarfile.open(path) as archive:
            for member in archive:
                if not member.isfile():
                    continue
                data = archive.extractfile(member).read()  # type: ignore[union-attr]
                if member.name.endswith(MODULE_SUFFIXES):
                    entries[member.name] = _source_text(member.name, data)
                if data[:4] == ZIP_MAGIC:
                    with zipfile.ZipFile(io.BytesIO(data)) as nested:
                        entries.update({f"{member.name}!{name}": text for name, text in _zip_module_entries(nested, 1).items()})
    except tarfile.ReadError:
        # A gzip that holds a single file rather than a tar has no entries to name.
        return {}
    return entries


def _archive_module_entries(path: Path) -> dict[str, str | None]:
    """Daml module and package entries anywhere inside the archive at `path`, as `outer!inner` names.

    A plain module file is its own entry, named "". Its source is read from disk when needed.
    """
    magic = _read_magic(path)
    if magic[:4] == ZIP_MAGIC:
        with zipfile.ZipFile(path) as archive:
            return _zip_module_entries(archive, 0)
    if _is_tar(magic):
        return _tar_module_entries(path)
    if path.name.endswith(MODULE_SUFFIXES):
        return {"": None}
    return {}


# Raised whenever what the scan records changes, so older cache entries are not trusted.
# A test pins a digest of the scan code to this number, so a change without a raise fails it.
_ARCHIVE_SCAN_VERSION = 3


def _store_files() -> list[Path]:
    """Every regular file in the store; symlinks point at files listed in their own right."""
    files: list[Path] = []
    for directory in ("daml", "dpm"):
        for current_dir, _dirnames, filenames in os.walk(SDK_STORE_ROOT / directory):
            for name in filenames:
                path = Path(current_dir) / name
                if not path.is_symlink() and path.is_file():
                    files.append(path)
    return sorted(files)


def _store_module_entries() -> dict[Path, dict[str, str | None]]:
    """Daml module and package entries inside every archive in the store, and its plain module files.

    Opening every jar takes about a minute, so the result is cached per file by size
    and modification time in a file at the store root, which containers do not mount.
    """
    cache_path = SDK_STORE_ROOT / _ARCHIVE_SCAN_CACHE_NAME
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    cached = cache.get("archives", {}) if cache.get("version") == _ARCHIVE_SCAN_VERSION else {}
    fresh: dict[str, dict] = {}
    result: dict[Path, dict[str, str | None]] = {}
    for path in _store_files():
        rel = os.path.relpath(path, SDK_STORE_ROOT)
        info = path.stat()
        entry = cached.get(rel)
        if entry is None or entry["size"] != info.st_size or entry["mtime_ns"] != info.st_mtime_ns:
            entry = {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "entries": _archive_module_entries(path)}
        fresh[rel] = entry
        if entry["entries"]:
            result[path] = entry["entries"]
    if fresh != cached:
        tmp_path = cache_path.with_name(cache_path.name + ".tmp")
        tmp_path.write_text(json.dumps({"version": _ARCHIVE_SCAN_VERSION, "archives": fresh}), encoding="utf-8")
        os.replace(tmp_path, cache_path)
    return result


def _by_container(entries: dict[str, str | None]) -> dict[str, dict[str, str | None]]:
    """Entries grouped by the zip or tar that holds them; "" is the archive itself."""
    grouped: dict[str, dict[str, str | None]] = {}
    for entry, text in entries.items():
        container, _, name = entry.rpartition("!")
        grouped.setdefault(container, {})[name] = text
    return grouped


def _dar_package_dir(path: Path, container: str, names: dict[str, str | None]) -> str | None:
    """The package directory when `container` in the archive at `path` is a DAR; None otherwise.

    A DAR keeps its package in one directory, with the main `.dalf` under the directory's
    own name and each module's source at its module path. Only zips nest, so a container
    other than the archive itself is a zip.
    """
    if not container and _read_magic(path)[:4] != ZIP_MAGIC:
        return None
    for name in names:
        package_dir, _, file_name = name.partition("/")
        if file_name == f"{package_dir}.dalf":
            return package_dir
    return None


def _holds_answer(text: str | None, fingerprints: dict[str, dict]) -> bool:
    return text is not None and holds_whole_target(text.encode("utf-8"), fingerprints)


def _loose_answers(names: dict[str, str | None], fingerprints: dict[str, dict], answer_dalfs: set[str]) -> list[str]:
    """The files among `names` that hold an answer on their own, to be removed one by one.

    `names` are the files of one zip or tar, or of one directory in the store. Each maps
    to its text for a `.daml` source, and to None otherwise. Files inside a bundled Daml
    package that is removed whole are not passed here. Examples of what this finds are a
    `.daml` in a zip of sources, a plain file in the store, or a stray `.dalf` in a jar.

    A target is a file a task asks the agent to write. A `.daml` file goes when its text
    holds a whole target. A `.hi` or `.hie` file of the same name goes with it, because
    damlc compiled them from that source. A `.hie` file embeds its source too, but the
    scan keeps no `.hie` text, since the store's `.hie` files add up to hundreds of
    megabytes. So a `.hi` or `.hie` file with no matching `.daml` beside it stays. A
    `.dalf` file goes when it has the file name of an answer package's compiled code.
    """
    matched = {name.removesuffix(_CACHED_SOURCE_SUFFIX) for name, text in names.items() if _holds_answer(text, fingerprints)}
    return [
        name
        for name in names
        if name.rpartition("/")[2] in answer_dalfs
        or (name.endswith((".daml", ".hi", ".hie")) and name.rpartition(".")[0] in matched)
    ]


def find_answers_in_store_archives(answer_files: list[str]) -> list[dict]:
    """The task answers in the store, as a list of things to remove.

    `answer_files` are the files the tasks ask the agent to write, called targets here.
    Each item has a `path` in the store and an `entry`, the file inside that archive to
    remove. An empty `entry` means the whole file at `path`. Inside nested archives,
    `entry` reads `outer!inner`.

    A bundled Daml package (a DAR) is removed whole when its source for a target's module
    holds that target. A DAR that bundles such a package as a dependency is removed whole
    too. Any other copy of a target's source is removed on its own, with its compiled forms
    beside it, and so is a stray copy of an answer package's compiled `.dalf`. A source
    holds a target when it contains the whole target, exact or reformatted. That is the
    same test the repository copy scan passes before it deletes a DAR from a task's copy.
    """
    module_paths = module_paths_of_targets(answer_files)
    fingerprints = fingerprints_of_targets(answer_files)
    if not fingerprints:
        return []
    store = _store_module_entries()
    archives = {path: _by_container(entries) for path, entries in store.items() if "" not in entries}
    whole: dict[Path, list[str]] = {}
    answer_dalfs: set[str] = set()
    for path, grouped in archives.items():
        for container, names in grouped.items():
            package_dir = _dar_package_dir(path, container, names)
            if package_dir is not None and any(
                _holds_answer(names.get(f"{package_dir}/{module_path}.daml"), fingerprints) for module_path in module_paths
            ):
                whole.setdefault(path, []).append(container)
                answer_dalfs.add(f"{package_dir}.dalf")
    # A DAR bundles the compiled `.dalf` of every package it depends on, directly or not,
    # under that package's file name. So a DAR that depends on an answer package holds the
    # answer in compiled form. Cutting that `.dalf` out would break the DAR, so it goes whole.
    for path, grouped in archives.items():
        for container, names in grouped.items():
            if _inside_any(container, whole.get(path, [])) or _dar_package_dir(path, container, names) is None:
                continue
            if any(name.rpartition("/")[2] in answer_dalfs for name in names):
                whole.setdefault(path, []).append(container)
    findings = [{"path": path, "entry": container} for path, containers in whole.items() for container in containers]
    for path, grouped in archives.items():
        for container, names in grouped.items():
            if _inside_any(container, whole.get(path, [])):
                continue
            prefix = f"{container}!" if container else ""
            findings.extend({"path": path, "entry": prefix + name} for name in _loose_answers(names, fingerprints, answer_dalfs))
    plain_by_dir: dict[Path, dict[str, str | None]] = {}
    for path, entries in store.items():
        if "" in entries:
            text = path.read_text(encoding="utf-8", errors="replace") if path.name.endswith(_CACHED_SOURCE_SUFFIX) else None
            plain_by_dir.setdefault(path.parent, {})[path.name] = text
    for directory, names in plain_by_dir.items():
        findings.extend({"path": directory / name, "entry": ""} for name in _loose_answers(names, fingerprints, answer_dalfs))
    return findings


def _inside_any(container: str, packages: list[str]) -> bool:
    """Whether the zip `container` lies inside one of `packages`, the bundled packages removed whole.

    Both name a zip by its place inside an archive in the store, such as `CantonExamples.dar`
    for a DAR in a jar, or `lib/canton.jar!CantonExamples.dar` for one in a jar in a tarball.
    "" names the archive itself. A file inside a package that is removed whole goes with it,
    so it needs no item of its own on the list of things to remove.
    """
    return any(not package or container == package or container.startswith(package + "!") for package in packages)


def _copy_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """A new header for a file inside a zip, with the original's name, date, compression method and attributes."""
    copy = zipfile.ZipInfo(info.filename, info.date_time)
    copy.compress_type = info.compress_type
    copy.comment = info.comment
    copy.create_system = info.create_system
    copy.create_version = info.create_version
    copy.external_attr = info.external_attr
    copy.internal_attr = info.internal_attr
    # zipfile adds its own zip64 field, which large files need, when it writes the file.
    copy.extra = _without_zip64_field(info.extra)
    return copy


def _without_zip64_field(extra: bytes) -> bytes:
    kept = b""
    offset = 0
    while offset + 4 <= len(extra):
        header_id, size = struct.unpack("<HH", extra[offset : offset + 4])
        if header_id != 0x0001:
            kept += extra[offset : offset + 4 + size]
        offset += 4 + size
    return kept


def _nested_names(remove: set[str], outer: str) -> set[str]:
    """The files in `remove` that lie inside the nested zip `outer`, named relative to it."""
    return {name[len(outer) + 1 :] for name in remove if name.startswith(outer + "!")}


def _strip_zip(source: object, destination: object, remove: set[str]) -> None:
    """Copy the zip `source` to `destination` without the files named in `remove`.

    The other files keep their order and compression method. `outer!inner` names the file
    `inner` inside the nested zip `outer`."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(destination, "w") as dst:  # type: ignore[arg-type]
        for info in src.infolist():
            if info.filename in remove:
                continue
            data = src.read(info)
            nested = _nested_names(remove, info.filename)
            if nested:
                data = _strip_nested_zip(data, nested)
            copy = _copy_zip_info(info)
            dst.writestr(copy, data)
            # zipfile gives a file with no attributes owner-only permissions. The zip records
            # attributes only in its index, written on close, so setting them here keeps the original's.
            copy.external_attr = info.external_attr
        dst.comment = src.comment


def _strip_nested_zip(data: bytes, remove: set[str]) -> bytes:
    stripped = io.BytesIO()
    _strip_zip(io.BytesIO(data), stripped, remove)
    return stripped.getvalue()


def _strip_tar(source: Path, destination: Path, remove: set[str]) -> None:
    """Copy the tarball `source` to `destination` without the files named in `remove`, gzipped if it was."""
    gzipped = _read_magic(source)[:2] == GZIP_MAGIC
    output = tarfile.open(destination, "w:gz", compresslevel=6) if gzipped else tarfile.open(destination, "w")
    with tarfile.open(source) as src, output as dst:
        for member in src:
            if not member.isfile():
                dst.addfile(member)
                continue
            if member.name in remove:
                continue
            data = src.extractfile(member).read()  # type: ignore[union-attr]
            nested = _nested_names(remove, member.name)
            if nested:
                data = _strip_nested_zip(data, nested)
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data))


def strip_answers_from_archives(answer_files: list[str]) -> list[dict]:
    """Remove the task answers that `find_answers_in_store_archives` finds, and return them per file.

    An archive is rewritten without the files inside it that hold an answer. Every other
    file inside it keeps its content, position and compression method. A plain file that
    holds an answer is deleted. This runs after every install, because a new SDK version
    brings new jars. It does nothing once no answer is left.
    """
    by_file: dict[Path, list[str]] = {}
    for finding in find_answers_in_store_archives(answer_files):
        by_file.setdefault(finding["path"], []).append(finding["entry"])
    stripped: list[dict] = []
    for path, entries in by_file.items():
        is_zip = _read_magic(path)[:4] == ZIP_MAGIC
        if "" in entries and is_zip:
            # The archive is itself a DAR that must go whole. Nothing can be cut out of it, so it
            # stays, and assert_store_archives_free_of_answers refuses to run.
            continue
        if "" in entries:
            path.unlink()
            print(f"SDK store: removed task answer {path}", flush=True)
        else:
            tmp_path = path.with_name(f".{path.name}.{os.getpid()}.strip-tmp")
            try:
                if is_zip:
                    _strip_zip(path, tmp_path, set(entries))
                else:
                    _strip_tar(path, tmp_path, set(entries))
                os.chmod(tmp_path, stat.S_IMODE(path.stat().st_mode))
                os.replace(tmp_path, path)
            finally:
                tmp_path.unlink(missing_ok=True)
            print(f"SDK store: removed {len(entries)} task answer entries from {path}", flush=True)
        stripped.append({"path": path, "entries": entries})
    return stripped


def assert_store_archives_free_of_answers(answer_files: list[str]) -> None:
    """Stop the run while any file in the store still holds a task answer, naming up to 20 of them."""
    findings = find_answers_in_store_archives(answer_files)
    if findings:
        listed = "\n".join(f"  {f['path']}!{f['entry']}" if f["entry"] else f"  {f['path']}" for f in findings[:20])
        raise RuntimeError(f"SDK store holds task answers; every agent container can read them:\n{listed}")
