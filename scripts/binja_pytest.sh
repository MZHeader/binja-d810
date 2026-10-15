#!/usr/bin/env bash
# Run pytest inside a fresh Binary Ninja GUI instance. Usage: scripts/binja_pytest.sh [pytest args]
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
OUT="$REPO/.binja-test"
BN="/Applications/Binary Ninja.app/Contents/MacOS/binaryninja"
rm -rf "$OUT" && mkdir -p "$OUT"
if [ "$#" -eq 0 ]; then set -- "$REPO/tests/binja"; fi
ARGS=$(/usr/bin/python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' -q -p no:cacheprovider --rootdir "$REPO" "$@")
BINJA_D810_PYTEST_ARGS="$ARGS" BINJA_D810_PYTEST_OUT="$OUT" "$BN" -n -e >"$OUT/binja.log" 2>&1 &
PID=$!
for _ in $(seq 1 900); do
  [ -f "$OUT/result.json" ] && break
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
wait "$PID" 2>/dev/null || true
cat "$OUT/pytest.log" 2>/dev/null || { echo "no pytest.log; Binary Ninja log:"; tail -50 "$OUT/binja.log"; exit 98; }
/usr/bin/python3 -c 'import json, sys; sys.exit(json.load(open(sys.argv[1]))["exit_code"])' "$OUT/result.json"
