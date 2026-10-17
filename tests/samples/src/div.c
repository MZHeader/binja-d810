// v3.2 fixtures: division in opaque predicates (x-tunnel.bin's 2 / (x * x + 1) == y * y + 3) and
// divisions that may trap. Barriers keep the compilers from seeing through values, so the divisions
// are emitted as division instructions (x86 div/idiv, ARM64 udiv/sdiv).
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#endif

// The predicate's inputs: globals the compiler cannot see through.
EXPORT uint32_t div_x, div_y;

// x * x + 1 is never 0 (squares are 0 or 1 modulo 4), so 2 / (x * x + 1) is 0, 1 or 2; y * y + 3 is
// none of those (squares are 0, 1 or 4 modulo 8). Always a + 1.
EXPORT uint32_t div_opaque(uint32_t a) {
    uint32_t x = div_x, y = div_y; B(x); B(y);
    uint32_t d = x * x + 1; B(d);
    uint32_t q = 2u / d; B(q);
    uint32_t r = y * y + 3; B(r);
    if (q == r) return a ^ 0x5a;
    return a + 1;
}

// (x * x + 1) % m with m = 4 (a register, not an immediate): a square plus one is 1 or 2 modulo 4.
// Always a + 1.
EXPORT uint32_t rem_opaque(uint32_t a) {
    uint32_t x = div_x; B(x);
    uint32_t m = 4; B(m);
    uint32_t d = x * x + 1; B(d);
    uint32_t r = d % m; B(r);
    if (r == 0) return a ^ 0x5a;
    return a + 1;
}

// Divisions that may trap, whose only use is a condition the plugin folds (q * (q + 1) is even): b
// may be 0, and x | 1, never 0, is -1 for x = -1 (MIN / -1 traps on x86). Both divisions stay.
EXPORT uint32_t div_trap(uint32_t a, uint32_t b) {
    uint32_t x = div_x; B(x);
    uint32_t q = a / b;
    int32_t d = (int32_t)(x | 1); B(d);
    int32_t s = (int32_t)a / d;
    uint32_t u = (uint32_t)s;
    if (((q * (q + 1)) & 1) || ((u * (u + 1)) & 1)) return a ^ 0x5a;
    return a + 1;
}

// The quotient compared with y * y + 3 computed on two paths and merged: the merged value is not
// constant, but the comparison is false on each path. Always a + 1.
EXPORT uint32_t div_merge(uint32_t a, uint32_t c) {
    uint32_t x = div_x, y = div_y; B(x); B(y);
    uint32_t r;
    if (c & 1) { r = y * y + 3; B(r); }
    else { uint32_t z = y + c; B(z); r = z * z + 3; B(r); }
    uint32_t d = x * x + 1; B(d);
    uint32_t q = 2u / d; B(q);
    if (q == r) return a ^ 0x5a;
    return a + 1;
}

// A division that cannot trap, kept: the MBA around the quotient is a + q, and q stays a division.
EXPORT uint32_t div_keep(uint32_t a) {
    uint32_t x = div_x; B(x);
    uint32_t d = x * x + 1; B(d);
    uint32_t q = a / d;
    return (q ^ a) + 2 * (q & a);
}
