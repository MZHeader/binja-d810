// v4.3 fixtures: OLLVM-style instruction substitution (bitwise only), ordinary bitwise code that must
// stay, and the classic number-theoretic predicates. Every intermediate passes an empty asm barrier,
// so the compiler cannot simplify across it. The dead branches xor with 0x5a.
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#endif

// x | y as (x & y) | (x ^ y).
EXPORT uint32_t sub_or(uint32_t x, uint32_t y) {
    uint32_t a = x & y; B(a);
    uint32_t b = x ^ y; B(b);
    return a | b;
}

// x & y as (x ^ ~y) & x.
EXPORT uint32_t sub_and(uint32_t x, uint32_t y) {
    uint32_t a = ~y; B(a);
    uint32_t b = x ^ a; B(b);
    return b & x;
}

// x ^ y as (~x & y) | (x & ~y).
EXPORT uint32_t sub_xor(uint32_t x, uint32_t y) {
    uint32_t a = ~x & y; B(a);
    uint32_t b = x & ~y; B(b);
    return a | b;
}

// One bit: (x ^ (y ^ 1)) & x & 1 is x & y & 1.
EXPORT uint32_t sub_bool(uint32_t x, uint32_t y) {
    uint32_t a = y ^ 1; B(a);
    uint32_t b = x ^ a; B(b);
    return b & x & 1;
}

// Constants that cancel: (x | 0xf0) & 0x0f is x & 0x0f.
EXPORT uint32_t sub_const(uint32_t x, uint32_t y) {
    uint32_t a = x | 0xf0; B(a);
    return a & 0x0f;
}

// x ^ y ^ x is y.
EXPORT uint32_t xor_cancel(uint32_t x, uint32_t y) {
    uint32_t a = x ^ y; B(a);
    return a ^ x;
}

// Ordinary bitwise code: a mask, a combination, a byte shuffle, a bit merge.
EXPORT uint32_t plain_mask(uint32_t x, uint32_t y) {
    B(x);
    return x & 0xff;
}

EXPORT uint32_t plain_or(uint32_t x, uint32_t y) {
    B(x);
    return x | y;
}

EXPORT uint32_t mix(uint32_t x, uint32_t y) {
    uint32_t a = (x >> 8) & 0xff; B(a);
    uint32_t b = (x >> 16) & 0xff00; B(b);
    return a | b;
}

EXPORT uint32_t merge(uint32_t x, uint32_t y) {
    uint32_t a = x & 0xf0f0; B(a);
    uint32_t b = y & ~0xf0f0u; B(b);
    return a | b;
}

// 7 * y * y - 1 is never a square.
EXPORT uint32_t sq_pred(uint32_t x, uint32_t y) {
    uint32_t a = 7 * y * y - 1; B(a);
    uint32_t b = x * x; B(b);
    if (a == b) return x ^ 0x5a;
    return x + y;
}

// 2 / (y * y + 1) is 0, 1 or 2; x * x + 3 never is.
EXPORT uint32_t sq_div(uint32_t x, uint32_t y) {
    uint32_t a = 2 / (y * y + 1); B(a);
    uint32_t b = x * x + 3; B(b);
    if (a == b) return x ^ 0x5a;
    return x + y;
}

// The square read through a loop merge: i is x * x on entry and (x + k) * (x + k) after.
EXPORT uint32_t sq_merged(uint32_t x, uint32_t y, uint32_t n) {
    uint32_t i = x * x; B(i);
    uint32_t r = 0;
    for (uint32_t k = 0; k < n; k++) {
        uint32_t a = 7 * y * y - 1; B(a);
        if (a == i) r ^= 0x5a;
        r += k;
        uint32_t z = x + k;
        i = z * z; B(i);
    }
    return r;
}

// A near miss: 7 * y * y - 1 == x * x + 6 holds for y = 1, x = 0. It stays.
EXPORT uint32_t sq_near(uint32_t x, uint32_t y) {
    uint32_t a = 7 * y * y - 1; B(a);
    uint32_t b = x * x + 6; B(b);
    if (a == b) return x ^ 0x5a;
    return x + y;
}
