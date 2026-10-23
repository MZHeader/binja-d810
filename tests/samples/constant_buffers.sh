#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
mkdir -p "$OUT"
"${D810_PYTHON:-$HERE/../../.venv/bin/python}" "$HERE/gen_constant_buffers.py" --out "$OUT"
for opt in O0 O2; do
  clang -arch arm64 "-$opt" -fno-vectorize -fno-slp-vectorize -fno-stack-protector -shared -o "$OUT/constant_buffers_$opt.dylib" "$OUT/constant_buffers.c"
  for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
    id=x86_64-pe
    if [ "$target" = i686-pc-windows-msvc ]; then id=i686-pe; fi
    clang -target "$target" "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -fno-vectorize -fno-slp-vectorize -c "$OUT/constant_buffers.c" -o "$OUT/constant_buffers_${id}_$opt.obj"
    lld-link /dll /noentry /nodefaultlib /out:"$OUT/constant_buffers_${id}_$opt.dll" "$OUT/constant_buffers_${id}_$opt.obj"
  done
  clang -target x86_64-linux-gnu -fPIC "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -fno-vectorize -fno-slp-vectorize -c "$OUT/constant_buffers.c" -o "$OUT/constant_buffers_x86_64-elf_$opt.o"
  ld.lld -shared -Bsymbolic -nostdlib -o "$OUT/constant_buffers_x86_64-elf_$opt.so" "$OUT/constant_buffers_x86_64-elf_$opt.o"
done
rm -f "$OUT"/constant_buffers_*.obj "$OUT"/constant_buffers_*.o "$OUT"/constant_buffers_*.lib "$OUT"/constant_buffers_*.exp
