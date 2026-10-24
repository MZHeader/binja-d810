"""Generate constant-input C buffer fixtures and an independent arithmetic manifest."""

import argparse
import json
from pathlib import Path
import random
import re


def fixture(seed):
    rng = random.Random(seed)
    rotate, multiplier, add, shift = rng.randrange(1, 32), rng.getrandbits(32), rng.getrandbits(32), rng.randrange(32)
    state = initial = rng.getrandbits(32)
    plaintext = bytes(rng.randrange(32, 127) for _ in range(rng.randrange(4, 64))) + b"\0"
    keys, cipher = [], []
    for byte in plaintext:
        key = rng.getrandbits(32)
        state = ((((state >> rotate) | (state << (32 - rotate))) & 0xffffffff) * multiplier + add + key) & 0xffffffff
        keys.append(key)
        cipher.append(((state >> shift) ^ byte) & 255)
    return dict(seed=seed, rotate=rotate, multiplier=multiplier, add=add, shift=shift,
                initial=initial, keys=keys, cipher=cipher, plaintext=list(plaintext))


HEADER = '''#include <stdint.h>
#ifdef _WIN32
#define F __declspec(dllexport) __attribute__((noinline,used))
#else
#define F __attribute__((noinline,used,visibility("default")))
#endif
#define STATE(x) __asm__ volatile("" : "+r"(x))
#define BUFFER(x) __asm__ volatile("" : "+m"(x) : : "memory")
volatile uint32_t constant_buffer_observer;
F void constant_buffer_use(const uint8_t *p) { constant_buffer_observer=*p; }
F void constant_buffer_length(const uint8_t *p, uint32_t n) { constant_buffer_observer=p[n-1]; }
F uint32_t constant_buffer_unknown(uint32_t x) { return constant_buffer_observer+x; }
uint8_t *volatile constant_buffer_saved;
F void constant_buffer_capture(uint8_t *p) { constant_buffer_saved=p; }
F void ce_length(void) {
  uint8_t buffer[7]; uint32_t s=65;
  for(uint32_t i=0;i<7;++i) { STATE(s); buffer[i]=(uint8_t)s; s+=1; }
  BUFFER(buffer); constant_buffer_length(buffer,7);
}
F void ce_unknown(uint32_t s) {
  uint8_t buffer[8];
  for(uint32_t i=0;i<8;++i) { STATE(s); buffer[i]=(uint8_t)s; s+=1; }
  BUFFER(buffer); constant_buffer_use(buffer);
}
F void ce_writable(void) {
  uint8_t buffer[8]; uint32_t s=constant_buffer_observer;
  for(uint32_t i=0;i<8;++i) { STATE(s); buffer[i]=(uint8_t)s; s+=1; }
  BUFFER(buffer); constant_buffer_use(buffer);
}
F void ce_call(void) {
  uint8_t buffer[8]; uint32_t s=constant_buffer_unknown(65);
  for(uint32_t i=0;i<8;++i) { STATE(s); buffer[i]=(uint8_t)s; s+=1; }
  BUFFER(buffer); constant_buffer_use(buffer);
}
F void ce_reentry(uint32_t count) {
  uint8_t buffer[4]; uint32_t s=65;
  do {
    STATE(s); buffer[0]=(uint8_t)s; buffer[1]=(uint8_t)(s+1);
    buffer[2]=(uint8_t)(s+2); buffer[3]=0; s+=1;
    BUFFER(buffer);
  } while(--count);
  constant_buffer_use(buffer);
}
F void ce_volatile(void) {
  volatile uint8_t buffer[8];
  for(uint32_t i=0;i<8;++i) buffer[i]=(uint8_t)(65+i);
  constant_buffer_observer=buffer[1];
}
'''


def source(item, mode):
    name = f"ce_{mode}_{item['seed']:03d}"
    length, r = len(item["keys"]), item["rotate"]
    update = f"(((s >> {r}) | (s << {32-r})) * {item['multiplier']}u + {item['add']}u + KEY)"
    lines = [f"F void {name}(void) {{", f"uint8_t buffer[{length}];", f"uint32_t s={item['initial']}u;"]
    if mode == "loop":
        lines += ["static const uint32_t keys[]={" + ",".join(str(v)+"u" for v in item["keys"]) + "};",
                  "static const uint8_t cipher[]={" + ",".join(map(str,item["cipher"])) + "};",
                  f"for (uint32_t i=0;i<{length};++i) {{", "s="+update.replace("KEY", "keys[i]")+"; STATE(s);",
                  f"buffer[i]=(uint8_t)((s >> {item['shift']}) ^ cipher[i]);", "}"]
    else:
        for index, (key, cipher) in enumerate(zip(item["keys"], item["cipher"])):
            lines += ["s="+update.replace("KEY", str(key)+"u")+"; STATE(s);",
                      f"buffer[{index}]=(uint8_t)((s >> {item['shift']}) ^ {cipher});"]
    lines += ["BUFFER(buffer);", "constant_buffer_use(buffer);", "}"]
    return name, "\n".join(lines)


def generate(out, seeds):
    out.mkdir(parents=True, exist_ok=True)
    code, manifest = [HEADER], []
    for seed in seeds:
        item = fixture(seed)
        for mode in ("loop", "unrolled"):
            name, function = source(item, mode)
            code.append(function)
            manifest.append(dict(item, name=name, mode=mode))
    (out / "constant_buffers.c").write_text("\n\n".join(code))
    (out / "constant_buffers.json").write_text(json.dumps(manifest, indent=2) + "\n")
    extra, regions = [], []
    def body(seed, suffix):
        item = fixture(seed)
        _, code = source(item, 'loop')
        code = code[code.index('{')+1:code.rindex('}')]
        code = re.sub(r'\b(buffer|s|keys|cipher)\b', lambda m: m[0] + suffix, code)
        return code, bytes(item['plaintext']).decode('ascii').rstrip('\0')
    for seed in (20, 21):
        code, expected = body(seed, '')
        name = f'ce_after_call_{seed}'
        extra.append(f'F void {name}(void) {{ constant_buffer_unknown(3); {code} }}')
        regions.append(dict(name=name, texts=[expected], regions=1))
    first, a = body(22, 'a'); second, b = body(23, 'b')
    extra.append(f'F void ce_branch_regions(uint32_t choose) {{ if(choose) {{ {first} }} else {{ {second} }} }}')
    regions.append(dict(name='ce_branch_regions', texts=[a,b], regions=2, branch=True))
    first, a = body(24, 'a'); second, b = body(25, 'b')
    extra.append(f'F void ce_two_regions(void) {{ {first} {second} BUFFER(buffera); constant_buffer_use(buffera); }}')
    regions.append(dict(name='ce_two_regions', texts=[a,b], regions=2))
    code, _ = body(26, '')
    index = code.index('uint32_t s=')
    code = code[:index] + 'constant_buffer_capture(buffer);\n' + code[index:]
    extra.append(f'F void ce_previously_escaped(void) {{ {code} }}')
    path = out / 'constant_buffers.c'
    path.write_text(path.read_text() + '\n\n' + '\n\n'.join(extra))
    (out / 'constant_regions.json').write_text(json.dumps(regions, indent=2) + '\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seeds", type=int, default=16)
    args = parser.parse_args()
    generate(args.out, range(args.seeds))
