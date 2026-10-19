import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HOOK_DIR = Path(__file__).resolve().parents[2] / "hooks"

PROMPT_PAYLOAD: dict[str, object] = {
    "session_id": "abc123",
    "prompt_id": "p-1",
    "cwd": "/tmp/x",
    "transcript_path": "/tmp/x.jsonl",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "hi",
}
PRECOMPACT_PAYLOAD: dict[str, object] = {**PROMPT_PAYLOAD, "trigger": "manual"}

HOOKS: dict[str, tuple[Path, str, dict[str, object]]] = {
    "prompt": (HOOK_DIR / "prompt_hook.py", "/hooks/prompt", PROMPT_PAYLOAD),
    "precompact": (HOOK_DIR / "precompact_hook.py", "/hooks/precompact", PRECOMPACT_PAYLOAD),
}


def _handler(received: list[tuple[str, bytes]], behaviour: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.path, body))
            if behaviour == "slow":
                time.sleep(5)
            self.send_response(500 if behaviour == "erroring" else 202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


@contextmanager
def _service(behaviour: str) -> Iterator[tuple[int, list[tuple[str, bytes]]]]:
    """Yield (port, received requests) for a stub service with the given behaviour."""
    received: list[tuple[str, bytes]] = []
    if behaviour == "down":
        # Close the probe before yielding so the port refuses connections instead of hanging.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        yield port, received
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(received, behaviour))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], received
    finally:
        server.shutdown()
        server.server_close()


def _run(
    script: Path, stdin: bytes, port: int, cwd: Path
) -> tuple[subprocess.CompletedProcess[bytes], float]:
    env = {**os.environ, "SMARTROUTER_PORT": str(port)}
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=stdin,
        capture_output=True,
        cwd=cwd,
        env=env,
        timeout=10,
        check=False,
    )
    return proc, time.monotonic() - start


@pytest.mark.parametrize("hook", ["prompt", "precompact"])
@pytest.mark.parametrize("behaviour", ["up", "down", "slow", "erroring"])
def test_hook_exits_silently(hook: str, behaviour: str, tmp_path: Path) -> None:
    script, path, payload = HOOKS[hook]
    body = json.dumps(payload).encode()
    with _service(behaviour) as (port, received):
        proc, elapsed = _run(script, body, port, tmp_path)

    assert proc.returncode == 0
    assert proc.stdout == b""
    assert proc.stderr == b""
    if behaviour == "up":
        assert received == [(path, body)]
    if behaviour == "slow":
        assert elapsed < 3


@pytest.mark.parametrize("hook", ["prompt", "precompact"])
def test_invalid_json_exits_silently(hook: str, tmp_path: Path) -> None:
    script, _, _ = HOOKS[hook]
    with _service("up") as (port, _):
        proc, _ = _run(script, b"{not json", port, tmp_path)

    assert proc.returncode == 0
    assert proc.stdout == b""
    assert proc.stderr == b""
