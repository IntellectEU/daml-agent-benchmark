"""Test that the AppServerRunner handles recoverable errors during background file syncing.

The AppServerRunner runs a background process (`mysync`) to sync implementation files.
These tests verify that if the `docker cp` command (which underlies the sync watcher)
fails sporadically (e.g. due to Docker daemon latency or temporary unavailability), the
runner's `_run_impl_sync_watcher` logic does not crash the entire evaluation task.
Instead, it should log the error and continue attempting to sync files.
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from daml_agent_benchmark.config import DEFAULTS
from daml_agent_benchmark.task_run.app_server import AppServerRunner


def _make_runner(temp_dir: str, *, copyback_rel_paths: list[str]) -> AppServerRunner:
    temp_path = Path(temp_dir)
    repo_copy_dir = temp_path / "repo_copy"
    repo_copy_dir.mkdir()

    mock_wrapper = temp_path / "codex_in_container.py"
    mock_wrapper.write_text("#!/usr/bin/env python3\n")
    mock_wrapper.chmod(0o755)

    with patch("daml_agent_benchmark.task_run.app_server.CODEX_TIMEOUT_INTERRUPT_GRACE_SECONDS", 1.0):
        runner = AppServerRunner(
            config=DEFAULTS,
            codex_bin=str(mock_wrapper),
            codex_env={"OPENAI_API_KEY": "sk-test"},
            repo_copy_root=str(repo_copy_dir),
            prompt="dummy",
            timeout_seconds=2,
            copyback_rel_paths=copyback_rel_paths,
            protected_rel_paths=["daml/Test.daml"],
            log_prefix="test",
            task_log_path=temp_path / "task.log",
            live_stdout_events_path=temp_path / "events.jsonl",
        )
    runner.impl_sync.container_id = "abc123"
    return runner


def test_compute_impl_hashes_missing_file_is_non_fatal() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        runner = _make_runner(
            temp_dir,
            copyback_rel_paths=["package/example-impl/daml/Example/Trade.daml"],
        )
        with patch(
            "daml_agent_benchmark.task_run.impl_sync.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["docker", "exec"],
                returncode=1,
                stdout="",
                stderr=(
                    "sha256sum: /workspace/package/example-impl/daml/Example/Trade.daml: "
                    "No such file or directory\n"
                ),
            ),
        ):
            assert runner.impl_sync._compute_impl_hashes_in_container() is None


def test_compute_impl_hashes_docker_daemon_outage_is_non_fatal() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        runner = _make_runner(temp_dir, copyback_rel_paths=["impl.daml"])
        with patch(
            "daml_agent_benchmark.task_run.impl_sync.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["docker", "exec"],
                returncode=1,
                stdout="",
                stderr=(
                    "Cannot connect to the Docker daemon at "
                    "unix:///var/run/docker.sock. Is the docker daemon running?\n"
                ),
            ),
        ):
            assert runner.impl_sync._compute_impl_hashes_in_container() is None


def test_copy_impl_file_missing_file_is_non_fatal() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        runner = _make_runner(temp_dir, copyback_rel_paths=["impl.daml"])
        with patch(
            "daml_agent_benchmark.task_run.impl_sync.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["docker", "cp"],
                returncode=1,
                stdout="",
                stderr="Error response from daemon: Could not find the file /workspace/impl.daml in container\n",
            ),
        ):
            assert not runner.impl_sync._copy_impl_file_from_container("impl.daml")


def test_impl_sync_loop_continues_after_sync_exception() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        runner = _make_runner(temp_dir, copyback_rel_paths=["impl.daml"])
        runner.impl_sync.impl_sync_poll_seconds = 0.0
        call_count = 0

        def fake_sync(*, force: bool) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("sync exploded")
            runner.impl_sync.impl_sync_stop_event.set()

        with patch.object(runner.impl_sync, "sync_if_changed", side_effect=fake_sync):
            runner.impl_sync._impl_sync_loop()

        assert call_count == 2


def test_benign_impl_sync_failure_classifier() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        runner = _make_runner(temp_dir, copyback_rel_paths=["impl.daml"])

        assert runner.impl_sync._is_benign_impl_sync_subprocess_failure(
            stdout_text="",
            stderr_text="Error response from daemon: Container abc123 is not running",
        )

        runner.impl_sync.impl_sync_stop_event.set()
        assert runner.impl_sync._is_benign_impl_sync_subprocess_failure(
            stdout_text="",
            stderr_text="",
        )

        assert not runner.impl_sync._is_benign_impl_sync_subprocess_failure(
            stdout_text="",
            stderr_text="sha256sum: /workspace/impl.daml: No such file or directory",
        )
