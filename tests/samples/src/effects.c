// Effect-policy fixtures (spec section 5.2): values materialized from calls and aliased or
// volatile reads, with an MBA over them.
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
#define EXPORT __attribute__((noinline, used, visibility("default")))

EXPORT uint32_t eff_source(uint32_t x) { __asm__ volatile("" : "+r"(x)); return x * 2654435761u; }
EXPORT void eff_sink(uint32_t *p) { __asm__ volatile("" : : "r"(p) : "memory"); }

EXPORT uint32_t eff_call_result(uint32_t x, uint32_t y) {
    uint32_t m = eff_source(x);
    uint32_t t0 = m ^ y; B(t0);
    uint32_t t1 = m & y; B(t1);
    return t0 + 2u * t1;
}
EXPORT uint32_t eff_aliased(uint32_t x, uint32_t y) {
    uint32_t a = x;
    eff_sink(&a);
    uint32_t t0 = a ^ y; B(t0);
    uint32_t t1 = a & y; B(t1);
    return t0 + 2u * t1;
}
volatile uint32_t eff_global;
EXPORT uint32_t eff_volatile(uint32_t y) {
    uint32_t t0 = eff_global ^ y; B(t0);
    uint32_t t1 = eff_global & y; B(t1);
    return t0 + 2u * t1;
}
