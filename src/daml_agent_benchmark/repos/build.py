"""Daml build command selection shared by copy prep and validation."""

from __future__ import annotations

import os
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from daml_agent_benchmark.constants import REPO_COPY_DIR_SPLITTER
from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.tasklist_catalog import load_repos


def repo_name_for_package_path(path: str | Path) -> str | None:
    """Resolve the owning repository's name for any path inside it.

    Understands the two layouts in which a package path can appear: the canonical checkout
    under the sources root, and repository copies named <repository><SPLITTER><suffix>.
    """
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(locations.sources_root.resolve()).parts[0]
    except (ValueError, IndexError):
        pass
    parts = resolved.parts
    for part in parts:
        if REPO_COPY_DIR_SPLITTER in part:
            return part.split(REPO_COPY_DIR_SPLITTER, 1)[0]
    return None


def _package_uses_dpm(package_root: str | Path) -> bool:
    """Whether packages at this path build and test through dpm rather than the daml assistant.

    The task list's repos.yaml routes every repository by name. Config heuristics proved
    unreliable (classic 3.4.x also expands ${DAML_VERSION}; multi-package roots often have no
    daml.yaml), so a path that resolves to no declared repository is an error, not a guess.
    """
    repo_name = repo_name_for_package_path(package_root)
    if repo_name is None:
        raise ValueError(f"Cannot resolve a repository for {package_root!s}; declare it in the task list's repos.yaml.")
    repos = load_repos()
    if repo_name not in repos:
        raise KeyError(f"Repository {repo_name!r} is not declared in the task list's repos.yaml.")
    return repos[repo_name].build_tool == "dpm"


class _EvalState(threading.local):
    """Ambient per-thread state telling _run() to exec commands in an eval container.

    Set by eval_in_container; ambient (rather than a parameter) so the container id
    doesn't have to be threaded through every build/test call site. Per-thread because
    benchmark tasks run in parallel threads, each with its own eval container — a module
    global would collide across tasks. Subclassing threading.local means __init__ runs
    once per accessing thread, so every thread sees these defaults (container_id=None ⇒
    run on host) even if it never entered a container.
    """

    def __init__(self) -> None:
        self.container_id: str | None = None
        self.docker_bin: str = "docker"


_eval_state = _EvalState()


def in_eval_container() -> bool:
    """Whether this thread's commands are currently routed into an eval container."""
    return _eval_state.container_id is not None


@contextmanager
def eval_in_container(
    mount_root: str | Path,
    image: str,
    *,
    platform: str = "linux/amd64",
    docker_bin: str = "docker",
    env: dict[str, str] | None = None,
) -> Iterator[str]:
    """Route this thread's subsequent _run() commands into a fresh Linux eval container.

    The eval copy is bind-mounted at its identical host path, so command cwds resolve
    unchanged and the container's own (Linux) SDKs are used — letting old-SDK repos
    (e.g. ex-models' 1.16, whose scenario service SIGABRTs on macOS) evaluate
    consistently. `env` entries become container environment variables (overriding
    same-named image ENV defaults) — used to inject .envrc exports for repos whose
    daml.yaml is ${VAR}-parameterized, since direnv can't run in the container. Only
    wrap the FINAL evaluation (ground-truth/agent build+test) with this — never copy
    prep, whose repository handlers must run on the host. Not re-entrant per thread.

    The container has no network (`daml test` only needs loopback) and runs as the
    host user, like the agent container. What it mounts is the host's copy, so
    running as root would leave root-owned build outputs in it that the host cannot
    clean up or copy back; and a component download would hide an SDK-store gap
    behind a passing eval.
    """
    if _eval_state.container_id is not None:
        raise RuntimeError("eval_in_container is not re-entrant within a thread")
    from daml_agent_benchmark.sdk_store import sdk_store_mount_args

    resolved_root = str(Path(mount_root).resolve())
    env_args = [arg for name, value in (env or {}).items() for arg in ("-e", f"{name}={value}")]
    start = subprocess.run(
        [docker_bin, "run", "-d", "--platform", platform, "--entrypoint", "sleep",
         "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
         "--tmpfs", "/tmp:exec,mode=1777", "-e", "HOME=/tmp/container-home", *env_args,
         *sdk_store_mount_args(read_only=True),
         "-v", f"{resolved_root}:{resolved_root}", "-w", resolved_root, image, "infinity"],
        capture_output=True, text=True, check=False,
    )
    if start.returncode != 0:
        raise RuntimeError(f"Failed to start eval container:\n{start.stderr or start.stdout}")
    container_id = start.stdout.strip()
    # Assemble the container-private DPM_HOME from the read-only store mount.
    setup = subprocess.run(
        [docker_bin, "exec", container_id, "bash", "/opt/setup_dpm_home.sh"],
        capture_output=True, text=True, check=False,
    )
    if setup.returncode != 0:
        subprocess.run([docker_bin, "rm", "-f", container_id], capture_output=True, text=True, check=False)
        raise RuntimeError(f"Eval container dpm setup failed:\n{setup.stderr or setup.stdout}")
    _eval_state.container_id = container_id
    _eval_state.docker_bin = docker_bin
    try:
        yield _eval_state.container_id # Currently don't actually use this result. Could be useful for future callers.
    finally:
        container_id = _eval_state.container_id
        _eval_state.container_id = None
        subprocess.run([docker_bin, "rm", "-f", container_id], capture_output=True, text=True, check=False)


