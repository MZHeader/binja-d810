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
# Shift-boundary fixtures (spec section 6.5), as exact assembly.
clang -target x86_64-linux-gnu -c "$HERE/src/shifts_x86_64.S" -o "$OUT/shifts_x86_64.o"
ld.lld -shared -o "$OUT/shifts_x86_64.so" "$OUT/shifts_x86_64.o"
clang -arch arm64 -shared -o "$OUT/shifts_arm64.dylib" "$HERE/src/shifts_arm64.S"
ls -la "$OUT"
