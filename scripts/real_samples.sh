#!/usr/bin/env bash
# Measure real samples, one Binary Ninja process per sample. Static analysis only.
# Usage: BINJA_D810_SAMPLES=<dir> scripts/real_samples.sh LABEL [sample ...]
# Results: .local/real/LABEL/<sample>.json (git-ignored). Set BINJA_D810_PLUGIN to measure another
# plugin revision (e.g. a v1.0 worktree). BINJA_D810_SAMPLE_TIMEOUT: seconds per sample (default 1500).
# BINJA_D810_FUNCTION_SECONDS: the plugin's per-function budget for this run (default: the setting's);
# Binary Ninja's own per-function limit is raised to allow it.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
: "${BINJA_D810_SAMPLES:?set BINJA_D810_SAMPLES to the samples directory}"
LABEL="${1:?usage: scripts/real_samples.sh LABEL [sample ...]}"
shift
DEST="$REPO/.local/real/$LABEL"
mkdir -p "$DEST"
if [ "$#" -eq 0 ]; then
  # Smallest first, so an expensive binary cannot hold up the others.
  set -- $(cd "$BINJA_D810_SAMPLES" && ls -Sr | grep -v '^\.')
fi
for sample in "$@"; do
  run="$DEST/run-$sample"
  start=$(date +%s)
  BINJA_D810_OUT="$run" BINJA_D810_SAMPLE="$sample" BINJA_D810_TIMEOUT="${BINJA_D810_SAMPLE_TIMEOUT:-1500}" \
    "$REPO/scripts/binja_pytest.sh" "$REPO/tests/binja/real/test_real_samples.py" > "$DEST/$sample.log" 2>&1
  code=$?
  if [ -f "$run/real/$sample.json" ]; then
    cp "$run/real/$sample.json" "$DEST/$sample.json"
  else
    echo '{}' > "$DEST/$sample.json"
  fi
  /usr/bin/python3 - "$DEST/$sample.json" "$code" "$(( $(date +%s) - start ))" <<'PY'
import json, sys
path, code, seconds = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
data = json.load(open(path))
if data.get("status") != "complete":
    data["status"] = {97: "timeout", 98: "crashed"}.get(code, f"failed (exit {code})")
data["wall_s"] = seconds
json.dump(data, open(path, "w"), indent=1)
print(f"{data.get('sample', path)}: {data['status']} in {seconds}s")
PY
done
