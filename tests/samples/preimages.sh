#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
uv run --project "$HERE/../.." python "$HERE/gen_preimages.py"
for opt in O0 O2; do
  clang -arch arm64 "-$opt" -shared -o "$OUT/preimages_$opt.dylib" "$OUT/preimages.c"
  for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
    id=x86_64-pe
    if [ "$target" = i686-pc-windows-msvc ]; then id=i686-pe; fi
    clang -target "$target" "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$OUT/preimages.c" -o "$OUT/preimages_${id}_$opt.obj"
    lld-link /dll /noentry /nodefaultlib /out:"$OUT/preimages_${id}_$opt.dll" "$OUT/preimages_${id}_$opt.obj"
  done
  clang -target x86_64-linux-gnu -fPIC "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$OUT/preimages.c" -o "$OUT/preimages_x86_64-elf_$opt.o"
  ld.lld -shared -Bsymbolic -nostdlib -o "$OUT/preimages_x86_64-elf_$opt.so" "$OUT/preimages_x86_64-elf_$opt.o"
done
rm -f "$OUT"/preimages_*.obj "$OUT"/preimages_*.o "$OUT"/preimages_*.lib "$OUT"/preimages_*.exp
