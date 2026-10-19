"""Spike hook: POST the hook JSON to the bare proxy. Stdlib only, always exit 0, no stdout."""

import json
import sys
import urllib.request

event = sys.argv[1] if len(sys.argv) > 1 else "prompt"
try:
    payload = json.load(sys.stdin)
    req = urllib.request.Request(
        f"http://127.0.0.1:8788/hooks/{event}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2)
except Exception:
    pass
sys.exit(0)
