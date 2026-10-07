"""Building the task image and checking that it can run the tasks selected."""

from __future__ import annotations

import shlex
import stat
import subprocess
from pathlib import Path

from daml_agent_benchmark.constants import (
    CONTAINER_CLEANUP_STALE_AFTER_SECONDS,
    CONTAINER_DOCKER_BIN,
    CONTAINER_DOCKERFILE,
    CONTAINER_EGRESS_PROXY_IMAGE,
    CONTAINER_IMAGE,
    CONTAINER_NETWORK_MODE,
    CONTAINER_VERIFY_COMMANDS,
    PACKAGE_DIR,
    WRAPPER_PATH,
)
from daml_agent_benchmark.container.daemon import ensure_docker_daemon, require_docker, sanitize_docker_env
from daml_agent_benchmark.container.egress_proxy import ensure_egress_proxy_image
from daml_agent_benchmark.docker_cleanup import cleanup_stale_daml_docker_resources
from daml_agent_benchmark.repos.registry import handler_for, sdk_store_env
from daml_agent_benchmark.sdk_store import assert_store_archives_free_of_answers, ensure_sdk_store
from daml_agent_benchmark.tasklist_catalog import repo_name_for_path, task_sdk_version


def _resolve_container_dockerfile() -> Path:
    """Resolve the Dockerfile path (which may be relative to the package) to an absolute path."""
    dockerfile = Path(CONTAINER_DOCKERFILE).expanduser()
    if not dockerfile.is_absolute():
        dockerfile = (PACKAGE_DIR / dockerfile).resolve()
    if not dockerfile.exists():
        raise FileNotFoundError(f"Container Dockerfile not found: {dockerfile}")
    return dockerfile


def _container_image_exists() -> bool:
    cmd = [
        CONTAINER_DOCKER_BIN,
        "image",
        "inspect",
        CONTAINER_IMAGE,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode == 0


def _build_container_image() -> None:
    """Build the Docker image. Daml SDKs are not part of it — they live in
    the host-side SDK store, which containers mount (see sdk_store.py)."""
    dockerfile = _resolve_container_dockerfile()
    cmd = [
        CONTAINER_DOCKER_BIN,
        "build",
        "--platform",
        "linux/amd64",
        "-f",
        str(dockerfile),
        "-t",
        CONTAINER_IMAGE,
        str(PACKAGE_DIR / "docker"),
    ]
    print(f"Building container image: {CONTAINER_IMAGE} ...", flush=True)
    proc = subprocess.run(cmd, text=True)
    if proc.returncode != 0:
        # Only show build output when something went wrong
        print("Docker build FAILED. Output:", flush=True)
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr)
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    print("Image built successfully.", flush=True)


