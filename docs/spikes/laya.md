# T0.3 spike: Laya load, answers and CPU latency

Date: 2026-10-19 · Machine: Apple M4, macOS 26, Python 3.11.15 · `laya` 0.4.11, torch 2.14.1, transformers 5.20.1
Code: `spikes/t03_laya/bench.py` · prompts: `spikes/t03_laya/prompts.jsonl` (20 coding prompts, my guessed tier per prompt) · raw results: `spikes/t03_laya/out/{cpu,mps}.json` (git-ignored, reproducible).

Questions: `config/questions.yaml`, exactly as TDD 5.2. State passed as `{"request": <prompt truncated to first+last 1,200 chars>}`. 3 warm-up calls, then 3 timed calls per prompt (n = 60 per checkpoint).

## Result: GO on the 400 ms deadline, with two design changes needed (below)

### Latency

| Checkpoint | Device | p50 | p95 | max | > 400 ms | Load (warm cache) |
| --- | --- | --- | --- | --- | --- | --- |
| `english` (ModernBERT-large, 512 tok) | CPU, 4 torch threads | 200 ms | 251 ms | 457 ms | 1 / 60 | 1.9–2.7 s |
| `english` | MPS (Apple GPU) | 117 ms | 151 ms | 173 ms | 0 / 60 | 4.5 s |
| `typed-decisions` (ModernBERT-large, 1024 tok) | CPU | 224 ms | 329 ms | 385 ms | 0 / 60 | 1.5 s |
| `typed-decisions` | MPS | 116 ms | 163 ms | 167 ms | 0 / 60 | 2.4 s |
| `multilingual` (mmBERT-base) | CPU | 78 ms | 86 ms | 89 ms | 0 / 60 | 2.4 s |

A first run on a cold cache was in the same range (english CPU p50 219 / p95 287 / max 404 ms). First-ever load downloads each checkpoint (~650–800 MB) and took 2–3 minutes per checkpoint.

- TDD goal "p50 under 300 ms on CPU" for hook + decision: Laya alone is 200 ms p50 on CPU, leaving ~100 ms for everything else. MPS halves it.
- 400 ms deadline: holds at p95 on CPU; about 1–2% of CPU calls on `english` will time out and keep the current model, as designed.
- Memory: one ModernBERT-large checkpoint resident ≈ 1.7 GB RSS.

### `USE_TF=0`

Confirmed. TensorFlow is not installed in the env, and the script asserts `USE_TF=0` before importing. No load deadlock seen across 6 process starts. Keep setting it anyway (transformers still probes for TF).

### Answers vs my guessed tiers (n = 20, labels are my guesses, so this is a smoke test, not accuracy)

| Checkpoint | Complexity exact match | Tier match (A→haiku, B/C→sonnet, D→opus) | Majority baseline (all sonnet) |
| --- | --- | --- | --- |
| `english` | 12 / 20 | 14 / 20 (argmax choice), 13 / 20 (argmax of summed tier probability) | 11 / 20 |
| `typed-decisions` | 8 / 20 | 13 / 20 (puts 13 of 20 prompts on C) | 11 / 20 |
| `multilingual` | 6 / 20 | 7 / 20 (puts 12 of 20 on A) | 11 / 20 |

- `english` is the only checkpoint clearly above the majority baseline. `multilingual` is fast but worse than always-Sonnet, so it is out despite the speed.
- `task_type` looks sensible on `english` (tests → T 1.00, debugging → D 0.93, planning → P 0.85–1.00).
- `continuation` returned A on every prompt: the state had no session facts, so the question has nothing to compare against. It needs the feature builder's session facts (T3.1) before it means anything.
- The one Hindi prompt (p20) got B on `english`, which matches; the model card warns that non-Latin scripts fail confidently, so one sample proves nothing.

## Finding 1 (changes the design): the confidence field the TDD gates on cannot reach 0.6

Each answer carries two numbers:

