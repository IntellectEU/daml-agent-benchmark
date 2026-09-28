"""Repository copy preparation: copying a repository, restoring the dependencies that the copy needs to build."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from daml_agent_benchmark.constants import REPO_COPY_DIR_SPLITTER
from daml_agent_benchmark.repos.registry import handler_for

DEFAULT_IGNORED_NAMES = frozenset(
    {
        ".daml",
        "build",
        "contracts-exposed",
        "cache",
        "artifacts",
        "THIS_IS_THE_OG_DOT_GIT_FOLDER",
        ".git",
    }
)

PathFilter = Callable[[str], bool]
NodeModulesMode = Literal["none", "copy", "hardlink"]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi_codes(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _copy_node_modules(source_root: Path, target_root: Path, mode: NodeModulesMode) -> None:
    if mode == "none":
        return
    source_node_modules = source_root / "node_modules"
    if not source_node_modules.is_dir():
        return
    dest_node_modules = target_root / "node_modules"
    flag = "-a" if mode == "copy" else "-al"
    subprocess.run(["cp", flag, str(source_node_modules), str(dest_node_modules)], check=True)


def copy_source_tree(
    source_root: str | Path,
    target_root: str | Path,
    *,
    ignored_names: frozenset[str] = DEFAULT_IGNORED_NAMES,
    generated_path_filter: PathFilter | None = None,
    node_modules_mode: NodeModulesMode = "none",
    symlinks: bool = False,
    ignore_nested_ignored_names: bool = True,
) -> None:
    """Copy source_root into target_root while skipping build/generated artifacts."""
    source_root = Path(source_root)
    target_root = Path(target_root)
    target_root.mkdir(parents=True, exist_ok=True)

    def copy_ignore(current_dir: str, names: list[str]) -> list[str]:
        ignored: list[str] = []
        for name in names:
            if ignore_nested_ignored_names and name in ignored_names:
                ignored.append(name)
                continue
            candidate = os.path.join(current_dir, name)
            if generated_path_filter is not None and generated_path_filter(candidate):
                ignored.append(name)
        return ignored

    _copy_node_modules(source_root, target_root, node_modules_mode)

    for item_name in os.listdir(source_root):
        if item_name in ignored_names or item_name == "node_modules":
            continue
        src_path = source_root / item_name
        dest_path = target_root / item_name
        if generated_path_filter is not None and generated_path_filter(str(src_path)):
            continue
        if symlinks and src_path.is_symlink():
            os.symlink(os.readlink(src_path), dest_path)
        elif src_path.is_dir():
            # ignore_dangling_symlinks: some repositories (e.g. canton) contain broken symlinks
            # (generated files absent in the checkout); with symlinks=False copytree would
            # otherwise abort the whole copy trying to dereference them.
            shutil.copytree(
                src_path, dest_path, symlinks=symlinks, ignore=copy_ignore, ignore_dangling_symlinks=True
            )
        elif src_path.is_file():
            shutil.copy2(src_path, dest_path)


def _copy_impl_files_into_repo_copy(
    source_root: str | Path,
    target_root: str | Path,
    original_impl_files: list[str],
) -> None:
    source_root = Path(source_root)
    target_root = Path(target_root)
    for impl_file_path in original_impl_files:
        impl_path = Path(impl_file_path)
        relative_path = os.path.relpath(impl_path, source_root)
        path_in_repo_copy = target_root / relative_path
        path_in_repo_copy.parent.mkdir(parents=True, exist_ok=True)
        if path_in_repo_copy.is_dir():
            shutil.rmtree(path_in_repo_copy)
        elif path_in_repo_copy.exists() or path_in_repo_copy.is_symlink():
            path_in_repo_copy.unlink()
        shutil.copy2(impl_path, path_in_repo_copy)


def _authorize_direnv_if_available(target_root: str | Path, *, log_prefix: str = "") -> None:
    target_root = Path(target_root)
    src_envrc = target_root / ".envrc"
    if not src_envrc.exists():
        return
    direnv_bin = shutil.which("direnv")
    if direnv_bin:
        allow_proc = subprocess.run(
            [direnv_bin, "allow"],
            cwd=target_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if allow_proc.returncode == 0:
            return
        src_envrc.unlink()
        err = (allow_proc.stderr or allow_proc.stdout or "").strip().splitlines()
        err_tail = strip_ansi_codes(err[-1]) if err else "unknown error"
        print(
            f"{log_prefix}[repo-copy] `direnv allow` failed; disabling direnv for this copy: {err_tail}",
            flush=True,
        )
        return
    src_envrc.unlink()
    print(f"{log_prefix}[repo-copy] direnv not found; skipping `direnv allow`", flush=True)


def prepare_repo_copy(
    repo_root: str,
    original_impl_files: list[str],
    run_repo_copies_dir: str,
    *,
    generated_path_filter: PathFilter | None,
    node_modules_mode: NodeModulesMode,
    log_prefix: str = "",
    repo_copy_dir_splitter: str = REPO_COPY_DIR_SPLITTER,
    ignore_nested_ignored_names: bool = True,
) -> str:
    """Create a task's repository copy and copy original implementation files."""
    repo_root = Path(repo_root)
    repo_copy_dir = Path(tempfile.mkdtemp(prefix=f"{repo_root.name}{repo_copy_dir_splitter}", dir=run_repo_copies_dir))
    copy_source_tree(
        repo_root,
        repo_copy_dir,
        generated_path_filter=generated_path_filter,
        node_modules_mode=node_modules_mode,
        symlinks=False,
        ignore_nested_ignored_names=ignore_nested_ignored_names,
    )
    _copy_impl_files_into_repo_copy(repo_root, repo_copy_dir, original_impl_files)
    _authorize_direnv_if_available(repo_copy_dir, log_prefix=log_prefix)
    return str(repo_copy_dir)


