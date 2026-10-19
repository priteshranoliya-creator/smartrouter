# Side spike: ScaleDown `/classify` as a decision engine

Date: 2026-10-19 · Code: `spikes/scaledown/bench.py` · raw: `spikes/scaledown/out/results.json` (git-ignored)
Same 20 prompts and guessed tiers as T0.3 (`spikes/t03_laya/prompts.jsonl`). 3 labels with rubrics (Trivial / Standard / Hard = Haiku / Sonnet / Opus), 3 calls per prompt, from this machine over the public API.

## Result

| | ScaleDown `/classify` | Laya `english` (CPU) | Always Sonnet |
| --- | --- | --- | --- |
| Tier agreement with my guesses | **16 / 20** | 13–14 / 20 | 11 / 20 |
| Confidence on its answers | mostly 0.6–0.99 (usable as-is) | 0.32–0.59, never passes 0.6 | — |
| Latency p50 / p95 / max | 1181 / 1690 / 2076 ms | 200 / 251 / 457 ms | — |
| Calls over the 400 ms deadline | **60 / 60** | 1 / 60 | — |
| Prompt leaves the machine | yes (ScaleDown servers) | no | — |

Misses: p12, p14, p15 (expected Standard, said Hard: multi-file refactor, library swap, memory leak; arguably defensible, and the error is on the safe side: a stronger model, not a weaker one) and p20, the Hindi prompt (Trivial 0.52 vs Standard 0.46).

## Notes

- The API returns `403 Forbidden` to Python urllib's default `User-Agent`; set any other `User-Agent`. Auth is the `x-api-key` header.
- The response has `top_label`, `scores` (sums to 1), per-label rubric echo, and a one-line `reasoning`. It also returns `input_tokens`, `chunk_count`, `ocr_text`.
- Labels and rubrics are sent per request, so the label set lives in our config, not in their account.

## What it means for the design

- Better and more confident than zero-shot Laya on this small set, but ~3x over the 400 ms deadline on every call. With the TDD as written, every turn would log `decision_timeout` and keep the current model.
- Using it needs one of: a longer deadline (adds ~1.2 s before the first token of every prompt), or a privacy exception, since TDD section 6 says prompts never leave the machine.
- n = 20 with my own labels. The real comparison is the golden test split in M5, run through the same `DecisionEngine` interface.
