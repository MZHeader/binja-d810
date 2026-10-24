// Integer encodings of Boolean decisions, with compiler barriers retaining each step.
#include <stdint.h>
#ifdef _WIN32
#define F __declspec(dllexport) __attribute__((noinline,used))
#else
#define F __attribute__((noinline,used,visibility("default")))
#endif
#define KEEP(x) __asm__ volatile("" : "+r"(x))
#define INPUTS(T) T p=x<y,q=y!=z,r=x==z; KEEP(p);KEEP(q);KEEP(r)
#define CASES(T,N) \
F uint32_t bc_and_##N(uint32_t x,uint32_t y,uint32_t z) { \
 INPUTS(T); T a=q^1;KEEP(a);T b=p^a;KEEP(b);T c=b&p;KEEP(c);return (c&1)!=0; } \
F uint32_t bc_or_##N(uint32_t x,uint32_t y,uint32_t z) { \
 INPUTS(T); T a=p+q;KEEP(a);T b=p*q;KEEP(b);T c=a-b;KEEP(c);return c!=0; } \
F uint32_t bc_xor_##N(uint32_t x,uint32_t y,uint32_t z) { \
 INPUTS(T); T a=p+q;KEEP(a);T b=p*q;KEEP(b);T c=b+b;KEEP(c);T d=a-c;KEEP(d);return d!=0; } \
F uint32_t bc_select_##N(uint32_t x,uint32_t y,uint32_t z) { \
 INPUTS(T); T a=p*q;KEEP(a);T b=1-p;KEEP(b);T c=b*r;KEEP(c);T d=a+c;KEEP(d);return d!=0; }
CASES(uint8_t,8)
CASES(uint16_t,16)
CASES(uint32_t,32)
CASES(uint64_t,64)
volatile uint32_t bc_observed;
F uint32_t bc_shared(uint32_t x,uint32_t y,uint32_t z) {
 uint32_t p=x<y,q=y!=z;KEEP(p);KEEP(q);bc_observed=p;
 uint32_t a=q^1;KEEP(a);uint32_t b=p^a;KEEP(b);return (b&p&1)!=0;
}
#define LOOP_BODY \
 for(uint32_t i=0;i<4;i++) { \
  uint32_t p=x==i,q=y!=i;KEEP(p);KEEP(q); \
  uint32_t inv=q^1;KEEP(inv);mix=p^inv;KEEP(mix); \
  uint32_t out=mix&p;KEEP(out);if(out&1)result++;KEEP(i);KEEP(result); }
F uint32_t bc_dead_merge(uint32_t x,uint32_t y,uint32_t z) {
 uint32_t result=0,mix=z;LOOP_BODY return result;
}
F uint32_t bc_live_merge(uint32_t x,uint32_t y,uint32_t z) {
 uint32_t result=0,mix=z;LOOP_BODY return result+mix;
}
