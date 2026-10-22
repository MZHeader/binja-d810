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
SOURCES=("$OUT/mba.c" "$OUT/opaque.c" "$HERE/src/regression.c" "$HERE/src/safety.c" "$HERE/src/effects.c" "$HERE/src/hlil_fixtures.c" "$HERE/src/predicates.c" "$HERE/src/bcf.c" "$HERE/src/div.c" "$HERE/src/flow.c" "$HERE/src/flatten.c" "$HERE/src/substitution.c" "$HERE/src/keys.c" "$HERE/src/key_bounds.c" "$HERE/src/pointer_uses.c" "$HERE/src/callees.c" "$HERE/src/outlined.c" "$HERE/src/mixed_outlined.c" "$HERE/src/loops.c")
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
# Exact-assembly fixtures: shift boundaries, effects, partial registers, carry-flag selects.
for asm in shifts_x86_64 effects_x86_64 partial_x86_64 select_x86_64; do
  clang -target x86_64-linux-gnu -c "$HERE/src/$asm.S" -o "$OUT/$asm.o"
  ld.lld -shared -o "$OUT/$asm.so" "$OUT/$asm.o"
done
clang -target i686-linux-gnu -c "$HERE/src/partial_i686.S" -o "$OUT/partial_i686.o"
ld.lld -shared -o "$OUT/partial_i686.so" "$OUT/partial_i686.o"
clang -arch arm64 -shared -o "$OUT/shifts_arm64.dylib" "$HERE/src/shifts_arm64.S"
# Internal branches misclassified as tail calls, plus actual-call and writable-table controls.
clang -arch arm64 -shared -o "$OUT/internal_branches.dylib" "$HERE/src/internal_branches.S"
clang -target x86_64-linux-gnu -fPIC -c "$HERE/src/internal_branches.S" -o "$OUT/internal_branches_x86_64.o"
ld.lld -shared -o "$OUT/internal_branches_x86_64.so" "$OUT/internal_branches_x86_64.o"
for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
  id="$([ "$target" = x86_64-pc-windows-msvc ] && echo x86_64-pe || echo i686-pe)"
  clang -target "$target" -c "$HERE/src/internal_branches.S" -o "$OUT/internal_branches_$id.obj"
  lld-link /dll /noentry /nodefaultlib /safeseh:no /export:internal_branch /export:internal_call \
    /export:written_internal_branch /export:set_internal_pointer \
    /out:"$OUT/internal_branches_$id.dll" "$OUT/internal_branches_$id.obj" >/dev/null
done
# Outlined dispatchers carrying a callback in an undeclared register.
clang -arch arm64 -shared -o "$OUT/entry_outlined.dylib" "$HERE/src/entry_outlined.S"
clang -target x86_64-linux-gnu -fPIC -c "$HERE/src/entry_outlined.S" -o "$OUT/entry_outlined_x86_64.o"
ld.lld -shared -Bsymbolic -o "$OUT/entry_outlined_x86_64.so" "$OUT/entry_outlined_x86_64.o"
for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
  id="$([ "$target" = x86_64-pc-windows-msvc ] && echo x86_64-pe || echo i686-pe)"
  clang -target "$target" -c "$HERE/src/entry_outlined.S" -o "$OUT/entry_outlined_$id.obj"
  lld-link /dll /noentry /nodefaultlib /safeseh:no \
    /export:entry_dispatch /export:entry_h1 /export:entry_h2 /export:entry_h3 /export:entry_forward \
    /export:volatile_dispatch /export:volatile_h1 /export:volatile_h2 /export:volatile_h3 \
    /export:clobber_dispatch /export:clobber_h1 /export:clobber_h2 /export:clobber_h3 \
    /out:"$OUT/entry_outlined_$id.dll" "$OUT/entry_outlined_$id.obj" >/dev/null
done
# Handlers carrying declared register parameters and stack state through tail branches.
clang -arch arm64 -shared -o "$OUT/inherited_outlined.dylib" "$HERE/src/inherited_outlined.S"
clang -target x86_64-linux-gnu -fPIC -c "$HERE/src/inherited_outlined.S" -o "$OUT/inherited_outlined_x86_64.o"
ld.lld -shared -Bsymbolic -o "$OUT/inherited_outlined_x86_64.so" "$OUT/inherited_outlined_x86_64.o"
for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
  id="$([ "$target" = x86_64-pc-windows-msvc ] && echo x86_64-pe || echo i686-pe)"
  clang -target "$target" -c "$HERE/src/inherited_outlined.S" -o "$OUT/inherited_outlined_$id.obj"
  lld-link /dll /noentry /nodefaultlib /safeseh:no \
    /export:incoming_dispatch /export:incoming_h1 /export:incoming_h2 /export:incoming_h3 \
    /export:changed_dispatch /export:changed_h1 /export:changed_h2 /export:changed_h3 \
    /export:shifted_dispatch /export:shifted_h1 /export:shifted_h2 /export:shifted_h3 \
    /out:"$OUT/inherited_outlined_$id.dll" "$OUT/inherited_outlined_$id.obj" >/dev/null
done
rm -f "$OUT"/*.obj "$OUT"/*.o "$OUT"/*.lib "$OUT"/*.exp
ls "$OUT"
