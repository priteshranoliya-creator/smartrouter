#!/usr/bin/env bash
# Run one Claude Code turn in the fixture repo through the spike proxy.
# usage: turn.sh <fixture-dir> <session-id> new|resume <prompt> [extra claude flags...]
set -euo pipefail
dir=$1 sid=$2 mode=$3 prompt=$4; shift 4
cd "$dir"
if [ "$mode" = new ]; then flag=(--session-id "$sid"); else flag=(--resume "$sid"); fi
env -u ANTHROPIC_BASE_URL -u CLAUDECODE -u ANTHROPIC_API_KEY \
  claude -p "${flag[@]}" "$@" <<<"$prompt"
