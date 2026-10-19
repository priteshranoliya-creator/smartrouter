# T0.1 spike: the key that ties a proxied request to its Claude Code session

Date: 2026-10-19 · Claude Code 2.1.303 · auth: Claude Team login (OAuth), no API key
Setup: `spikes/proxy/bare_proxy.py` (logging pass-through on 127.0.0.1:8788) and `spikes/proxy/hook.py`, wired through a fixture project's `.claude/settings.json` (`ANTHROPIC_BASE_URL`, `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`, UserPromptSubmit and PreCompact hooks). Sessions driven with `spikes/proxy/turn.sh` (`claude -p --session-id` / `--resume`). Log: `spikes/proxy/out/log.jsonl` (git-ignored).

Sessions logged: 3 two-turn sessions on Sonnet (9 requests, the second turn using the Read tool), then 1 Haiku session with compaction, then the 3 T0.2 sessions. Every request returned 200.

Note: the log of the 3 Sonnet sessions (and the 512,161-token figure below) was cleared before the slim runs, so those numbers survive only in the session transcript. The current `out/log.jsonl` holds the Haiku session and T0.2, where the session-id and prompt-id matches were re-confirmed on all 16 requests.

## Chosen keys

| Purpose | Key | Evidence |
| --- | --- | --- |
| Request → session | header `x-claude-code-session-id` | On all requests; equals the hook's `session_id` in all sessions. Also present inside `metadata.user_id` (a JSON string with `device_id`, `account_uuid`, `session_id`) as a second source |
| Request → turn | header `x-claude-code-prompt-id` (needs `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`) | Equals the `prompt_id` field in the UserPromptSubmit hook payload (`7e4ccb71-…` in both). All requests of one turn's agent loop (tool calls) share it |
| Main vs side request | header `x-claude-code-request-class` | `main` on turns, `compaction` on the compaction request. No `auxiliary` requests were seen in `-p` mode; interactive sessions still need checking in T2.3 |
| Compaction | PreCompact hook (`trigger`: `manual`/`auto`) + header `x-claude-code-compaction` on the compaction request | `x-claude-code-context-compacted` did **not** appear on the next main request (that turn ran in a new `claude -p` process). Keep the hook as the primary compaction signal, as the TDD designs |

Fallback when hint headers are absent (older Claude Code, or the env var removed): session from `x-claude-code-session-id` or `metadata.user_id`; turn = the latest open turn of that session; class by comparing `model` to the session's main model (TDD section 4).

## Other observations

- Hook payload fields: `session_id`, `prompt_id`, `prompt`, `cwd`, `transcript_path`, `permission_mode`, `hook_event_name`. PreCompact adds `trigger`, `custom_instructions`.
- Auth reaches the proxy as `Authorization: Bearer sk-ant-oat01-…` (OAuth). `anthropic-beta` includes `oauth-2025-04-20` and must be forwarded verbatim (it does today, by forwarding all headers). Logged credentials are redacted to their prefix.
- Other request headers: `anthropic-version`, `x-app`, `user-agent`, `x-stainless-*`, `anthropic-dangerous-direct-browser-access`, `x-claude-code-prev-tool-durations` (on the request after a tool call), `accept-encoding`.
- Requests use `POST /v1/messages?beta=true`. No `count_tokens`, `HEAD /api/hello` or `/v1/models` calls were seen in `-p` mode.
- Body `thinking` is `{"type": "adaptive", "display": "omitted"}`.
- **Context size:** a trivial first turn with the user's full global setup (plugins, skills, MCP servers) wrote **512,161 tokens** to cache. With `--setting-sources project --strict-mcp-config` it was **35,617**. Spikes and tests use the slim flags. This also means real sessions on this machine start with a ~500k-token prefix, which makes the switch tax large.
- **Cache TTL:** Claude Code writes the **1-hour** cache (`cache_creation.ephemeral_1h_input_tokens`) on this account, so writes price at 2.0× input, not 1.25×.
