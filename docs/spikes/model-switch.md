# T0.2 spike: switching models mid-session with thinking on

Date: 2026-10-19 · Claude Code 2.1.303 · proxy and fixture as in `docs/spikes/session-key.md`
Script: `spikes/proxy/t02.sh <fixture> X Y`. Turn 1 on X (thinking + a Read tool call), turn 2 rewritten by the proxy to Y (Claude Code still asks for X), turn 3 back on X. Flags `--setting-sources project --strict-mcp-config --effort high` to keep each request at ~36k tokens. Small models, per the user's instruction: Opus used on one turn per pair only.

## Result per direction (subscription / OAuth auth)

| Direction | Thinking blocks from the other model in history | Status | Retries | Answer correct |
| --- | --- | --- | --- | --- |
| Haiku → Sonnet | 2 | 200 | 0 | yes |
| Sonnet → Haiku | 2 | 200 | 0 | yes |
| Haiku → Opus | 2 | 200 | 0 | yes |
| Opus → Haiku | 2 | 200 | 0 | yes |
| Sonnet → Opus | 1 | 200 | 0 | yes |
| Opus → Sonnet | 1 | 200 | 0 | yes |

In the log, the X → Y rows are the requests with a `rewritten` field; the Y → X rows are the turn-3 requests that follow them, sent unrewritten as X with Y's thinking blocks in history.

API-key auth: **not tested.** The user has no API key (Team plan); see `docs/tdd-changes.md` #3.

## Decision on stripping thinking blocks

**Do not strip.** All three Claude 5.5 models accepted each other's thinking blocks with `display: omitted` in both directions; Claude Code itself did not strip them (history still carried them after switching back). Keep the request body byte-identical apart from `model`. If a future model rejects them, Claude Code already removes prior thinking blocks and retries on a signature rejection (gateway docs), so the proxy only has to forward the error body unmodified.

## Cache findings (change the policy's cost model)

| Request | cache write | cache read |
| --- | --- | --- |
| First switch to a model idle in this session | ~36k (whole prefix) | 0 |
| Switch back to the original model, minutes later | 95–161 | 35.0k–36.0k |
| First request of a model whose system + tools prefix another session cached recently | ~6k | ~29k |

1. **Each model keeps its own warm cache.** Switching back to a model used within its TTL is nearly free. The policy must track cache warmth **per model per session** (last request time per model), not just "the current model".
2. **The system + tools prefix (~29k) is shared across sessions** of the same model and Claude Code version, so a switch target is often partly warm. The switch tax should use the **conversation part** of the prefix when the target model was used recently by any session.
3. **TTL is 1 hour** (`ephemeral_1h`), so write price is 2.0× input and "cache cold" means idle > 3600 s, not 300 s. The `free_window` idle threshold should come from the observed TTL.
4. After a manual compaction, the next turn read ~31.8k from cache: compaction rewrites the conversation but not the system + tools prefix. The post-compaction window is free for the conversation part only.

## Proposed TDD updates (for approval)

- `free_window` cache-cold threshold: per model, from the TTL seen in `usage` (default 3600 s on this account).
- Switch tax: `prefix_conversation × write_price_1h(new) − prefix × read_price(current)`, where a recently used target keeps its warm part. The policy input gains `last_used_at` per model.
