"""Spike: ScaleDown /classify as a decision engine, on the same 20 prompts as T0.3.

Usage (from repo root, key in .env as SCALEDOWN_API_KEY):
    python3 -I spikes/scaledown/bench.py --repeats 3

Writes spikes/scaledown/out/results.json. Never prints the key.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
URL = "https://api.scaledown.xyz/classify"

LABELS = [
    {"name": "Trivial", "rubric": "The request is a tiny task: rename, formatting, a typo, a one-line fix, "
                                  "or a quick factual lookup about the project."},
    {"name": "Standard", "rubric": "The request needs normal coding work: a change in one or a few files, a short "
                                   "explanation, tests for one function, or debugging where the cause can be found "
                                   "by reading the code."},
    {"name": "Hard", "rubric": "The request needs deep reasoning: architecture or design trade-offs, race conditions "
                               "or concurrency, security review, or a risky migration."},
]
GUESS_TO_LABEL = {"A": "Trivial", "B": "Standard", "C": "Standard", "D": "Hard"}


def load_key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("SCALEDOWN_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("SCALEDOWN_API_KEY not found in .env")


def classify(key: str, text: str) -> tuple[float, dict]:
    body = json.dumps({"text": text, "labels": LABELS}).encode()
    req = urllib.request.Request(URL, data=body, method="POST",
                                 # the API returns 403 to urllib's default User-Agent
                                 headers={"x-api-key": key, "Content-Type": "application/json",
                                          "User-Agent": "laya-router-spike/0.1"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code}: {e.read()[:500]!r}")
    return (time.perf_counter() - t) * 1000, out


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    key = load_key()
    prompts = [json.loads(l) for l in (ROOT / "spikes/t03_laya/prompts.jsonl").read_text().splitlines() if l]

    _, first = classify(key, prompts[0]["prompt"])  # warm-up; also shows the response shape
    print("response keys:", sorted(first.keys()))

    rows, lat, correct = [], [], 0
    for item in prompts:
        times, out = [], {}
        for _ in range(args.repeats):
            ms, out = classify(key, item["prompt"])
            times.append(ms)
        lat.extend(times)
        rows.append({"id": item["id"], "expected": GUESS_TO_LABEL[item["guess"]], "ms": times, "raw": out})
        print(item["id"], GUESS_TO_LABEL[item["guess"]], f"{statistics.median(times):6.0f}ms",
              json.dumps(out)[:220], flush=True)

    report = {"p50_ms": statistics.median(lat), "p95_ms": pct(lat, 0.95), "max_ms": max(lat),
              "over_400ms": sum(m > 400 for m in lat), "n": len(lat), "rows": rows}
    (HERE / "out").mkdir(exist_ok=True)
    (HERE / "out/results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"p50 {report['p50_ms']:.0f}ms  p95 {report['p95_ms']:.0f}ms  max {report['max_ms']:.0f}ms  "
          f">400ms {report['over_400ms']}/{report['n']}")


if __name__ == "__main__":
    main()