def _restore_daml_dist_dirs(repo_root: str | Path, repo_copy: str | Path, target_files: list[Path]) -> None:
    """Copy .daml/dist from non-target packages so data-dependencies resolve."""
    repo_root = Path(repo_root)
    repo_copy = Path(repo_copy)
    target_resolved = {Path(t).resolve() for t in target_files}
    for dist_dir in repo_root.rglob(".daml/dist"):
        pkg_dir = dist_dir.parent.parent
        if any(t.is_relative_to(pkg_dir.resolve()) for t in target_resolved):
            continue
        if not any(dist_dir.iterdir()):
            continue
        dest = repo_copy / dist_dir.relative_to(repo_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dist_dir, dest, dirs_exist_ok=True)


def _command_prefix_for_repo_copy(repo_copy: Path) -> list[str]:
    repo_copy = repo_copy.resolve()
    if not (repo_copy / ".envrc").exists() or shutil.which("direnv") is None:
        return []
    direnv_config = repo_copy / ".direnv-codex"
    direnv_config.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DIRENV_CONFIG"] = str(direnv_config)
    env["XDG_DATA_HOME"] = str(direnv_config / "data")
    env["XDG_CONFIG_HOME"] = str(direnv_config / "config")
    env["DIRENV_LOG_FORMAT"] = ""
    allow_proc = subprocess.run(["direnv", "allow"], cwd=repo_copy, env=env, capture_output=True, text=True, check=False)
    if allow_proc.returncode != 0:
        return []
    return [
        "env",
        f"DIRENV_CONFIG={env['DIRENV_CONFIG']}",
        f"XDG_DATA_HOME={env['XDG_DATA_HOME']}",
        f"XDG_CONFIG_HOME={env['XDG_CONFIG_HOME']}",
        f"DIRENV_LOG_FORMAT={env['DIRENV_LOG_FORMAT']}",
        "direnv",
        "exec",
        str(repo_copy),
    ]


def restore_deps_needed_for_build(
    repo_root: str | Path,
    repo_copy: str | Path,
    target_files: list[Path],
    *,
    deps_cache: Path | None = None,
) -> None:
    """Make the copy buildable: restore compiled dependencies of non-target packages,
    then run the repository's own handler when it has one."""
    repo_root = Path(repo_root)
    repo_copy = Path(repo_copy)
    handler = handler_for(repo_root.name)
    if handler.restore_dist_dirs:
        _restore_daml_dist_dirs(repo_root, repo_copy, target_files)
    if handler.restore_deps is None:
        return
    if handler.restore_deps_takes_deps_cache:
        handler.restore_deps(
            repo_root, repo_copy, target_files, deps_cache=deps_cache
        )
    else:
        handler.restore_deps(repo_root, repo_copy, target_files)
