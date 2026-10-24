"""Random reversible encodings, with targets computed independently in Python."""
from pathlib import Path
import json,random


def generate(out):
    out.mkdir(exist_ok=True)
    lines=['#include <stdint.h>','#ifdef _WIN32',
           '#define F __declspec(dllexport) __attribute__((noinline,used))','#else',
           '#define F __attribute__((noinline,used,visibility("default")))','#endif',
           '#define KEEP(x) __asm__ volatile("" : "+r"(x))']
    cases=[]
    for width in (8,16,32,64):
        for seed in range(4):
            rng=random.Random(width*100+seed);mask=(1<<width)-1
            start=rng.getrandbits(width);m=rng.getrandbits(width)|1
            a,b,c=(rng.getrandbits(width) for _ in range(3));r=rng.randrange(1,width)
            value=((start*m+a)&mask)^b
            value=((value<<r)|(value>>(width-r)))&mask
            target=(value-c)&mask
            name=f'pi_{width}_{seed}'
            arithmetic=32 if width<=32 else 64
            lines.append(f'F uint32_t {name}(uint{width}_t x) {{ uint{width}_t t=x;'
                         f't=(uint{width}_t)((uint{arithmetic}_t)t*(uint{arithmetic}_t){m}ULL);KEEP(t);'
                         f't=(uint{width}_t)(t+{a}ULL);KEEP(t);'
                         f't=(uint{width}_t)(t^{b}ULL);KEEP(t);'
                         f't=(uint{width}_t)((t<<{r})|(t>>{width-r}));KEEP(t);'
                         f't=(uint{width}_t)(t-{c}ULL);KEEP(t);return t=={target}ULL; }}')
            cases.append({'name':name,'width':width,'value':start})
    (out/'preimages.c').write_text('\n'.join(lines)+'\n')
    (out/'preimages.json').write_text(json.dumps(cases,indent=2)+'\n')


if __name__=='__main__':generate(Path(__file__).resolve().parent/'build')
