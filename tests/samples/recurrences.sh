#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
uv run --project "$HERE/../.." python "$HERE/gen_recurrences.py"
for opt in O0 O2; do
  clang -arch arm64 "-$opt" -shared -o "$OUT/recurrences_$opt.dylib" "$OUT/recurrences.c"
  for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
    id=x86_64-pe
    if [ "$target" = i686-pc-windows-msvc ]; then id=i686-pe; fi
    clang -target "$target" "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$OUT/recurrences.c" -o "$OUT/recurrences_${id}_$opt.obj"
    lld-link /dll /noentry /nodefaultlib /out:"$OUT/recurrences_${id}_$opt.dll" "$OUT/recurrences_${id}_$opt.obj"
  done
  clang -target x86_64-linux-gnu -fPIC "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$OUT/recurrences.c" -o "$OUT/recurrences_x86_64-elf_$opt.o"
  ld.lld -shared -Bsymbolic -nostdlib -o "$OUT/recurrences_x86_64-elf_$opt.so" "$OUT/recurrences_x86_64-elf_$opt.o"
done
rm -f "$OUT"/recurrences_*.obj "$OUT"/recurrences_*.o "$OUT"/recurrences_*.lib "$OUT"/recurrences_*.exp
