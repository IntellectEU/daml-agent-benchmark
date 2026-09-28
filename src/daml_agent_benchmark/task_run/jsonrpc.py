"""Talking JSON-RPC to a process over its stdin and stdout.

The transport only moves messages: it spawns the process, frames requests and reads what
comes back. What the messages mean is the driver's business.
"""

from __future__ import annotations

import selectors
from typing import TextIO, cast

import json
import subprocess


class JsonRpcStdioClient:
    """Thin JSON-RPC-over-stdio transport used by Codex `app-server` mode.

    Why JSON-RPC here:
    - `codex app-server --listen stdio://` speaks JSON-RPC 2.0 over stdin/stdout.
    - The evaluator acts as the JSON-RPC client:
      - sends requests (with `id`) such as `initialize`, `thread/start`, `turn/start`
      - sends notifications (without `id`) such as `initialized`
      - receives:
        - responses for request ids
        - server notifications/events (turn/item/token usage updates)

    This class intentionally stays transport-only:
    - process lifetime for the child app-server
    - request-id assignment + JSON serialization
    - non-blocking line reads from stdout/stderr via `selectors`

    Higher-level protocol/state logic (FSM, timeout policy, event canonicalization)
    is handled by `AppServerRunner`.
    """

    def __init__(self, *, cmd: list[str], env: dict, cwd: str):
        """Start the app-server subprocess and wire stdio for JSON-RPC exchange."""
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            cwd=cwd,
        )
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        self.stdin = self.proc.stdin
        self.stdout = self.proc.stdout
        self.stderr = self.proc.stderr
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.stdout, selectors.EVENT_READ, data="stdout")
        self.selector.register(self.stderr, selectors.EVENT_READ, data="stderr")
        self.next_request_id = 1

    def poll(self) -> int | None:
        """Return subprocess exit code if exited, else None."""
        return self.proc.poll()

    def send_json(self, payload: dict) -> None:
        """Write one JSON message line to app-server stdin."""
        self.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.stdin.flush()

    def send_request(self, method: str, params: dict) -> int:
        """Send a JSON-RPC request and return the allocated request id."""
        req_id = self.next_request_id
        self.next_request_id += 1
        self.send_json({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return req_id

    def send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no `id`, no response expected)."""
        self.send_json({"jsonrpc": "2.0", "method": method, "params": params})

    def read_ready_lines(self, timeout: float) -> list[tuple[str, str]]:
        """Read currently-available lines from stdout/stderr.

        Returns list of `(stream_name, line)` where stream_name is `"stdout"` or
        `"stderr"`. Uses `selectors` so caller can run a single event loop.
        """
        out: list[tuple[str, str]] = []
        for key, _ in self.selector.select(timeout=timeout):
            stream_obj = cast(TextIO, key.fileobj)
            line = stream_obj.readline()
            if not line:
                continue
            stream = str(key.data or "")
            out.append((stream, line))
        return out

    def read_remaining_stdout_lines(self) -> list[str]:
        """Drain any remaining buffered stdout text after process exit."""
        remaining = self.stdout.read()
        if not remaining:
            return []
        return remaining.splitlines()

    def read_remaining_stderr_lines(self) -> list[str]:
        """Drain any remaining buffered stderr text after process exit."""
        remaining = self.stderr.read()
        if not remaining:
            return []
        return remaining.splitlines()

    def terminate(self) -> None:
        """Request graceful subprocess termination (SIGTERM)."""
        self.proc.terminate()

    def kill(self) -> None:
        """Forcefully stop subprocess (SIGKILL)."""
        self.proc.kill()

    def wait(self) -> int:
        """Wait for subprocess exit and return exit code."""
        return self.proc.wait()

    def close(self) -> None:
        """Release selector resources owned by this transport wrapper."""
        self.selector.close()
