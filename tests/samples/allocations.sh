#!/usr/bin/env bash
# Static fixtures with real allocator imports. No sample binary is executed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
mkdir -p "$OUT"
printf 'LIBRARY ucrtbase.dll\nEXPORTS\n malloc\n free\n' > "$OUT/allocation_imports.def"
for opt in O0 O2; do
  clang -arch arm64 "-$opt" -shared -o "$OUT/allocation_roundtrips_$opt.dylib" "$HERE/src/allocation_roundtrips.c"
  for target in x86_64-pc-windows-msvc i686-pc-windows-msvc; do
    id=x86_64-pe; machine=x64
    if [ "$target" = i686-pc-windows-msvc ]; then id=i686-pe; machine=x86; fi
    lld-link /lib /def:"$OUT/allocation_imports.def" /machine:"$machine" /out:"$OUT/allocation_imports_$id.lib"
    clang -target "$target" "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$HERE/src/allocation_roundtrips.c" -o "$OUT/allocation_roundtrips_${id}_$opt.obj"
    lld-link /dll /noentry /nodefaultlib /out:"$OUT/allocation_roundtrips_${id}_$opt.dll" "$OUT/allocation_roundtrips_${id}_$opt.obj" "$OUT/allocation_imports_$id.lib"
  done
  clang -target x86_64-linux-gnu -fPIC "-$opt" -ffreestanding -fno-builtin -fno-stack-protector -c "$HERE/src/allocation_roundtrips.c" -o "$OUT/allocation_roundtrips_x86_64-elf_$opt.o"
  ld.lld -shared -Bsymbolic -nostdlib -o "$OUT/allocation_roundtrips_x86_64-elf_$opt.so" "$OUT/allocation_roundtrips_x86_64-elf_$opt.o"
done
rm -f "$OUT"/allocation_imports.def "$OUT"/allocation_imports_*.lib "$OUT"/allocation_imports_*.exp "$OUT"/allocation_roundtrips_*.obj "$OUT"/allocation_roundtrips_*.o "$OUT"/allocation_roundtrips_*.lib "$OUT"/allocation_roundtrips_*.exp
