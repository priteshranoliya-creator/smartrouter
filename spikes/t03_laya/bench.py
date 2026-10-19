"""T0.3 spike: load Laya, run the 3 router questions on 20 prompts, measure latency.

Usage (from repo root):
    USE_TF=0 spikes/t03_laya/.venv/bin/python -I spikes/t03_laya/bench.py \
        --checkpoints english,multilingual,typed-decisions --device cpu --repeats 3

Writes spikes/t03_laya/out/<device>.json with raw answers and timings.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import resource
import statistics
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HEAD_TAIL_CHARS = 1200  # TDD 5.2: first and last 1,200 characters


def truncate(prompt: str) -> str:
    if len(prompt) <= 2 * HEAD_TAIL_CHARS:
        return prompt
    return prompt[:HEAD_TAIL_CHARS] + "\n...\n" + prompt[-HEAD_TAIL_CHARS:]


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def rss_mb() -> float:
    # macOS reports ru_maxrss in bytes, Linux in KiB
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoints", default="english,multilingual,typed-decisions")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=3)
    args = ap.parse_args()

    assert os.environ.get("USE_TF") == "0", "run with USE_TF=0"
    tf_present = importlib.util.find_spec("tensorflow") is not None

    import torch
    import laya
    from laya import Router

    questions = yaml.safe_load((ROOT / "config/questions.yaml").read_text())
    prompts = [json.loads(line) for line in (HERE / "prompts.jsonl").read_text().splitlines() if line]

    report: dict = {
        "env": {
            "python": platform.python_version(),
            "machine": platform.machine(),
            "laya": laya.__version__,
            "torch": torch.__version__,
            "torch_threads": torch.get_num_threads(),
            "device": args.device,
            "USE_TF": os.environ.get("USE_TF"),
            "tensorflow_installed": tf_present,
        },
        "checkpoints": {},
    }

    for name in args.checkpoints.split(","):
        router = Router(device=args.device, max_loaded=1)
        t0 = time.perf_counter()
        router.preload([name])
        load_s = time.perf_counter() - t0

        def ask(p: str) -> tuple[float, dict]:
            t = time.perf_counter()
            out = router.predict({"request": truncate(p)}, questions, model=name)
            return (time.perf_counter() - t) * 1000, out

        for _ in range(args.warmup):
            ask(prompts[0]["prompt"])

        lat: list[float] = []
        rows = []
        for item in prompts:
            times = []
            out: dict = {}
            for _ in range(args.repeats):
                ms, out = ask(item["prompt"])
                times.append(ms)
            lat.extend(times)
            answers = {
                q: {"choice": a.get("choice"), "confidence": a.get("confidence")}
                for q, a in out.get("answers", {}).items()
            }
            rows.append({"id": item["id"], "guess": item["guess"], "ms": times, "answers": answers})

        report["checkpoints"][name] = {
            "load_s": round(load_s, 2),
            "rss_mb_after": round(rss_mb(), 0),
            "p50_ms": round(statistics.median(lat), 1),
            "p95_ms": round(pct(lat, 0.95), 1),
            "max_ms": round(max(lat), 1),
            "n": len(lat),
            "sample_raw": out,  # last raw payload, to document the shape
            "rows": rows,
        }
        c = report["checkpoints"][name]
        print(f"{name:16s} load {c['load_s']:6.1f}s  p50 {c['p50_ms']:7.1f}ms  "
              f"p95 {c['p95_ms']:7.1f}ms  max {c['max_ms']:7.1f}ms  rss {c['rss_mb_after']:.0f}MB",
              flush=True)
        router.unload()

    out_dir = HERE / "out"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{args.device}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print("wrote", path)


if __name__ == "__main__":
    main()
