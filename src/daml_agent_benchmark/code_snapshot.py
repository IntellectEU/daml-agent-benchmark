import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

from daml_agent_benchmark.constants import PACKAGE_DIR, WRAPPER_PATH
from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.repos.registry import load_builtin_handlers


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp_path.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _zip_directory(source_dir: Path, zip_path: Path, root_name: str) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(source_dir)
            zf.write(path, arcname=str(Path(root_name) / rel))


def _snapshot_roots() -> list[tuple[str, Path]]:
    """The package's directory and each extra code directory, with the kind of code each holds."""
    roots = [("package", PACKAGE_DIR.resolve())]
    roots += [("extra", extra_dir.resolve()) for extra_dir in locations.extra_code_dirs]
    names = [root.name for _, root in roots]
    if len(set(names)) != len(names):
        raise ValueError(f"code snapshot roots must have distinct directory names: {names}")
    return roots


def _unimported_package_files() -> list[Path]:
    """The package's files that run without being imported.

    These are every file under `docker/`, which the harness copies into containers or
    builds images from, and the container wrapper, which runs as the codex executable.
    """
    docker_dir = PACKAGE_DIR / "docker"
    files = [path for path in docker_dir.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    return [*files, WRAPPER_PATH]


def _loaded_module_files() -> list[Path]:
    """The source file of every loaded module that has one."""
    files: list[Path] = []
    for module in list(sys.modules.values()):
        module_file = getattr(module, "__file__", None)
        if module_file and module_file.endswith(".py"):
            files.append(Path(module_file))
    return files


def _task_list_files() -> list[Path]:
    """The files of every task list the run reads.

    They say which files each task empties and at which commit each repository is pinned. A
    task-list directory can also hold a `handlers.py`, whose `__pycache__` is left out.
    """
    return [
        path
        for tasklist_dir in locations.tasklist_dirs
        for path in tasklist_dir.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]


def stage_code_snapshot_for_run(run_dir: Path) -> dict[str, object]:
    """Snapshot the harness code of this process: its loaded modules, the package's unimported files and the task lists.

    Only files under the package's directory or an extra code directory are included.
    The built-in repository handlers are loaded first, since the registry imports them lazily.
    Each file is stored under its root's directory name.
    """
    load_builtin_handlers()
    roots = _snapshot_roots()
    candidates = {path.resolve() for path in [*_loaded_module_files(), *_unimported_package_files(), *_task_list_files()]}
    source_root = run_dir / "artifacts" / "code_snapshot" / "files"
    source_root.mkdir(parents=True, exist_ok=True)

    files: list[dict[str, str]] = []
    for source_file in sorted(path for path in candidates if path.is_file()):
        for kind, root in roots:
            if source_file.is_relative_to(root):
                rel = Path(root.name) / source_file.relative_to(root)
                files.append({"path": rel.as_posix(), "root": kind})
                dest = source_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, dest)
                break

    manifest: dict[str, object] = {
        "strategy": "loaded_modules",
        "file_count": len(files),
        "files": files,
    }
    manifest_path = run_dir / "artifacts" / "code_snapshot" / "manifest.json"
    _write_json_atomic(manifest_path, manifest)

    snapshot_zip = run_dir / "artifacts" / "code_snapshot" / "files.zip"
    _zip_directory(source_root, snapshot_zip, root_name="files")
    snapshot_zip_sha256 = _sha256_file(snapshot_zip)

    print(
        f"[snapshot] code snapshot {snapshot_zip} (files={len(files)}, sha256={snapshot_zip_sha256})",
        flush=True,
    )
    return {
        **manifest,
        "manifest_path": str(manifest_path),
        "snapshot_dir": str(source_root),
        "snapshot_zip": str(snapshot_zip),
        "snapshot_zip_sha256": snapshot_zip_sha256,
    }