def _run(
    command: list[str], cwd: str | Path, *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a build/test command, either on the host or inside this thread's eval container.

    All daml/dpm invocations in this module go through here, so the decision "host or
    container?" lives in exactly one place. It is made from ambient state: if the current
    thread is inside an `eval_in_container` block, the command is rewritten to
    `docker exec -w <cwd> <container> <command>` and runs in that container; otherwise it
    runs directly on the host. Callers cannot tell the difference — same arguments, same
    CompletedProcess back — which works because eval containers mount the copy at its
    identical host path, so `cwd` is valid in both worlds.
    """
    if _eval_state.container_id is not None:
        docker_command = [_eval_state.docker_bin, "exec", "-w", str(cwd), _eval_state.container_id, *command]
        return subprocess.run(docker_command, capture_output=True, text=True, check=False, timeout=timeout)
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout)


def _dpm_install_package(
    package_root: str | Path, command_prefix: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Install the DPM components declared by one package before DPM commands.

    Best effort: the command resolves the package's SDK release against DPM's
    registry, which the eval container cannot reach. When the components are
    already in the container's DPM cache the following build needs nothing from
    it, so a failure here is reported and the build decides the outcome.
    """
    install = _run((command_prefix or []) + ["dpm", "install", "package"], package_root)
    if install.returncode != 0:
        detail = (install.stderr or install.stdout or "").strip().splitlines()
        print(f"`dpm install package` failed in {package_root}; continuing with the cached components.")
        if detail:
            print(f"  {detail[0][:200]}")
    return install


def build_daml_package(
    package_root: str | Path,
    *,
    command_prefix: list[str] | None = None,
    extra_args: list[str] | None = None,
    build_all: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Build a Daml package using DPM when the package is DPM-configured."""
    command_prefix = command_prefix or []
    extra_args = extra_args or []
    if _package_uses_dpm(package_root):
        _dpm_install_package(package_root, command_prefix)
        return _run(command_prefix + ["dpm", "build"] + extra_args + (["--all"] if build_all else []), package_root)
    return _run(command_prefix + ["daml", "build"] + extra_args + (["--all"] if build_all else []), package_root)


def test_daml_package(
    package_root: str | Path,
    test_file: str | Path,
    *,
    command_prefix: list[str] | None = None,
    extra_args: list[str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a package's Daml Script tests, using DPM when the package is DPM-configured.

    Mirrors build_daml_package: DPM/override-components packages have no `sdk-version`
    field, so the classic `daml test` assistant errors out; they must go through
    `dpm damlc test` instead.
    """
    command_prefix = command_prefix or []
    extra_args = extra_args or []
    if _package_uses_dpm(package_root):
        _dpm_install_package(package_root, command_prefix)
        command = command_prefix + ["dpm", "damlc", "test"] + extra_args + ["--files", str(test_file)]
    else:
        command = command_prefix + ["daml", "test"] + extra_args + ["--files", str(test_file)]
    return _run(command, package_root, timeout=timeout)


def lint_daml_files(
    package_root: str | Path,
    files: list[str],
    *,
    command_prefix: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Lint Daml files using DPM when the package is DPM-configured."""
    command_prefix = command_prefix or []
    if _package_uses_dpm(package_root):
        _dpm_install_package(package_root, command_prefix)
        return _run(command_prefix + ["dpm", "damlc", "lint"] + files, package_root)
    return _run(command_prefix + ["daml", "damlc", "lint"] + files, package_root)
