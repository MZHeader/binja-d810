#include <stdint.h>
#ifdef _WIN32
#define F __declspec(dllexport) __attribute__((noinline,used))
#else
#define F __attribute__((noinline,used,visibility("default")))
#endif
#define B(x) __asm__ volatile("" : "+r"(x))
#define FLAGS \
    uint32_t d=x-y, n=d>>31, z=(d==0), v=((x^y)&(x^d))>>31, c=(x<y); \
    B(n); B(z); B(v); B(c)
#define CONDITION(name, expression) \
    F uint32_t flag_##name(uint32_t x,uint32_t y) { FLAGS; return (expression); }
CONDITION(eq, z)
CONDITION(ne, !z)
CONDITION(slt, n!=v)
CONDITION(sle, z || n!=v)
CONDITION(sgt, !z && n==v)
CONDITION(sge, n==v)
CONDITION(ult, c)
CONDITION(ule, c || z)
CONDITION(ugt, !c && !z)
CONDITION(uge, !c)
F uint32_t flag_signed_chain(int32_t x,int32_t y,int32_t z) { return x<=y && x>=z; }
F uint32_t flag_fixed_chain(int32_t x,int32_t y) { return x<=y && x>=17; }
F uint32_t flag_unsigned_chain(uint32_t x,uint32_t y,uint32_t z) { return x<y || x>z; }
F uint32_t flag_nested_chain(uint32_t x,uint32_t y,uint32_t z) {
    return (x<y || x>z) && (y!=z || x==7);
}
volatile uint32_t flag_output;
F uint32_t flag_shared(uint32_t x,uint32_t y) {
    FLAGS;
    flag_output=n;
    return z || n!=v;
}
