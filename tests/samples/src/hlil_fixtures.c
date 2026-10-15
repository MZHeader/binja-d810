// HLIL-pass fixtures (spec section 5.5): MBA in branches, joins and loops; a reassigned input;
// effects embedded in HLIL expressions; conditional evaluation.
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#endif

EXPORT uint32_t hl_source(uint32_t x) { __asm__ volatile("" : "+r"(x)); return x * 2654435761u; }

EXPORT uint32_t cf_branch(uint32_t x, uint32_t y, uint32_t c) {
    uint32_t r;
    if (c & 1) {
        uint32_t t0 = x ^ y; B(t0);
        uint32_t t1 = x & y; B(t1);
        r = t0 + 2u * t1;
    } else {
        uint32_t t0 = x | y; B(t0);
        uint32_t t1 = x & y; B(t1);
        r = t0 - t1;
    }
    return r + c;
}
EXPORT uint32_t cf_loop(uint32_t x, uint32_t y, uint32_t n) {
    uint32_t acc = y;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t t0 = acc ^ x; B(t0);
        uint32_t t1 = acc & x; B(t1);
        acc = t0 + 2u * t1 + i;
    }
    return acc;
}
EXPORT uint32_t cf_reassign(uint32_t x, uint32_t y) {
    uint32_t t0 = x ^ y; B(t0);
    uint32_t t1 = x & y; B(t1);
    x = x * 3u + 1u; B(x);
    return t0 + 2u * t1 + x;
}
EXPORT uint32_t hl_call_inline(uint32_t x, uint32_t y) {
    uint32_t t1 = x & y; B(t1);
    return (hl_source(x) ^ y) + 2u * t1;
}
volatile uint32_t hl_global;
EXPORT uint32_t hl_volatile(uint32_t y) {
    uint32_t t1 = hl_global & y; B(t1);
    return (hl_global ^ y) + 2u * t1;
}
EXPORT uint32_t hl_select(uint32_t x, uint32_t y, uint32_t c) {
    uint32_t t0 = x ^ y; B(t0);
    uint32_t t1 = x & y; B(t1);
    return c ? t0 + 2u * t1 : y;
}
