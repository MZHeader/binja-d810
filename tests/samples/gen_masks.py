"""Deterministic random mask partitions; expected results are recorded independently of the IL."""
import json
import random
from pathlib import Path


def generate(out):
    out.mkdir(exist_ok=True)
    lines=['#include <stdint.h>',
           '#ifdef _WIN32', '#define F __declspec(dllexport) __attribute__((noinline,used))',
           '#else', '#define F __attribute__((noinline,used,visibility("default")))', '#endif',
           '#define KEEP(x) __asm__ volatile("" : "+r"(x))']
    cases=[]
    for width in (8,16,32,64):
        for seed in range(8):
            rng=random.Random(width*100+seed)
            mask=rng.randrange(1,(1<<width)-1)
            keep=((1<<width)-1)^mask
            name=f'mask_{width}_{seed}'
            lines.append(f'F uint{width}_t {name}(uint{width}_t x,uint{width}_t y) {{'
                         f' uint{width}_t a=x & {keep}ULL; KEEP(a);'
                         f' uint{width}_t b=x ^ y; KEEP(b);'
                         f' uint{width}_t c=b & {mask}ULL; KEEP(c); return a | c; }}')
            cases.append({'name':name,'width':width,'expected':f'x ^ (y & {mask})'})
    (out/'masks.c').write_text('\n'.join(lines)+'\n')
    (out/'masks.json').write_text(json.dumps(cases,indent=2)+'\n')


if __name__=='__main__':
    generate(Path(__file__).resolve().parent/'build')
