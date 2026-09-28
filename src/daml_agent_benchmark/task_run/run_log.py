"""The per-task log a run writes while the agent works.

Everything the driver prints goes through here, so a task's output lands in one file that
the dashboard can tail and the run record can point at.
"""

from __future__ import annotations

import json
import threading
from daml_agent_benchmark.task_run.events import redact_secret_values
from pathlib import Path


class TaskRunLogger:
    def __init__(
        self,
        *,
        log_prefix: str,
        task_log_path: Path | None,
        live_stdout_events_path: Path | None,
        secrets: list[str],
    ):
        self.log_prefix = log_prefix
        self.secrets = secrets
        self.lock = threading.Lock()
        self.io_closed = False
        self.task_log_file = None
        self.live_stdout_events_file = None
        if task_log_path:
            task_log_path.parent.mkdir(parents=True, exist_ok=True)
            self.task_log_file = open(task_log_path, "a", encoding="utf-8")
        if live_stdout_events_path:
            live_stdout_events_path.parent.mkdir(parents=True, exist_ok=True)
            self.live_stdout_events_file = open(live_stdout_events_path, "a", encoding="utf-8")

    def _safe_file_write(self, file_obj, text: str) -> None:
        if file_obj is None:
            return
        with self.lock:
            if self.io_closed:
                return
            try:
                file_obj.write(text)
                file_obj.flush()
            except ValueError:
                return

    def log(self, line: str) -> None:
        line = redact_secret_values(line, self.secrets)
        print(f"{self.log_prefix}{line}", flush=True)
        if self.task_log_file is not None:
            self._safe_file_write(self.task_log_file, f"{line}\n")

    def write_live_event(self, event_record: dict) -> None:
        if self.live_stdout_events_file is None:
            return
        self._safe_file_write(
            self.live_stdout_events_file,
            redact_secret_values(json.dumps(event_record, ensure_ascii=False), self.secrets) + "\n",
        )

    def close(self) -> None:
        with self.lock:
            self.io_closed = True
            if self.task_log_file is not None:
                try:
                    self.task_log_file.close()
                except ValueError:
                    pass
            if self.live_stdout_events_file is not None:
                try:
                    self.live_stdout_events_file.close()
                except ValueError:
                    pass
