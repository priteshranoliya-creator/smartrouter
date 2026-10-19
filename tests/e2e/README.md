# End-to-end checklist (manual)

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

## Active mode (T6.2)

Leaves the main router and its database alone: a second router on port 8790 with its own database.

1. Start a second router (in another terminal) and check `curl -s 127.0.0.1:8790/healthz` shows `"mode":"active"`:
   ```sh
   uv run python -c '
   from dataclasses import replace; import uvicorn
   from smartrouter.app import create_app; from smartrouter.settings import Mode, load_settings
   s = load_settings(); r = s.router
   r = r.model_copy(update={"mode": Mode.ACTIVE, "allow_active": True,
                            "paths": r.paths.model_copy(update={"db": "/tmp/e2e-router.db"})})
   uvicorn.run(create_app(replace(s, router=r)), host="127.0.0.1", port=8790)'
   ```
2. `ROUTER_PORT=8790 uv run router install --project /tmp/e2e --manifest-dir /tmp/e2e-manifests`.
3. Three turns on Haiku as above (put `--allowedTools=Read` with `=`, or it swallows the prompt): `Reply with just the word: ready`, `@sonnet Read notes.txt and reply with only the number in it.`, `Reply with just the word: after`.
4. Check the database:
   - every request has `overhead_ms` of a few ms, status 200, no `error`
   - turn 2: both main requests `requested_model` Haiku, `sent_model` Sonnet (the override applies from the first request)
   - turn 3: Claude Code asks for Haiku, the request goes out on Sonnet (a new turn starts on the session's model); the decision is `cooldown`
   - `sessions.current_model` Sonnet, `last_switch_turn` 2
5. Stop the server and uninstall as above.

The switch on a turn's second request and the fallback retry are covered by `tests/integration/test_active.py`; a real 4xx from a routed model is not reproducible on demand.

## Runs

| Date | Milestone | Result |
| --- | --- | --- |
| 2026-10-19 | M2 (shadow, observe only) | Pass. 1 session, 3 turns (override parsed), 5 requests (4 main, 1 compaction) all linked to turns, 1 compaction (prefix 35,793), session_models prefix 36,981. Uninstall removed `.claude/`. |
| 2026-10-19 | M3 (shadow, ScaleDown decisions) | Pass. 4 turns, 4 decision rows (free_window, same_tier, payback, override), ScaleDown 572–1461 ms; all 5 main requests have a counterfactual; requests went out unchanged on Haiku. The design prompt scored D at 0.72, under `upgrade_confidence` 0.75, so the router stayed: a threshold to revisit in T6.1. |
| 2026-10-19 | M4 (dashboard) | Pass. Same 4-turn shadow session; all six panels rendered in headless Chrome at 1440 px and inside a 390 px frame (no horizontal scroll); /healthz ok; stats: 1 session, 4 turns, 5 requests, 1 switch (override), 4 rules. |
| 2026-10-19 | M6 (active, T6.2) | Pass. Router on 8790, 1 session, 3 turns, 4 main requests. Turn 2 (`@sonnet`, one Read tool call): both requests asked Haiku, went out on Sonnet. Turn 3 asked Haiku, went out on Sonnet, decision `cooldown`. `last_switch_turn` 2. Proxy overhead 1.6–1.8 ms per request. ScaleDown answered turns 1–2 after the 2.5 s deadline (5 s on a cold call), so they logged `decision_timeout` and stayed on the session's model, as designed. No errors. |
