"""POST a Claude Code hook payload to the local smartrouter service.

Hooks print nothing and always exit 0. UserPromptSubmit stdout is injected into the
conversation, which would change the prompt and break the prompt cache.
"""

import contextlib
import os
import sys
import urllib.request


def post(path: str) -> None:
    """Forward stdin to smartrouter at `path`. Never raises, never writes output."""
    with contextlib.suppress(Exception):
        body = sys.stdin.buffer.read()
        port = os.environ.get("SMARTROUTER_PORT") or "8787"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Bypass proxy env vars so the loopback call never leaves the machine.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=2):
            pass
