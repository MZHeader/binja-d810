#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
uv run --project "$HERE/../.." python "$HERE/gen_masks.py"
for opt in O0 O2; do
  clang -arch arm64 "-$opt" -shared -o "$OUT/masks_$opt.dylib" "$OUT/masks.c"
  for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
    id=x86_64-pe
    if [ "$target" = i686-pc-windows-msvc ]; then id=i686-pe; fi
    clang -target "$target" "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$OUT/masks.c" -o "$OUT/masks_${id}_$opt.obj"
    lld-link /dll /noentry /nodefaultlib /out:"$OUT/masks_${id}_$opt.dll" "$OUT/masks_${id}_$opt.obj"
  done
  clang -target x86_64-linux-gnu -fPIC "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$OUT/masks.c" -o "$OUT/masks_x86_64-elf_$opt.o"
  ld.lld -shared -Bsymbolic -nostdlib -o "$OUT/masks_x86_64-elf_$opt.so" "$OUT/masks_x86_64-elf_$opt.o"
done
rm -f "$OUT"/masks_*.obj "$OUT"/masks_*.o "$OUT"/masks_*.lib "$OUT"/masks_*.exp
