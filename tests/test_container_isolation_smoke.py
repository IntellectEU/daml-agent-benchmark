"""Production-path isolation smoke test for the container wrapper.

Runs `codex_in_container.py` exactly as the benchmark does: the run's egress proxy
is started by the runner's own setup code, and the wrapper runs against a fixture
image built from the real eval image in which `codex` is replaced by a shell
script. The fake codex probes the isolation from inside the container and writes
its findings to the workspace; the test asserts on those findings plus on the
wrapper's copyback, egress and workspace-change reports.

Never calls OpenAI. Needs Docker and the `daml-agent-eval:latest` image, so it is
skipped unless RUN_CONTAINER_ISOLATION_SMOKE=1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from daml_agent_benchmark.constants import CONTAINER_IMAGE, WRAPPER_PATH
from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.container.egress_proxy import (
    ensure_egress_proxy_image,
    setup_shared_egress_proxy,
    teardown_shared_egress_proxy,
)

WRAPPER = WRAPPER_PATH
FIXTURE_IMAGE = "daml-agent-eval-isolation-smoke:latest"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_CONTAINER_ISOLATION_SMOKE") != "1",
    reason="set RUN_CONTAINER_ISOLATION_SMOKE=1 to run the Docker-backed isolation smoke test",
)

# The fake codex: probe the environment, then behave like an agent that edits its
# implementation file, tampers with the test file, and tries to phone home.
FAKE_CODEX = r"""#!/bin/bash
out=/workspace/.probe.json
key_in_env=no; [ -n "${OPENAI_API_KEY:-}" ] && key_in_env=yes
auth_json=no; [ -e /workspace/.codex_home/auth.json ] && auth_json=yes
daml_ro=no; touch /opt/daml/.probe 2>/dev/null || daml_ro=yes
git_dir=no; [ -e /workspace/.git ] && git_dir=yes
web_search=$(grep -E '^web_search' /workspace/.codex_home/config.toml | tr -d ' "' || true)
blocked_http=$(curl -sS -m 8 -o /dev/null -w '%{http_code}' http://example.com/ 2>/dev/null || echo "rc=$?")
allowed_http=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' https://api.openai.com/v1/models 2>/dev/null || echo "rc=$?")
direct_http=$(curl -sS -m 5 --noproxy '*' -o /dev/null -w '%{http_code}' http://1.1.1.1/ 2>/dev/null || echo "rc=$?")
gateway=$(python3 -c 'import socket,struct
for line in open("/proc/net/route").read().splitlines()[1:]:
    f=line.split()
    if f[1]=="00000000": print(socket.inet_ntoa(struct.pack("<L", int(f[2],16)))); break
else: print("")')
if [ -n "$gateway" ]; then
  gateway_http=$(curl -sS -m 5 --noproxy '*' -o /dev/null -w '%{http_code}' "http://$gateway:3128/" 2>/dev/null || echo "rc=$?")
else
  gateway_http="no-default-route"
fi
echo 'module Impl where' > /workspace/daml/Impl.daml
echo '-- tampered' >> /workspace/daml/Test.daml
mkdir -p /workspace/.daml/dist && echo build > /workspace/.daml/dist/out.dar
echo x > /tmp/scratch
printf '{"key_in_env":"%s","auth_json":"%s","daml_ro":"%s","git_dir":"%s","web_search":"%s","blocked_http":"%s","allowed_http":"%s","direct_http":"%s","gateway_http":"%s","user":"%s"}\n' \
  "$key_in_env" "$auth_json" "$daml_ro" "$git_dir" "$web_search" "$blocked_http" "$allowed_http" "$direct_http" "$gateway_http" "$(id -u)" > "$out"
cat "$out"
"""


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


@pytest.fixture(scope="module")
def fixture_image(tmp_path_factory) -> str:
    build_dir = tmp_path_factory.mktemp("isolation-smoke-image")
    (build_dir / "codex").write_text(FAKE_CODEX, encoding="utf-8")
    (build_dir / "Dockerfile").write_text(
        f"FROM {CONTAINER_IMAGE}\nCOPY codex /usr/local/bin/codex\nRUN chmod 0755 /usr/local/bin/codex\n",
        encoding="utf-8",
    )
    _docker("build", "--platform", "linux/amd64", "-t", FIXTURE_IMAGE, str(build_dir))
    return FIXTURE_IMAGE


@pytest.fixture(scope="module")
def run_proxy() -> dict:
    ensure_egress_proxy_image()
    state = setup_shared_egress_proxy()
    yield state
    teardown_shared_egress_proxy(state)


@pytest.fixture()
def repo_copy(tmp_path) -> Path:
    root = tmp_path / "repo_____smoke"
    (root / "daml").mkdir(parents=True)
    (root / "daml" / "Impl.daml").write_text("", encoding="utf-8")
    (root / "daml" / "Test.daml").write_text("module Test where\n", encoding="utf-8")
    (root / "daml.yaml").write_text("sdk-version: 2.9.0\n", encoding="utf-8")
    (root / ".codex_home").mkdir()
    (root / ".codex_home" / "config.toml").write_text('web_search = "disabled"\n', encoding="utf-8")
    return root


def _run_wrapper(repo_copy: Path, image: str, run_proxy: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "CONTAINER_AGENT_EVAL_IMAGE": image,
            "CONTAINER_AGENT_EVAL_PROJECT_ROOT": str(locations.root),
            "CONTAINER_AGENT_EVAL_EGRESS_PROXY_CONTAINER": run_proxy["proxy_container_id"],
            "CONTAINER_AGENT_EVAL_EGRESS_ACCESS_LOG": run_proxy["access_log_path"],
            "CONTAINER_AGENT_EVAL_COPYBACK_REL_PATHS": "daml/Impl.daml",
            "CONTAINER_AGENT_EVAL_CONTAINER_UID": str(os.getuid()),
            "CONTAINER_AGENT_EVAL_CONTAINER_GID": str(os.getgid()),
            "OPENAI_API_KEY": "sk-smoke-test-not-a-real-key-0000000000",
            "CODEX_API_KEY": "sk-smoke-test-not-a-real-key-0000000000",
            "CODEX_HOME": ".codex_home",
        }
    )
    return subprocess.run(
        [sys.executable, str(WRAPPER), "-C", str(repo_copy), "exec", "--json", "probe"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=600,
    )


def _audit_lines(stderr: str, tag: str) -> list[dict]:
    out = []
    for line in stderr.splitlines():
        if line.startswith(f"[{tag}] "):
            out.append(json.loads(line[len(tag) + 3 :]))
    return out


def test_wrapper_isolation_end_to_end(fixture_image: str, repo_copy: Path, run_proxy: dict) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    proc = _run_wrapper(repo_copy, fixture_image, run_proxy)
    assert proc.returncode == 0, proc.stderr[-4000:]

    probe_lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert probe_lines, f"fake codex produced no probe output\n{proc.stderr[-4000:]}"
    probe = json.loads(probe_lines[-1])
    diagnostics = f"probe={probe}\n--- wrapper stderr tail ---\n{proc.stderr[-6000:]}"
    assert probe["auth_json"] == "no", "API key file must not be in the agent's codex home"
    assert probe["key_in_env"] == "yes", "codex itself still needs the key in its process environment"
    assert probe["daml_ro"] == "yes", "/opt/daml (SDK store) must be read-only"
    assert probe["git_dir"] == "no"
    assert probe["web_search"] == "web_search=disabled"
    assert probe["user"] == str(os.getuid())
    # Through the proxy: the allowlisted host answers (401 without credentials), the
    # rest is refused by squid (403). Bypassing the proxy there is no route at all.
    assert probe["allowed_http"] in {"200", "401"}, (
        f"allowlisted OpenAI host must be reachable via the proxy\n{diagnostics}"
    )
    assert probe["blocked_http"] == "403", f"non-allowlisted domain must be denied by the proxy\n{diagnostics}"
    assert probe["direct_http"].startswith("000"), (
        f"the --internal network must have no direct route out\n{diagnostics}"
    )
    # The bridge gateway is the Docker host; it must not answer either (isolated gateway mode).
    assert probe["gateway_http"].startswith("000") or probe["gateway_http"] == "no-default-route", (
        f"the Docker host must not be reachable from the task network\n{diagnostics}"
    )

    # Copyback is impl-only: the tampered test file must not reach the copy on the host.
    assert (repo_copy / "daml" / "Impl.daml").read_text(encoding="utf-8") == "module Impl where\n"
    assert (repo_copy / "daml" / "Test.daml").read_text(encoding="utf-8") == "module Test where\n"
    assert not (repo_copy / ".probe.json").exists()

    # The wrapper reported the tamper and the egress attempts.
    changes = {c["path"]: c["kind"] for c in _audit_lines(proc.stderr, "workspace-change")}
    assert changes["daml/Impl.daml"] == "modified"
    assert changes["daml/Test.daml"] == "modified"
    assert changes[".probe.json"] == "added"
    assert not any(p.startswith(".daml/") for p in changes), "build output dirs are not reported"
    assert "[workspace-audit] complete" in proc.stderr

    events = _audit_lines(proc.stderr, "egress-event")
    blocked = {e["domain"] for e in events if e["blocked"]}
    served = {e["domain"] for e in events if not e["blocked"]}
    assert "example.com" in blocked
    assert served <= {"api.openai.com"}, served

    # The per-task network and proxy attachment were torn down.
    networks = _docker("network", "ls", "--format", "{{.Name}}").stdout
    assert "daml-agent-task-" not in networks
    assert "sk-smoke-test" not in proc.stderr, "key value must be redacted from wrapper logs"
