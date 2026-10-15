// Review Focus inputs: reassigned inputs, narrow types, loads.
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#endif

EXPORT uint32_t safety_reassign(uint32_t x, uint32_t y) {
    uint32_t t0 = x ^ y; B(t0);
    uint32_t t1 = x & y; B(t1);
    x = x * 3u + 1u; B(x);
    return t0 + 2u * t1 + x;
}
EXPORT uint8_t safety_u8(uint8_t x, uint8_t y) {
    uint8_t t0 = x ^ y; B(t0);
    uint8_t t1 = x & y; B(t1);
    return (uint8_t)(t0 + 2u * t1);
}
EXPORT uint32_t safety_load(const uint32_t *p, uint32_t y) {
    uint32_t m = *p;
    uint32_t t0 = m ^ y; B(t0);
    uint32_t t1 = m & y; B(t1);
    return t0 + 2u * t1;
}
