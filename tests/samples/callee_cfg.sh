#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
mkdir -p "$OUT"
for opt in O0 O2; do
  clang -arch arm64 "-$opt" -shared -o "$OUT/callee_cfg_$opt.dylib" "$HERE/src/callee_cfg.c"
  for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
    id=x86_64-pe
    if [ "$target" = i686-pc-windows-msvc ]; then id=i686-pe; fi
    clang -target "$target" "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$HERE/src/callee_cfg.c" -o "$OUT/callee_cfg_${id}_$opt.obj"
    lld-link /dll /noentry /nodefaultlib /out:"$OUT/callee_cfg_${id}_$opt.dll" "$OUT/callee_cfg_${id}_$opt.obj"
  done
  clang -target x86_64-linux-gnu -fPIC "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$HERE/src/callee_cfg.c" -o "$OUT/callee_cfg_x86_64-elf_$opt.o"
  ld.lld -shared -Bsymbolic -nostdlib -o "$OUT/callee_cfg_x86_64-elf_$opt.so" "$OUT/callee_cfg_x86_64-elf_$opt.o"
done
rm -f "$OUT"/callee_cfg_*.obj "$OUT"/callee_cfg_*.o "$OUT"/callee_cfg_*.lib "$OUT"/callee_cfg_*.exp