| Field | What it is (from the package's own docstrings) | Range seen on `english`, `complexity` |
| --- | --- | --- |
| `confidence` | `1 − H(p)/log k`, normalised entropy. "Useful, but not calibrated: it is not what temperature scaling fits." | 0.03 – 0.26 |
| `answer_confidence` | `max(p)`, the probability of the reported answer. The quantity temperature scaling fits and ECE is computed on; the package's own gate (`laya.confidence`) uses this one first. | 0.32 – 0.59 |

TDD 5.2 says "gate on `confidence`", with `min_confidence` 0.6. On real prompts `confidence` never exceeds 0.26, so every decision would hit `low_confidence` and the router would never switch. Even `answer_confidence` never reached 0.6 on these 20 prompts, because a 4-way question spreads mass (typical: B 0.45, C 0.32).

The router's real decision is over 3 tiers, not 4 choices. Summing probabilities per tier (B + C = Sonnet) gives tier confidence 0.49–0.82; 10 of 20 prompts reach 0.6, all of them Sonnet. No Haiku or Opus answer reached 0.6 zero-shot, so in practice the uncalibrated router would only ever say "Sonnet or stay".

The package also warns at load: *"this checkpoint ships invalid temperatures … using choice:11+=0.1006 -> 0.5. Treat confidence from the affected entries as uncalibrated."* That bucket is for 11+ options; ours have 2–6, so it does not touch our questions, but it confirms the shipped confidences need our own calibration (T5.4).

**Proposed change (needs approval):** the engine returns full probability distributions; the policy gates on **tier probability** = sum of calibrated `p(choice)` over the choices that map to the desired tier. Thresholds stay in `router.yaml` and get re-set after T5.4 calibration. Shadow mode is unaffected: it logs everything either way.

## Finding 2: package facts that differ from the TDD and the model card

- PyPI has `laya` 0.4.11; the model card still talks about 0.3.20. Pin `laya==0.4.11` and the checkpoint revision (`laya.PINNED_REVISIONS` holds the SHA).
- `Router()` defaults to the **`multilingual`** checkpoint, not English. We must pass `model="english"` explicitly (from config).
- A third checkpoint, `typed-decisions`, reads 1024 tokens instead of 512 at the same latency, but routed worse here (fine-tuned for four other workflows). Keep `english`; revisit if the 512-token budget truncates too much.
- The package ships calibration tools: `laya.fit_temperatures`, `fit_one_temperature`, `ece_score`. T5.4 should use them instead of writing our own NLL fit.
- The package ships a `router_questions()` preset (difficulty score 0–3, domain, needs_tools, is_sensitive). It is a different question set from ours; worth one comparison in T5.4, not adopted now.

## Finding 3: keep the decision engine swappable

Laya is the first engine, not the only one. Nothing Laya-specific should leak past `decision/laya_engine.py`. Proposed contract for T3.1:

```python
@dataclass(frozen=True)
class QuestionAnswer:
    choice: str
    probabilities: dict[str, float]   # calibrated if a calibration file is loaded
    raw: dict[str, Any]               # engine-specific extras (entropy confidence, etc.), logged only

@dataclass(frozen=True)
class Decision:
    engine: str                       # "laya:english@<sha>", "fake", later others
    answers: dict[str, QuestionAnswer]
    latency_ms: float

class DecisionEngine(Protocol):
    name: str
    def decide(self, state: DecisionState) -> Decision: ...
```

Tier mapping and the confidence gate live in the policy, driven by `router.yaml`, so a new engine only has to answer the questions in `questions.yaml`. `router.yaml` gets `decision: {engine: laya, checkpoint: english, device: cpu}`; device `mps` is a config switch.

## Go / no-go

- **Go** on the 400 ms deadline on CPU with `english` (p95 ~250 ms). Recommend allowing `device: mps` in config as an option.
- **Go** on `USE_TF=0`.
- **Blocker for `active` mode, not for shadow:** zero-shot confidences are too flat to pass the TDD thresholds. Needs Finding 1's tier-probability gate plus T5.4 calibration before T6.1.
