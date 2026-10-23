#include <stdint.h>
#ifdef _WIN32
#define F __declspec(dllexport) __attribute__((noinline,used))
#define D __declspec(dllexport)
#else
#define F __attribute__((noinline,used,visibility("default")))
#define D __attribute__((visibility("default")))
#endif
static uint32_t seed=0x1234;
static uint32_t atomic_seed=0x5623;
static uint32_t atomic_add_seed=0x9876;
static uint32_t bad_seed=0x1234;
static uint32_t escaped_seed=0x1234;
static uint32_t zero_seed;
static uint32_t large_seed=17;
D uint32_t exported_seed=0x1234;
uint32_t *volatile saved_seed;
F void mv_update(uint32_t guard) { if(guard&1) seed^=0x40; if(guard&2) seed+=0x80000000u; }
F uint32_t mv_invariant(void) { return seed&0x3f; }
F uint32_t mv_varying(void) { return seed; }
F uint32_t mv_two_reads(void) { uint32_t a=seed; __asm__ volatile("" ::: "memory"); return a^seed; }
F void mv_atomic_update(uint32_t guard) { if(guard) __atomic_fetch_xor(&atomic_seed,0x40,__ATOMIC_RELAXED); }
F uint32_t mv_atomic_invariant(void) { return atomic_seed&0x3f; }
F void mv_atomic_add(uint32_t guard) { if(guard) __atomic_fetch_add(&atomic_add_seed,0x80000000u,__ATOMIC_RELAXED); }
F uint32_t mv_atomic_add_invariant(void) { return atomic_add_seed&0x7fffffff; }
F void mv_unknown_update(uint32_t value) { bad_seed=value; }
F uint32_t mv_unknown_read(void) { return bad_seed&0x3f; }
F void mv_escape(void) { saved_seed=&escaped_seed; }
F uint32_t mv_escaped_read(void) { return escaped_seed&0x3f; }
F uint32_t mv_exported_read(void) { return exported_seed&0x3f; }
F void mv_exported_update(uint32_t guard) { if(guard) exported_seed^=0x4000; }
F uint32_t mv_exported_partial(void) { return ((uint8_t*)&exported_seed)[1]&0x3f; }
F void mv_zero_update(uint32_t guard) { __asm__ volatile("" : "+m"(zero_seed)); if(guard) zero_seed^=0x40; }
F uint32_t mv_zero_invariant(void) { return zero_seed&0x3f; }
F void mv_large_update(uint32_t guard) { if(guard) large_seed+=1; }
F uint32_t mv_large_read(void) { return large_seed&0x3f; }
