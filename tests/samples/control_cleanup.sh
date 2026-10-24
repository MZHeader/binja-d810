#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
mkdir -p "$OUT"
for opt in O0 O2; do
  clang -arch arm64 "-$opt" -shared -o "$OUT/control_cleanup_$opt.dylib" "$HERE/src/control_cleanup.c"
  for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
    id=x86_64-pe
    if [ "$target" = i686-pc-windows-msvc ]; then id=i686-pe; fi
    clang -target "$target" "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$HERE/src/control_cleanup.c" -o "$OUT/control_cleanup_${id}_$opt.obj"
    lld-link /dll /noentry /nodefaultlib /out:"$OUT/control_cleanup_${id}_$opt.dll" "$OUT/control_cleanup_${id}_$opt.obj"
  done
  clang -target x86_64-linux-gnu -fPIC "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$HERE/src/control_cleanup.c" -o "$OUT/control_cleanup_x86_64-elf_$opt.o"
  ld.lld -shared -Bsymbolic -nostdlib -o "$OUT/control_cleanup_x86_64-elf_$opt.so" "$OUT/control_cleanup_x86_64-elf_$opt.o"
done
rm -f "$OUT"/control_cleanup_*.obj "$OUT"/control_cleanup_*.o "$OUT"/control_cleanup_*.lib "$OUT"/control_cleanup_*.exp
