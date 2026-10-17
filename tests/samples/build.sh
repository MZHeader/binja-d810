#!/usr/bin/env bash
# Build the sample binaries into tests/samples/build: the generated MBA corpus and the fixture
# sources for every target in tests/binja/arch.py, at -O0 and -O2, plus exact-assembly fixtures.
# ARM64 keeps v1's names (mba_O2.dylib); other targets add the build id (mba_x86_64-pe_O2.dll).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
mkdir -p "$OUT"
uv run --project "$HERE/../.." python "$HERE/gen_mba.py" --out "$OUT"
uv run --project "$HERE/../.." python "$HERE/gen_opaque.py" --out "$OUT"
SOURCES=("$OUT/mba.c" "$OUT/opaque.c" "$HERE/src/regression.c" "$HERE/src/safety.c" "$HERE/src/effects.c" "$HERE/src/hlil_fixtures.c" "$HERE/src/predicates.c" "$HERE/src/bcf.c")
FLAGS=(-ffreestanding -fno-builtin -fno-stack-protector)
for opt in O0 O2; do
  for src in "${SOURCES[@]}"; do
    base="$(basename "$src" .c)"
    clang -arch arm64 "-$opt" -shared -o "$OUT/${base}_$opt.dylib" "$src"
    for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
      id="$([ "$target" = x86_64-pc-windows-msvc ] && echo x86_64-pe || echo i686-pe)"
      clang -target "$target" "-$opt" "${FLAGS[@]}" -c "$src" -o "$OUT/${base}_${id}_$opt.obj"
      lld-link /dll /noentry /nodefaultlib /out:"$OUT/${base}_${id}_$opt.dll" "$OUT/${base}_${id}_$opt.obj" >/dev/null
    done
    clang -target x86_64-linux-gnu -fPIC "-$opt" "${FLAGS[@]}" -c "$src" -o "$OUT/${base}_x86_64-elf_$opt.o"
    ld.lld -shared -nostdlib -o "$OUT/${base}_x86_64-elf_$opt.so" "$OUT/${base}_x86_64-elf_$opt.o"
  done
done
# Exact-assembly fixtures: shift boundaries (spec 6.5), effects (5.2), partial registers (6.1).
for asm in shifts_x86_64 effects_x86_64 partial_x86_64; do
  clang -target x86_64-linux-gnu -c "$HERE/src/$asm.S" -o "$OUT/$asm.o"
  ld.lld -shared -o "$OUT/$asm.so" "$OUT/$asm.o"
done
clang -target i686-linux-gnu -c "$HERE/src/partial_i686.S" -o "$OUT/partial_i686.o"
ld.lld -shared -o "$OUT/partial_i686.so" "$OUT/partial_i686.o"
clang -arch arm64 -shared -o "$OUT/shifts_arm64.dylib" "$HERE/src/shifts_arm64.S"
rm -f "$OUT"/*.obj "$OUT"/*.o "$OUT"/*.lib "$OUT"/*.exp
ls "$OUT"
