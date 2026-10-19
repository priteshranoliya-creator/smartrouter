# smartrouter

A local router for Claude Code that picks the model (Haiku, Sonnet or Opus) for every prompt, weighing a decision model's answer against the cost of losing the prompt cache, and logs every decision, token and dollar to a local dashboard.

**Status:** proof of concept, milestone M0 (spikes) done. Not ready to use yet.

- Design changes from the original technical design: [`docs/tdd-changes.md`](docs/tdd-changes.md)
- Spike findings: [`docs/spikes/`](docs/spikes/)

Runs on `127.0.0.1` only. Decision engines are pluggable (ScaleDown `/classify` by default, Laya as an alternative).
