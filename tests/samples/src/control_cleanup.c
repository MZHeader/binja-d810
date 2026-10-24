#include <stdint.h>
#ifdef _WIN32
#define F __declspec(dllexport) __attribute__((noinline,used))
#else
#define F __attribute__((noinline,used,visibility("default")))
#endif
#define KEEP(x) __asm__ volatile("" : "+r"(x))
#define CASE(T,N) \
F uint32_t ce_equal_##N(uint32_t x,uint32_t y,uint32_t z) { \
 T a,b,t; if(z) { t=(T)x ^ (T)y;KEEP(t);a=t ^ (T)y;KEEP(a);b=(T)x+(T)y;KEEP(b); } \
 else { t=(T)x+(T)y;KEEP(t);a=t-(T)y;KEEP(a);b=(T)x+(T)y;KEEP(b); } \
 KEEP(a);KEEP(b);return (uint32_t)(a ^ b); }
CASE(uint8_t,8)
CASE(uint16_t,16)
CASE(uint32_t,32)
CASE(uint64_t,64)
F uint32_t ce_different(uint32_t x,uint32_t y,uint32_t z) {
 uint32_t a;if(z){a=x+1;KEEP(a);}else{a=y+1;KEEP(a);}KEEP(a);return a;
}
volatile uint32_t ce_observed;
F uint32_t ce_store(uint32_t x,uint32_t y,uint32_t z) {
 if(z){ce_observed=x;}else{ce_observed=y;}return x+y;
}
F uint32_t ce_guard_load(uint32_t x,uint32_t y,uint32_t z) {
 uint32_t a;if(ce_observed){a=x^y;KEEP(a);a^=y;}else{a=x+y;KEEP(a);a-=y;}return a;
}
F uint32_t dl_dead(uint32_t x,uint32_t y,uint32_t z) {
 uint32_t a=x,i=0;
 do {a=(a^y)*13+5;KEEP(a);i++;KEEP(i);} while(i!=6);
 return z+3;
}
F uint32_t dl_live(uint32_t x,uint32_t y,uint32_t z) {
 uint32_t a=x,i=0;
 do {a=(a^y)*13+5;KEEP(a);i++;KEEP(i);} while(i!=6);
 return a+z;
}
F uint32_t dl_store(uint32_t x,uint32_t y,uint32_t z) {
 uint32_t a=x,i=0;
 do {a=(a^y)*13+5;KEEP(a);ce_observed=a;i++;KEEP(i);} while(i!=6);
 return z+3;
}
