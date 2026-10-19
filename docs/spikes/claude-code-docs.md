# Pre-spike research for T0.1 and T0.2: what the Claude Code docs say

Date: 2026-10-19 · Claude Code on this machine: 2.1.303.
Sources: [authentication](https://code.claude.com/docs/en/authentication), [LLM gateways](https://code.claude.com/docs/en/llm-gateway), [gateway compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol).
These are documented claims; T0.1 and T0.2 still have to confirm them on real traffic.

## Auth on a Claude Team plan (no API key)

- Setting only `ANTHROPIC_BASE_URL` keeps the claude.ai (Team) login as the credential; requests go through the proxy and count against the plan's usage limits. The proxy must forward `anthropic-beta` verbatim, because it carries the OAuth capability; stripping it returns `401`.
- `claude setup-token` mints a one-year OAuth token for Pro, Max, **Team** and Enterprise plans, used as `CLAUDE_CODE_OAUTH_TOKEN`. It can only make model requests. Use it for scripted `claude -p` runs (spikes, eval runner), not for calling the API directly from our own code.
- Consequence for the TDD: there are no billed dollars. Every cost in the dashboard is **API-equivalent cost** computed from `usage` × `pricing.yaml`. The usage numbers are exact; the dollars are notional.
- Consequence for the eval runner (T5.2): the TDD assumes `ANTHROPIC_API_KEY`. Without one, Claude arms run through `claude -p --model <id> --output-format json` (which reports usage).

## Session key (T0.1)

- Every request carries `x-claude-code-session-id`: "a unique identifier for the current Claude Code session". This is the likely T0.1 answer; the spike must confirm it equals the hook's `session_id`.
- Subagent requests also carry `x-claude-code-agent-id` (and `x-claude-code-parent-agent-id` when nested).

## Hint headers (opt-in through a custom base URL)

With `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` (needs v2.1.273+; v2.1.283+ for prompt id) Claude Code adds:

| Header | Could replace in the TDD |
| --- | --- |
| `x-claude-code-request-class`: `main` / `subagent` / `workflow` / `compaction` / `auxiliary` | The "rewrite only if `model` matches the main model" heuristic for background requests (section 4) |
| `x-claude-code-prompt-id`: UUID shared by every request serving one user prompt | Turn correlation in the proxy |
| `x-claude-code-context-compacted`: on the first main request after compaction | A second signal for the `free_window` rule, next to the PreCompact hook |
| `x-claude-code-compaction`: on the compaction request itself | Logging compaction requests as their own kind |

The hooks are still needed: the proxy gets the prompt text from the hook in time to start Laya before the first API request arrives.

## Thinking blocks after a model switch (T0.2)

- When the upstream rejects a thinking signature (including `bound to a different conversation`), Claude Code itself strips earlier thinking blocks, retries, and keeps them out for the rest of the session. A proxy that rewrites `system`, `tools` or earlier `messages` can trigger that rejection.
- Haiku 5.5 and later get `thinking: {"type": "adaptive"}`.

## Other notes

- Forward `anthropic-*` headers and all body fields as an open list; forward error bodies unmodified (Claude Code's retry logic matches their wording); keep the `system` array unchanged (the attribution block must stay first and separate).
- Forward `retry-after`, `x-should-retry` and `anthropic-ratelimit-unified-*` response headers; the last one drives the Team-plan usage display.
- Claude Code sends `HEAD /api/hello` at startup; it can be rejected or passed through.
- The docs state Anthropic "doesn't support routing Claude Code to non-Claude models through any gateway". It is possible, but T7.1 (Gemini live) runs outside supported territory.
