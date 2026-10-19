# End-to-end checklist (manual until M6)

Runs a real Claude Code session through the router on Haiku 5.5 with a slim setup, so each
request is ~36k tokens. Uses your normal Claude Code login.

1. Make a throwaway project: `mkdir /tmp/e2e && cd /tmp/e2e && git init && echo "The secret number is 4817." > notes.txt`
2. `uv run router install --project /tmp/e2e`, then check `.claude/settings.local.json` has `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_GATEWAY_HINT_HEADERS` and both hooks.
3. `uv run router serve` in another terminal; `curl -s 127.0.0.1:8787/healthz` returns `{"status":"ok"}`.
4. Run four turns in `/tmp/e2e` with `env -u ANTHROPIC_BASE_URL claude -p --model claude-haiku-5-5 --setting-sources project,local --strict-mcp-config` (first with `--session-id <uuid>`, then `--resume <uuid>`):
   `Reply with just the word: ready`, `@haiku Read notes.txt and reply with only the number in it.` (add `--allowedTools Read`), `/compact`, `Reply with just the word: after`.
5. Check `data/router.db`:
   - `sessions`: 1 row, `current_model` set, `state` back to `active`
   - `turns`: 3 rows, seq 1–3, `override = haiku` on seq 2 (`/compact` fires no prompt hook)
   - `requests`: every row has `turn_id`, status 200, tokens and `cost_usd`; one row has `kind = compaction`
   - `compactions`: 1 row, `trigger = manual`, `prefix_tokens_before` set
   - `session_models`: 1 row with `prefix_tokens`
6. Stop the server, `uv run router uninstall --project /tmp/e2e`; the project has no `.claude/` left.

## Runs

| Date | Milestone | Result |
| --- | --- | --- |
| 2026-10-19 | M2 (shadow, observe only) | Pass. 1 session, 3 turns (override parsed), 5 requests (4 main, 1 compaction) all linked to turns, 1 compaction (prefix 35,793), session_models prefix 36,981. Uninstall removed `.claude/`. |
| 2026-10-19 | M3 (shadow, ScaleDown decisions) | Pass. 4 turns, 4 decision rows (free_window, same_tier, payback, override), ScaleDown 572–1461 ms; all 5 main requests have a counterfactual; requests went out unchanged on Haiku. The design prompt scored D at 0.72, under `upgrade_confidence` 0.75, so the router stayed: a threshold to revisit in T6.1. |
| 2026-10-19 | M4 (dashboard) | Pass. Same 4-turn shadow session; all six panels rendered in headless Chrome at 1440 px and inside a 390 px frame (no horizontal scroll); /healthz ok; stats: 1 session, 4 turns, 5 requests, 1 switch (override), 4 rules. |
