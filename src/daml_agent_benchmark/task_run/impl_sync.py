"""Keeping the host's copy of the implementation files in step with the container's.

The agent writes inside the container, but the dashboard reads the host's copy and the
run record snapshots it, so a background thread hashes the implementation files in the
container and copies back the ones that changed. It polls rather than following the
agent's file-change events, because those can arrive late or stop early, and because the
final copy-back can fail once the container is gone.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from time import time

from daml_agent_benchmark.constants import CONTAINER_DOCKER_BIN


class ImplSyncWatcher:
    """Mirrors the container's implementation files into the copy while a task runs."""

    def __init__(
        self,
        *,
        repo_copy_root: str,
        allowed_impl_rel_paths_sorted: list[str],
        logger,
        is_exiting: Callable[[], bool],
    ) -> None:
        self.repo_copy_root = repo_copy_root
        self.allowed_impl_rel_paths_sorted = allowed_impl_rel_paths_sorted
        self.logger = logger
        # The driver knows when the run is winding down; failures are benign then.
        self.is_exiting = is_exiting
        self.container_id: str | None = None
        self.impl_hashes_by_rel_path: dict[str, str] = {}
        self.impl_sync_poll_seconds = 1.0
        self.impl_sync_stop_event = threading.Event()
        self.impl_sync_thread: threading.Thread | None = None
        self.impl_sync_warning_last_log_at: dict[str, float] = {}
        self.impl_sync_warning_throttle_seconds = 10.0

    @property
    def stopping(self) -> bool:
        return self.impl_sync_stop_event.is_set()

    def _compute_impl_hashes_in_container(self) -> dict[str, str] | None:
        if self.container_id is None:
            raise RuntimeError("Cannot compute impl hashes: container id not set")
        container_paths = [f"/workspace/{rel_path}" for rel_path in self.allowed_impl_rel_paths_sorted]
        cmd = [CONTAINER_DOCKER_BIN, "exec", self.container_id, "sha256sum", *container_paths]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            stderr_text = proc.stderr or ""
            stdout_text = proc.stdout or ""
            self._handle_impl_sync_warning(
                cmd=cmd,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                skip_if_benign=True,
            )
            return None
        out: dict[str, str] = {}
        for raw_line in (proc.stdout or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            digest, path = line.split(maxsplit=1)
            rel_path = path.removeprefix("/workspace/")
            out[rel_path] = digest
        missing = [rel_path for rel_path in self.allowed_impl_rel_paths_sorted if rel_path not in out]
        if missing:
            raise RuntimeError(f"Missing impl hashes for paths: {missing}")
        return out

    def _copy_impl_file_from_container(self, rel_path: str) -> bool:
        if self.container_id is None:
            raise RuntimeError("Cannot copy impl file: container id not set")
        src = f"{self.container_id}:/workspace/{rel_path}"
        dst = Path(self.repo_copy_root) / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp_dst = dst.with_name(f".{dst.name}.syncing")
        cmd = [CONTAINER_DOCKER_BIN, "cp", src, str(tmp_dst)]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            stderr_text = proc.stderr or ""
            stdout_text = proc.stdout or ""
            self._handle_impl_sync_warning(
                cmd=cmd,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                rel_path=rel_path,
                skip_if_benign=True,
            )
            return False
        os.replace(tmp_dst, dst)
        return True

    def copy_all(self) -> None:
        """Copy all impl files from container via docker cp (works on stopped containers).

        Unlike _sync_impl_files_if_changed, this does NOT require docker exec (sha256sum)
        inside the container, so it works even when the container has been stopped.
        """
        if self.container_id is None:
            self.logger.log("[codex] WARNING: ImplSyncWatcher.copy_all called with no container_id")
            return
        if len(self.allowed_impl_rel_paths_sorted) == 0:
            self.logger.log("[codex] WARNING: ImplSyncWatcher.copy_all called with no impl paths")
            return
        copied = []
        for rel_path in self.allowed_impl_rel_paths_sorted:
            try:
                if self._copy_impl_file_from_container(rel_path):
                    copied.append(rel_path)
            except Exception as exc:
                self.logger.log(f"[codex] final copyback failed for {rel_path}: {exc}")
        if copied:
            self.logger.log(f"[codex] final copyback from container: {', '.join(copied)}")

    def sync_if_changed(self, *, force: bool) -> None:
        """Mirror impl files based on real container file changes.

        Bigger picture: this is event-independent, so host impl files stay accurate even
        if app-server event delivery is delayed/truncated, and if final cleanup copyback
        does not complete.
        """
        if self.container_id is None or len(self.allowed_impl_rel_paths_sorted) == 0:
            return
        current_hashes = self._compute_impl_hashes_in_container()
        if current_hashes is None:
            return
        if force:
            rel_paths_to_sync = list(self.allowed_impl_rel_paths_sorted)
        else:
            rel_paths_to_sync = [
                rel_path
                for rel_path in self.allowed_impl_rel_paths_sorted
                if current_hashes[rel_path] != self.impl_hashes_by_rel_path.get(rel_path)
            ]
        if len(rel_paths_to_sync) == 0:
            return
        for rel_path in rel_paths_to_sync:
            copied = self._copy_impl_file_from_container(rel_path)
            if not copied:
                return
        self.impl_hashes_by_rel_path = current_hashes
        self.logger.log(f"[codex] watcher synced impl files: {', '.join(rel_paths_to_sync)}")

    def _impl_sync_loop(self) -> None:
        while not self.impl_sync_stop_event.is_set():
            try:
                self.sync_if_changed(force=False)
            except Exception as exc:
                self._handle_impl_sync_warning(
                    cmd=["impl_sync_loop"],
                    stdout_text="",
                    stderr_text=f"{type(exc).__name__}: {exc}",
                )
            self.impl_sync_stop_event.wait(self.impl_sync_poll_seconds)

    def start(self, container_id: str | None) -> None:
        """Start the thread that mirrors the container's implementation files to the host.

        The driver runs codex through `codex_in_container.py`, which prints the container
        id before it starts the container, so the id is known by the time this runs.
        Without an id, or for a task with no implementation files, there is nothing to
        watch and the final copy-back does the work instead. A second call while the
        thread runs does nothing.
        """
        self.container_id = container_id
        if self.container_id is None or self.impl_sync_thread is not None:
            return
        if len(self.allowed_impl_rel_paths_sorted) == 0:
            return
        self.impl_sync_thread = threading.Thread(target=self._impl_sync_loop, daemon=True)
        self.impl_sync_thread.start()
        self.logger.log("[codex] started impl-file sync watcher")

    def stop(self) -> None:
        """Ask the thread to finish its current poll and end."""
        self.impl_sync_stop_event.set()
        if self.impl_sync_thread is None:
            return
        self.impl_sync_thread.join(timeout=2.0)
        self.impl_sync_thread = None

    def _handle_impl_sync_warning(
        self,
        *,
        cmd: list[str],
        stdout_text: str,
        stderr_text: str,
        rel_path: str | None = None,
        skip_if_benign: bool = False,
    ) -> None:
        if skip_if_benign and self._is_benign_impl_sync_subprocess_failure(
            stdout_text=stdout_text,
            stderr_text=stderr_text,
        ):
            return
        first_error_line = next((line for line in (stderr_text or "").splitlines() if line.strip()), "")
        if not first_error_line:
            first_error_line = next((line for line in (stdout_text or "").splitlines() if line.strip()), "")
        cmd_key = " ".join(cmd[:2]) if len(cmd) >= 2 else " ".join(cmd)
        rel_key = rel_path or "-"
        key = f"{cmd_key}|{rel_key}|{first_error_line[:200]}"
        now = time()
        last_logged_at = self.impl_sync_warning_last_log_at.get(key)
        if last_logged_at is not None and (now - last_logged_at) < self.impl_sync_warning_throttle_seconds:
            return
        self.impl_sync_warning_last_log_at[key] = now
        stderr_tail = "\n".join((stderr_text or "").splitlines()[-5:])
        stdout_tail = "\n".join((stdout_text or "").splitlines()[-5:])
        rel_path_info = f" rel_path={rel_path}" if rel_path is not None else ""
        self.logger.log(
            f"[codex] WARNING: impl-file sync watcher skipped one poll{rel_path_info}.\n"
            f"cmd={' '.join(shlex.quote(part) for part in cmd)}\n"
            f"stdout_tail={stdout_tail}\n"
            f"stderr_tail={stderr_tail}"
        )

    def _is_benign_impl_sync_subprocess_failure(
        self,
        *,
        stdout_text: str,
        stderr_text: str,
    ) -> bool:
        """Return True when watcher subprocess failures should be skipped without warning."""
        if self._is_container_not_running_error(stderr_text):
            return True
        if self._is_ambiguous_container_unavailable_failure(
            stdout_text=stdout_text,
            stderr_text=stderr_text,
        ):
            return True
        return False

    @staticmethod
    def _is_container_not_running_error(stderr_text: str) -> bool:
        lowered = (stderr_text or "").lower()
        return "is not running" in lowered or "no such container" in lowered

    def _is_ambiguous_container_unavailable_failure(
        self,
        *,
        stdout_text: str,
        stderr_text: str,
    ) -> bool:
        """Treat empty-output docker exec/cp failures as benign during shutdown.

        During timeout interrupt/termination windows, Docker can return a non-zero status
        with no diagnostic text while the container is disappearing. In that case, we
        should skip this poll quietly instead of logging noisy false-positive warnings.
        """
        if (stdout_text or "").strip():
            return False
        if (stderr_text or "").strip():
            return False
        return self.is_exiting()
