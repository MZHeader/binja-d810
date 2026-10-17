#!/usr/bin/env bash
# Run pytest inside a fresh Binary Ninja GUI instance. Usage: scripts/binja_pytest.sh [pytest args]
#
# BINJA_D810_OUT     output directory (default .binja-test); wiped at the start of the run.
# BINJA_D810_PLUGIN  plugin directory to test instead of the installed one. Binary Ninja then runs
#                    with an isolated user directory ($OUT/user) that links the real license and
#                    Python packages, so another revision (e.g. a v1.0 worktree) can be measured
#                    without touching the user's plugin folder.
# BINJA_D810_TIMEOUT seconds before the instance is killed (default 3600; the full suite takes about 26 minutes).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
OUT="${BINJA_D810_OUT:-$REPO/.binja-test}"
BN="/Applications/Binary Ninja.app/Contents/MacOS/binaryninja"
BN_USER="$HOME/Library/Application Support/Binary Ninja"
TIMEOUT="${BINJA_D810_TIMEOUT:-3600}"
rm -rf "$OUT" && mkdir -p "$OUT"
if [ "$#" -eq 0 ]; then set -- "$REPO/tests/binja"; fi
ARGS=$(/usr/bin/python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' -q -p no:cacheprovider --rootdir "$REPO" "$@")
ENV=(BINJA_D810_PYTEST_ARGS="$ARGS" BINJA_D810_PYTEST_OUT="$OUT")
if [ -n "${BINJA_D810_PLUGIN:-}" ]; then
  PLUGIN="$(cd "$BINJA_D810_PLUGIN" && pwd)"
  mkdir -p "$OUT/user/plugins"
  ln -s "$BN_USER/license.dat" "$OUT/user/license.dat"
  ln -s "$BN_USER/python313" "$OUT/user/python313"
  echo '{}' > "$OUT/user/settings.json"
  ln -s "$PLUGIN" "$OUT/user/plugins/binja_d810"
  ENV+=(BN_USER_DIRECTORY="$OUT/user")
  echo "plugin under test: $PLUGIN"
fi
env "${ENV[@]}" "$BN" -n -e >"$OUT/binja.log" 2>&1 &
PID=$!
for _ in $(seq 1 "$TIMEOUT"); do
  [ -f "$OUT/result.json" ] && break
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
if [ ! -f "$OUT/result.json" ] && kill -0 "$PID" 2>/dev/null; then
  echo "timeout after ${TIMEOUT}s; killing Binary Ninja"
  kill "$PID" 2>/dev/null || true
  sleep 2
  kill -9 "$PID" 2>/dev/null || true
fi
wait "$PID" 2>/dev/null || true
cat "$OUT/pytest.log" 2>/dev/null || { echo "no pytest.log; Binary Ninja log:"; tail -50 "$OUT/binja.log"; exit 98; }
[ -f "$OUT/result.json" ] || exit 97
/usr/bin/python3 -c 'import json, sys; sys.exit(json.load(open(sys.argv[1]))["exit_code"])' "$OUT/result.json"
