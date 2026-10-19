Recorded Claude Code traffic for the proxy tests.

- `text`, `tool_use`, `text_after_tool`: three consecutive `/v1/messages` responses from one Haiku 5.5 session (2026-10-19), recorded with `spikes/proxy/record.py`. `.sse.gz` is the gzip body as Claude Code receives it; `.sse` is the same stream uncompressed. Local paths in the tool input were replaced with `/tmp/fixture-repo`.
- `non_streaming.json`, `error_400.json`: hand-written in the documented Messages API shapes.

Usage appears in `message_start` (with the 5m/1h `cache_creation` split) and again, cumulative, in `message_delta` (without the split).
