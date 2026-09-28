"""Finding, starting and sanity-checking the docker daemon on the host."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from time import monotonic, sleep

from daml_agent_benchmark.constants import (
    CONTAINER_AUTO_SWITCH_DESKTOP_CONTEXT,
    CONTAINER_DOCKER_BIN,
    CONTAINER_DOCKER_INFO_TIMEOUT_SECONDS,
    CONTAINER_DOCKER_START_STATUS_INTERVAL_SECONDS,
    CONTAINER_DOCKER_START_TIMEOUT_SECONDS,
    CONTAINER_SANITIZE_DOCKER_ENV,
)


def require_docker() -> None:
    """Fail fast if Docker is not installed, before we try to build/run anything."""
    if not shutil.which(CONTAINER_DOCKER_BIN):
        raise FileNotFoundError(
            f"Docker binary not found: {CONTAINER_DOCKER_BIN!r}. "
            "Install Docker, or set `CONTAINER_DOCKER_BIN` in daml_agent_benchmark/constants.py."
        )


def maybe_sanitize_docker_env() -> None:
    """Clear docker host/TLS override env vars for local Docker Desktop workflows.
    These overrides commonly break `docker info` in IDE-launched processes."""
    if not CONTAINER_SANITIZE_DOCKER_ENV:
        return
    removed: list[str] = []
    for name in ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION"):
        value = os.environ.pop(name, None)
        if value:
            removed.append(name)
    if removed:
        print(
            "[container] cleared docker environment overrides: " + ", ".join(removed),
            flush=True,
        )


def docker_daemon_is_running() -> bool:
    """Check if the Docker daemon is responsive."""
    ok, _ = _docker_daemon_status()
    return ok


def _docker_daemon_status() -> tuple[bool, str]:
    """Return `(is_running, diagnostic)` based on `docker info`."""
    timeout_seconds = CONTAINER_DOCKER_INFO_TIMEOUT_SECONDS
    try:
        proc = subprocess.run(
            [CONTAINER_DOCKER_BIN, "info"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"`docker info` timed out after {timeout_seconds}s"
    diagnostic = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    return proc.returncode == 0, diagnostic


def _is_docker_api_version_mismatch_error(diagnostic: str) -> bool:
    text = (diagnostic or "").lower()
    if not text:
        return False
    return (
        "requested api version" in text
        or "client version" in text
        and "api version" in text
        and "too new" in text
        or "supports the requested api version" in text
    )


def _fail_fast_on_unhealthy_daemon(last_error: str, phase: str) -> None:
    raise RuntimeError(
        f"Docker daemon unhealthy detected {phase}.\n"
        f"Last docker error:\n{last_error}\n"
        "This is a Docker Desktop engine/socket issue (not the evaluator).\n"
        "Try: `docker desktop restart` (or restart Docker Desktop UI), then rerun.\n"
        "Quick checks: `docker version`, `docker ps`."
    )


def _maybe_switch_to_desktop_context() -> bool:
    """On macOS with Docker Desktop, switch to the `desktop-linux` context when available.
    This helps when the current context points to a stale/unreachable daemon."""
    if sys.platform != "darwin":
        return False
    if not CONTAINER_AUTO_SWITCH_DESKTOP_CONTEXT:
        return False

    list_proc = subprocess.run(
        [
            CONTAINER_DOCKER_BIN,
            "context",
            "ls",
            "--format",
            "{{if .Current}}*{{end}}{{.Name}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if list_proc.returncode != 0:
        return False

    lines = [line.strip() for line in (list_proc.stdout or "").splitlines() if line.strip()]
    contexts = [line[1:] if line.startswith("*") else line for line in lines]
    if "desktop-linux" not in contexts:
        return False

    current = ""
    for line in lines:
        if line.startswith("*"):
            current = line[1:]
            break
    if current == "desktop-linux":
        return False

    use_proc = subprocess.run(
        [CONTAINER_DOCKER_BIN, "context", "use", "desktop-linux"],
        capture_output=True,
        text=True,
        check=False,
    )
    if use_proc.returncode == 0:
        print("Switched Docker context to 'desktop-linux'.", flush=True)
        return True
    return False


def _docker_desktop_app_is_running() -> bool:
    """Best-effort check for Docker Desktop app state on macOS."""
    if sys.platform != "darwin":
        return False
    proc = subprocess.run(
        [CONTAINER_DOCKER_BIN, "desktop", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False
    status_text = ((proc.stdout or "").strip() or (proc.stderr or "").strip()).lower()
    return "running" in status_text


def ensure_docker_daemon() -> None:
    """Start Docker Desktop if the daemon isn't running, and wait for it to be ready.
    On macOS this opens Docker.app; on Linux the daemon is typically managed by systemd."""
    running, last_error = _docker_daemon_status()
    if running:
        return
    if _is_docker_api_version_mismatch_error(last_error):
        _fail_fast_on_unhealthy_daemon(last_error, "before startup wait")

    # First try context self-healing (common on macOS when current context is stale).
    _maybe_switch_to_desktop_context()
    running, last_error = _docker_daemon_status()
    if running:
        print("Docker daemon is ready after context check.", flush=True)
        return
    if _is_docker_api_version_mismatch_error(last_error):
        _fail_fast_on_unhealthy_daemon(last_error, "after context check")

    if sys.platform == "darwin":
        if _docker_desktop_app_is_running():
            print(
                "Docker Desktop app is running, but daemon is not ready yet; waiting for engine...",
                flush=True,
            )
        else:
            print(
                "Docker daemon not running, starting Docker Desktop... "
                "(macOS may prompt for privileged access on first launch)",
                flush=True,
            )
            subprocess.run(["open", "-a", "Docker"], check=True)
    else:
        print(
            "Docker daemon not running, starting Docker service via systemd...",
            flush=True,
        )
        # Linux: try systemd
        subprocess.run(["sudo", "systemctl", "start", "docker"], check=False)

    # Wait for daemon to become responsive
    timeout_seconds = CONTAINER_DOCKER_START_TIMEOUT_SECONDS
    status_interval_seconds = CONTAINER_DOCKER_START_STATUS_INTERVAL_SECONDS
    if status_interval_seconds <= 0:
        raise ValueError("CONTAINER_DOCKER_START_STATUS_INTERVAL_SECONDS must be > 0")
    restart_after_seconds = 30  # restart Docker Desktop if daemon is still stuck after this
    deadline = monotonic() + timeout_seconds
    start_time = monotonic()
    next_status_at = start_time
    attempted_context_switch = False
    attempted_restart = False
    while monotonic() < deadline:
        running, last_error = _docker_daemon_status()
        if running:
            print("Docker daemon is ready.", flush=True)
            return
        if _is_docker_api_version_mismatch_error(last_error):
            _fail_fast_on_unhealthy_daemon(last_error, "during startup wait")
        if not attempted_context_switch:
            attempted_context_switch = _maybe_switch_to_desktop_context()
            if attempted_context_switch:
                running, last_error = _docker_daemon_status()
                if running:
                    print("Docker daemon is ready after context switch.", flush=True)
                    return
        # If the app is running but the engine is wedged, restart Docker Desktop.
        elapsed = monotonic() - start_time
        if (
            not attempted_restart
            and elapsed >= restart_after_seconds
            and sys.platform == "darwin"
            and _docker_desktop_app_is_running()
        ):
            attempted_restart = True
            print(
                f"[container] Docker Desktop app is running but daemon unresponsive for "
                f"{int(elapsed)}s; restarting Docker Desktop...",
                flush=True,
            )
            subprocess.run(
                [CONTAINER_DOCKER_BIN, "desktop", "restart"],
                capture_output=True,
                check=False,
                timeout=30,
            )
        now = monotonic()
        if now >= next_status_at:
            elapsed_seconds = int(now - start_time)
            err_summary = ((last_error or "").splitlines() or ["unknown error"])[0]
            print(
                f"[container] waiting for Docker daemon ({elapsed_seconds}s/{timeout_seconds}s). "
                f"Last error: {err_summary}",
                flush=True,
            )
            next_status_at = now + status_interval_seconds
        sleep(2)

    error_tail = "\n".join((last_error or "").splitlines()[-8:]).strip()
    diagnostic = f"\nLast docker error:\n{error_tail}" if error_tail else ""
    raise RuntimeError(
        f"Docker daemon did not start within {timeout_seconds} seconds.{diagnostic}\n"
        "Troubleshooting: run `docker context ls`, `docker context use desktop-linux`, then `docker info`."
    )


def stop_docker_daemon() -> None:
    """Shut down Docker Desktop. The runner calls this at the end of a run when Docker was off before it."""
    if not docker_daemon_is_running():
        return
    print("Stopping Docker Desktop...", flush=True)
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'quit app "Docker"'], check=False)
    else:
        subprocess.run(["sudo", "systemctl", "stop", "docker"], check=False)


def docker_image_exists(docker_bin: str, image: str) -> bool:
    cmd = [docker_bin, "image", "inspect", image]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode == 0
