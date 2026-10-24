#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
mkdir -p "$OUT"
for opt in O0 O2; do
  clang -arch arm64 "-$opt" -shared -o "$OUT/boolean_conditions_$opt.dylib" "$HERE/src/boolean_conditions.c"
  for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
    id=x86_64-pe
    if [ "$target" = i686-pc-windows-msvc ]; then id=i686-pe; fi
    clang -target "$target" "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$HERE/src/boolean_conditions.c" -o "$OUT/boolean_conditions_${id}_$opt.obj"
    lld-link /dll /noentry /nodefaultlib /out:"$OUT/boolean_conditions_${id}_$opt.dll" "$OUT/boolean_conditions_${id}_$opt.obj"
  done
  clang -target x86_64-linux-gnu -fPIC "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$HERE/src/boolean_conditions.c" -o "$OUT/boolean_conditions_x86_64-elf_$opt.o"
  ld.lld -shared -Bsymbolic -nostdlib -o "$OUT/boolean_conditions_x86_64-elf_$opt.so" "$OUT/boolean_conditions_x86_64-elf_$opt.o"
done
rm -f "$OUT"/boolean_conditions_*.obj "$OUT"/boolean_conditions_*.o "$OUT"/boolean_conditions_*.lib "$OUT"/boolean_conditions_*.exp
