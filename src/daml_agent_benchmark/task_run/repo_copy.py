"""Preparing the repository copy one task gives the agent.

Each task runs against its own copy of the source repository, with the implementation
files emptied. The preflight check runs once per run, before any copy is made.
"""

from __future__ import annotations

import re
from pathlib import Path

from daml_agent_benchmark.constants import (
    CONTAINER_IMAGE,
    CONTAINER_REQUIRE_NIX_FOR_NIX_TASKS,
    REPO_COPY_DIR_SPLITTER,
)
from daml_agent_benchmark.container.image import container_has_command
from daml_agent_benchmark.context_guardrails import make_repo_copy_path_filter
from daml_agent_benchmark.repos.registry import handler_for
from daml_agent_benchmark.repos.repo_copy import prepare_repo_copy, restore_deps_needed_for_build
from daml_agent_benchmark.repo_copy_integrity import prune_dars_containing_targets
from daml_agent_benchmark.tasklist_catalog import repo_root_for_path


def _envrc_requires_nix_shell(envrc_path: Path) -> bool:
    """Check if a .envrc file contains a `use nix` directive, meaning the repository needs
    nix-shell to set up its build environment."""
    try:
        text = envrc_path.read_text(encoding="utf-8")
    except Exception:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^use\s+nix(?:\s|$)", stripped):
            return True
    return False


def _repos_requiring_nix_shell(tasks: dict[str, list[str]]) -> list[Path]:
    """Find all source repositories among the selected tasks that need nix-shell.
    Used in preflight to fail early if nix isn't available in the container."""
    required: list[Path] = []
    seen: set[Path] = set()
    for test_file in tasks.keys():
        repo_root = Path(repo_root_for_path(test_file)).resolve()
        if repo_root in seen:
            continue
        seen.add(repo_root)
        envrc_path = repo_root / ".envrc"
        if _envrc_requires_nix_shell(envrc_path):
            required.append(repo_root)
    return sorted(required)


def assert_nix_shell_preflight(tasks: dict[str, list[str]]) -> None:
    """Check that nix-shell is available inside the container image (not on the host)
    if any selected tasks need it. Fails early rather than letting tasks fail at runtime."""
    if not CONTAINER_REQUIRE_NIX_FOR_NIX_TASKS:
        return

    required_repos = _repos_requiring_nix_shell(tasks)
    if not required_repos:
        return

    if container_has_command("nix-shell"):
        return

    repo_lines = "\n".join(f"  - {p}" for p in required_repos)
    raise RuntimeError(
        "Preflight failed: selected tasks require `nix-shell`, but it is not available in "
        f"container image {CONTAINER_IMAGE!r}.\n"
        "The following reference libs have `.envrc` with `use nix`:\n"
        f"{repo_lines}\n"
        "Install nix in the image or switch tasks."
    )


def prepare_task_repo_copy(
    repo_root: str,
    original_impl_files: list[str],
    run_repo_copies_dir: str,
    log_prefix: str = "",
    test_file_path: str | None = None,
) -> tuple[str, list[dict]]:
    """Create an isolated copy of a source repository for one task to run in.

    Returns the copy path and the prebuilt archives the last step deleted from it.

    Each task gets its own copy so the agent can freely modify files without affecting
    other tasks or the original source. Uses deep copies (cp -a) instead of hard links
    (cp -al) because in dangerous mode the agent could modify hard-linked files and
    corrupt the originals.
    """
    repo_copy_dir = prepare_repo_copy(
        repo_root,
        original_impl_files,
        run_repo_copies_dir,
        generated_path_filter=make_repo_copy_path_filter(repo_root, original_impl_files),
        node_modules_mode="copy",
        log_prefix=log_prefix,
        repo_copy_dir_splitter=REPO_COPY_DIR_SPLITTER,
    )
    # The copy so far is a filtered copy of the repo's source files. For some repos
    # that is not yet buildable: they need extra preparation, like fetching dependency
    # DARs or fixing up files the copy could not represent. The repository's handler
    # knows what its repo is missing, and says whether it should run on this path (its
    # `restore_in_repo_copy` flag): other repos' tasks build fine from the plain copy,
    # and their handlers serve only repository copies that other callers prepare.
    if handler_for(Path(repo_root).name).restore_in_repo_copy:
        # Tell the handler which files the task is about: the implementation files
        # (which will be blanked) plus the test file. Handlers use this list both to
        # know what to keep buildable and — critically — to know which compiled
        # artifacts must NOT be restored: a prebuilt DAR of a target package contains
        # the very code the agent is supposed to write.
        target_files = [Path(f) for f in original_impl_files]
        if test_file_path is not None:
            target_files.append(Path(test_file_path))
        restore_deps_needed_for_build(repo_root, repo_copy_dir, target_files)

    # Last step for every repo, after any handler: a prebuilt DAR of a target package
    # is the agent's answer in compiled form. Build leftovers are gitignored, so one
    # can be present on one machine and absent on another; pruning here keeps the
    # hazard out of every copy instead of relying on each handler to avoid it.
    removed_dars = prune_dars_containing_targets(repo_copy_dir, original_impl_files)
    for removed in removed_dars:
        print(f"{log_prefix}removed prebuilt archive carrying a target module: {removed['path']}", flush=True)
    return repo_copy_dir, removed_dars
