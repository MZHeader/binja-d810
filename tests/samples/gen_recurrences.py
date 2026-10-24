"""Small, exhaustive-domain recurrence fixtures with independently computed preimages."""
from pathlib import Path
import json,random


def generate(out):
    out.mkdir(exist_ok=True)
    lines=['#include <stdint.h>','#ifdef _WIN32',
           '#define F __declspec(dllexport) __attribute__((noinline,used))','#else',
           '#define F __attribute__((noinline,used,visibility("default")))','#endif',
           '#define KEEP(x) __asm__ volatile("" : "+r"(x))']
    cases=[]
    for width in (32,64):
        for seed in range(3):
            rng=random.Random(width*100+seed);mask=(1<<width)-1
            start=rng.getrandbits(width);m=rng.getrandbits(width)|1;a=rng.getrandbits(width)
            def model(x):
                state=start
                for i in range(4):state=((state^((x>>(2*i))&3))*m+a)&mask
                return state
            outputs=[model(x) for x in range(256)]
            target=outputs[rng.randrange(256)]
            if seed==2:
                target=0
                while target in outputs:target+=1
            matches=[x for x,value in enumerate(outputs) if value==target]
            name=f'rp_{width}_{seed}'
            lines.append(f'F uint32_t {name}(uint32_t x,uint32_t y,uint32_t z) {{'
                         f'uint{width}_t s={start}ULL;uint32_t i=0;'
                         f'do {{s=(s^(uint{width}_t)((x>>(2*i))&3))*(uint{width}_t){m}ULL+(uint{width}_t){a}ULL;'
                         f'KEEP(s);i++;KEEP(i);}}while(i!=4);return s=={target}ULL;}}')
            cases.append({'name':name,'width':width,'matches':matches})
    (out/'recurrences.c').write_text('\n'.join(lines)+'\n')
    (out/'recurrences.json').write_text(json.dumps(cases,indent=2)+'\n')


if __name__=='__main__':generate(Path(__file__).resolve().parent/'build')
