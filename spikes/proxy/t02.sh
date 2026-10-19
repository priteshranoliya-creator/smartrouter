#!/usr/bin/env bash
# T0.2: model switch X -> Y -> X mid-session through the proxy, thinking on.
# usage: t02.sh <fixture-dir> <modelX> <modelY>
set -euo pipefail
FX=$1 X=$2 Y=$3
HERE=$(cd "$(dirname "$0")" && pwd)
SLIM=(--setting-sources project --strict-mcp-config --effort high --allowedTools Read)
S=$(uuidgen | tr A-Z a-z)
echo "== $X -> $Y -> $X  session $S"
rm -f "$HERE/out/override.json"
"$HERE/turn.sh" "$FX" "$S" new "Think it through step by step: compute 37*43-19. Then read notes.txt and add the number in it to your result. Reply with only the final number." --model "$X" "${SLIM[@]}" 2>&1 | tail -1
echo "{\"model\": \"$Y\"}" > "$HERE/out/override.json"
"$HERE/turn.sh" "$FX" "$S" resume "Think carefully: is that final number divisible by 7? Reply yes or no and the remainder." --model "$X" "${SLIM[@]}" 2>&1 | tail -1
rm -f "$HERE/out/override.json"
"$HERE/turn.sh" "$FX" "$S" resume "Think carefully: what is that final number modulo 9? Reply with only the number." --model "$X" "${SLIM[@]}" 2>&1 | tail -1
