# smartrouter

A local router for Claude Code that picks the model (Haiku, Sonnet or Opus) for every prompt, weighing a decision model's answer against the cost of losing the prompt cache, and logs every decision, token and dollar to a local dashboard.

**Status:** proof of concept, running in shadow mode (decides and logs, changes nothing). M0–M5 done.

![smartrouter dashboard](docs/img/dashboard.png)

- Design changes from the original technical design: [`docs/tdd-changes.md`](docs/tdd-changes.md)
- Spike findings: [`docs/spikes/`](docs/spikes/)

Runs on `127.0.0.1` only. Decision engines are pluggable (ScaleDown `/classify` by default, Laya as an alternative).

## Develop

Needs [uv](https://docs.astral.sh/uv/) and Python 3.11.

```bash
uv sync                 # install
cp .env.example .env    # then fill in SCALEDOWN_API_KEY / GEMINI_API_KEY
make check              # ruff, mypy --strict, unit tests
uv run uvicorn smartrouter.app:create_app --factory --host 127.0.0.1 --port 8787
```

Configuration lives in `config/` (`router.yaml`, `questions.yaml`, `pricing.yaml`). The SQLite database is created at `data/router.db` on startup. The dashboard is at http://127.0.0.1:8787/.

Opt a project in with `uv run router install --project PATH` (and `uninstall` to undo), run `uv run router serve`, and export data with `uv run router export --since 7d`.

## Evaluate

The golden set lives in `data/golden/golden.jsonl` (one JSON object per line: prompt, context, reference answer, 2-5 rubric points, expected tier, train/test split). It is git-ignored because it is built from real prompts; bring your own file in that format (see `tests/fixtures/golden_small.jsonl`).

```bash
uv run router eval validate                    # schema, tiers and split counts
uv run router eval smoke --item g-0007         # one item through every arm
uv run router eval calibrate                   # fit the engine's temperature on train, report on test
uv run router eval run --split test            # every arm + router + oracle, judged by Gemini Argon
uv run router eval report                      # latest run: arm table, routing matrix, judge agreement
```

Claude arms run through `claude -p` with your Claude Code login; Gemini arms and the judge need `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in `.env`. Reports land in `data/reports/`, and the dashboard's Eval panel shows the same numbers.

The dashboard UI is Preact + TypeScript + Vite in `dashboard/`; the built `dashboard/dist` is committed, so Node is only needed to change it (`make dashboard`, `make dashboard-check`).
