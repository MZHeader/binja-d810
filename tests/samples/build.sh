#!/usr/bin/env bash
# Build ARM64 sample dylibs at -O0 and -O2 into tests/samples/build.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
mkdir -p "$OUT"
uv run --project "$HERE/../.." python "$HERE/gen_mba.py" --out "$OUT"
for opt in O0 O2; do
  clang -arch arm64 "-$opt" -shared -o "$OUT/mba_$opt.dylib" "$OUT/mba.c"
  clang -arch arm64 "-$opt" -shared -o "$OUT/regression_$opt.dylib" "$HERE/src/regression.c"
  clang -arch arm64 "-$opt" -shared -o "$OUT/safety_$opt.dylib" "$HERE/src/safety.c"
done
ls -la "$OUT"
