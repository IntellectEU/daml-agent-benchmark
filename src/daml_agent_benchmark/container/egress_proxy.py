"""The egress proxy every task container talks through, shared by one run."""

from __future__ import annotations

from daml_agent_benchmark.constants import EGRESS_ALLOWED_SUFFIXES

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import time

from daml_agent_benchmark.constants import (
    CONTAINER_DOCKER_BIN,
    CONTAINER_EGRESS_PROXY_AUTO_BUILD_IMAGE,
    CONTAINER_EGRESS_PROXY_DOCKERFILE,
    CONTAINER_EGRESS_PROXY_IMAGE,
    CONTAINER_EGRESS_PROXY_PORT,
    PACKAGE_DIR,
)
from daml_agent_benchmark.container.daemon import docker_image_exists
from daml_agent_benchmark.docker_cleanup import RUNNER_DOCKER_LABEL, RUNNER_DOCKER_LABEL_VALUE
from daml_agent_benchmark.egress_audit import parse_squid_access_log_text, squid_config_text


def _resolve_egress_proxy_dockerfile() -> Path:
    """Resolve the egress proxy Dockerfile path to an absolute path."""
    dockerfile = Path(CONTAINER_EGRESS_PROXY_DOCKERFILE).expanduser()
    if not dockerfile.is_absolute():
        dockerfile = (PACKAGE_DIR / dockerfile).resolve()
    if not dockerfile.exists():
        raise FileNotFoundError(f"Egress proxy Dockerfile not found: {dockerfile}")
    return dockerfile


def setup_shared_egress_proxy(
    allowed_suffixes: list[str] = EGRESS_ALLOWED_SUFFIXES, allowed_hosts: list[str] | None = None
) -> dict:
    """Start the run's single squid egress proxy.

    It allows `allowed_suffixes` together with their subdomains, and `allowed_hosts` by
    exact name only.

    One proxy serves every task of the run; the container wrapper attaches it to
    each task's private `--internal` network, so tasks share the allowlist and the
    access log but cannot see each other. The wrapper attributes log lines to its
    task by client subnet, and the orchestrator reads the whole log at teardown
    for the run-level view.
    """
    suffix = f"{int(time())}-{os.getpid()}"
    proxy_container_name = f"daml-agent-egress-{suffix}"
    state_dir = tempfile.mkdtemp(prefix=f"daml-egress-{suffix}-")
    conf_path = os.path.join(state_dir, "squid.conf")
    access_log_path = os.path.join(state_dir, "access.log")
    Path(access_log_path).touch()

    Path(conf_path).write_text(
        squid_config_text(str(CONTAINER_EGRESS_PROXY_PORT), allowed_suffixes, allowed_hosts or []), encoding="utf-8"
    )

    docker = CONTAINER_DOCKER_BIN
    # The proxy sits on the default bridge for external connectivity; task networks are attached per task.
    proxy_id = subprocess.run(
        [
            docker,
            "run",
            "-d",
            "--platform",
            "linux/amd64",
            "--name",
            proxy_container_name,
            "--label",
            f"{RUNNER_DOCKER_LABEL}={RUNNER_DOCKER_LABEL_VALUE}",
            "--network",
            "bridge",
            "--mount",
            f"type=bind,src={conf_path},dst=/etc/squid/squid.conf,ro",
            "--mount",
            f"type=bind,src={state_dir},dst=/var/log/squid",
            CONTAINER_EGRESS_PROXY_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    print(f"[egress] shared proxy started: proxy={proxy_container_name}, allowed={','.join([*allowed_suffixes, *(allowed_hosts or [])])}")
    return {
        "proxy_container_id": proxy_id,
        "proxy_container_name": proxy_container_name,
        "state_dir": state_dir,
        "access_log_path": access_log_path,
    }


def teardown_shared_egress_proxy(state: dict) -> list[dict]:
    """Remove the shared proxy and return every egress event it logged during the run."""
    docker = CONTAINER_DOCKER_BIN
    events: list[dict] = []

    try:
        events = parse_squid_access_log_text(
            Path(state["access_log_path"]).read_text(encoding="utf-8", errors="replace")
        )
    except OSError as exc:
        print(f"proxy access log could not be read; run-level egress summary is empty: {exc}", flush=True)

    subprocess.run([docker, "rm", "-f", state["proxy_container_name"]], capture_output=True)
    shutil.rmtree(state["state_dir"], ignore_errors=True)

    return events


def ensure_egress_proxy_image() -> None:
    """Build the egress proxy image if it is missing."""
    image = CONTAINER_EGRESS_PROXY_IMAGE
    if docker_image_exists(CONTAINER_DOCKER_BIN, image):
        return
    if not CONTAINER_EGRESS_PROXY_AUTO_BUILD_IMAGE:
        raise RuntimeError(
            f"Egress proxy image not found: {image}. "
            "Enable `CONTAINER_EGRESS_PROXY_AUTO_BUILD_IMAGE=True` or build it manually."
        )
    dockerfile = _resolve_egress_proxy_dockerfile()
    cmd = [
        CONTAINER_DOCKER_BIN,
        "build",
        "--platform",
        "linux/amd64",
        "-f",
        str(dockerfile),
        "-t",
        image,
        str(PACKAGE_DIR / "docker"),
    ]
    print(f"Building egress proxy image: {image} ...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print("Egress proxy Docker build FAILED. Output:", flush=True)
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr)
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    print("Egress proxy image built successfully.", flush=True)