def _verify_container_commands() -> None:
    """Smoke-test the built image by checking that required binaries (codex, daml, direnv)
    are actually on PATH inside the container. Catches image build issues early."""
    commands = [c.strip() for c in CONTAINER_VERIFY_COMMANDS if c.strip()]
    if not commands:
        return
    checks = " && ".join(f"command -v {shlex.quote(cmd)} >/dev/null" for cmd in commands)
    probe_script = f"set -euo pipefail; {checks}"
    from daml_agent_benchmark.sdk_store import sdk_store_mount_args

    # daml/dpm come from the SDK store mount, so the probe needs the mounts too.
    cmd = [
        CONTAINER_DOCKER_BIN,
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        *sdk_store_mount_args(read_only=True),
        "--network",
        CONTAINER_NETWORK_MODE,
        CONTAINER_IMAGE,
        "bash",
        "-lc",
        probe_script,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return
    stderr_tail = "\n".join((proc.stderr or "").splitlines()[-40:])
    raise RuntimeError(
        f"Container image is missing required commands ({', '.join(commands)}). Probe failed with:\n{stderr_tail}"
    )


def _collect_required_daml_versions(tasks: dict[str, list[str]]) -> list[str]:
    """The SDK versions that the selected tasks need, for the SDK store to install.

    A repository whose own bootstrap script installs its SDK is left out.
    """
    versions: list[str] = []
    seen: set[str] = set()
    for test_file_path in tasks.keys():
        if handler_for(repo_name_for_path(test_file_path, stripped=True)).sdk_installed_by_repo_script:
            continue
        try:
            version = str(task_sdk_version(test_file_path)).strip()
        except Exception as exc:
            print(f"[container] warning: failed to detect SDK version for {test_file_path}: {exc}", flush=True)
            continue
        if not version or version in seen:
            continue
        versions.append(version)
        seen.add(version)
    return versions


def container_has_command(command_name: str) -> bool:
    """Check if a command is available inside the container image (used for nix-shell preflight)."""
    cmd = [
        CONTAINER_DOCKER_BIN,
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--network",
        CONTAINER_NETWORK_MODE,
        CONTAINER_IMAGE,
        "bash",
        "-lc",
        f"command -v {shlex.quote(command_name)} >/dev/null",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode == 0


def ensure_container_ready(tasks: dict[str, list[str]], answer_files: list[str]) -> None:
    """Top-level preflight: make sure the Docker image exists, has all required DAML SDKs
    baked in, and passes the command smoke test. Rebuilds the image automatically if needed.
    Also checks that the SDK store every container mounts holds no copy of a task's answer,
    `answer_files`: what the agents write, which depends on the run's task kind."""
    _ensure_wrapper_executable(WRAPPER_PATH)
    sanitize_docker_env()
    require_docker()
    ensure_docker_daemon()
    cleanup_stale_daml_docker_resources(
        docker_bin=CONTAINER_DOCKER_BIN,
        container_image=CONTAINER_IMAGE,
        egress_proxy_image=CONTAINER_EGRESS_PROXY_IMAGE,
        stale_after_seconds=int(CONTAINER_CLEANUP_STALE_AFTER_SECONDS),
    )
    sdk_versions = _collect_required_daml_versions(tasks)

    # Build from scratch if image doesn't exist yet. SDKs are NOT part of the image:
    # they live in the host-side SDK store (mounted into containers), so the image
    # build is small and SDK changes never require a rebuild.
    if not _container_image_exists():
        _build_container_image()
    # Make sure the SDK store has every SDK version the selected tasks need
    # (downloads only what is missing).
    ensure_sdk_store(
        sdk_versions,
        image=CONTAINER_IMAGE,
        env=sdk_store_env(),
        answer_files=answer_files,
        docker_bin=CONTAINER_DOCKER_BIN,
    )
    # The store is mounted read-only into every agent container, outside the copy
    # the per-task scan covers. A task whose answer ships inside an SDK (the tutorial
    # tasks did, as `daml new` examples; the canton tasks do, inside canton's jars)
    # must fail the run here, not be found by an agent.
    assert_store_archives_free_of_answers(answer_files)

    _verify_container_commands()
    ensure_egress_proxy_image()


def container_image_id(image: str) -> str:
    """Resolve the image digest once per run; it is part of the ground-truth control cache key."""
    cached = _CONTAINER_IMAGE_ID_CACHE.get(image)
    if cached is not None:
        return cached
    proc = subprocess.run(
        [CONTAINER_DOCKER_BIN, "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
        text=True,
        check=False,
    )
    image_id = (proc.stdout or "").strip() if proc.returncode == 0 else "unknown"
    _CONTAINER_IMAGE_ID_CACHE[image] = image_id
    return image_id


def _ensure_wrapper_executable(path: Path) -> None:
    """Make sure the container codex wrapper script exists and is executable."""
    if not path.exists():
        raise FileNotFoundError(f"Container codex wrapper not found: {path}")
    mode = path.stat().st_mode
    if mode & stat.S_IXUSR:
        return
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


_CONTAINER_IMAGE_ID_CACHE: dict[str, str] = {}
