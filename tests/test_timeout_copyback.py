"""Test that impl files are copied back from the container even when the task times out.

The pre-commit bug: on timeout, _terminate_then_kill sent SIGKILL to the wrapper after
0.5s, and _shutdown_process returned early because the process was already dead. The
wrapper's cleanup (docker stop + docker cp) never completed, so files were lost.

The fix: after the wrapper is killed on timeout, the runner calls docker cp directly
(via ImplSyncWatcher.copy_all) since docker cp works on stopped containers.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from daml_agent_benchmark.config import DEFAULTS
from daml_agent_benchmark.task_run.app_server import AppServerRunner


def test_post_timeout_copyback():
    """After a timeout, the runner should copy impl files from the container directly."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_copy_dir = Path(temp_dir) / "repo_copy"
        repo_copy_dir.mkdir()

        mock_wrapper = Path(temp_dir) / "codex_in_container.py"

        # Mock wrapper: prints container ID on stderr (so runner captures it),
        # then blocks forever (simulating a hung agent). Does NOT do any copyback
        # itself — the point is that the runner should do it after killing the wrapper.
        mock_wrapper.write_text(
            """#!/bin/sh
trap 'exit 143' TERM
echo 'CONTAINER_ID=abcdef1234567890' >&2
echo 'container.start begin' >&2
while true; do
    sleep 0.1
done
"""
        )
        mock_wrapper.chmod(0o755)

        with patch("daml_agent_benchmark.task_run.app_server.CODEX_TIMEOUT_INTERRUPT_GRACE_SECONDS", 1.0):
            runner = AppServerRunner(
                config=DEFAULTS,
                codex_bin=str(mock_wrapper),
                codex_env={"OPENAI_API_KEY": "sk-test"},
                repo_copy_root=str(repo_copy_dir),
                prompt="dummy",
                timeout_seconds=2,
                copyback_rel_paths=["impl.daml"],
                protected_rel_paths=["daml/Test.daml"],
                log_prefix="test",
                task_log_path=Path(temp_dir) / "task.log",
                live_stdout_events_path=Path(temp_dir) / "events.jsonl",
            )

        copy_calls = []

        def mock_copy(rel_path):
            """Simulate a successful docker cp by creating the file."""
            dst = Path(runner.repo_copy_root) / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("copied from container")
            copy_calls.append(rel_path)
            return True

        with patch.object(runner.impl_sync, "_copy_impl_file_from_container", side_effect=mock_copy):
            runner.run()

        assert runner.timing.timed_out, "Expected the task to have timed out"
        assert runner.container_id == "abcdef1234567890", (
            f"Expected container_id to be captured, got {runner.container_id!r}"
        )
        assert "impl.daml" in copy_calls, (
            f"Expected the watcher to copy back the impl.daml after timeout, "
            f"but calls were: {copy_calls}"
        )
        copied_file = repo_copy_dir / "impl.daml"
        assert copied_file.exists(), "impl.daml should exist in the copy after post-timeout copyback"
        assert copied_file.read_text() == "copied from container"


if __name__ == "__main__":
    test_post_timeout_copyback()
    print("Test passed!")
