// HLIL-pass fixtures: MBA in branches, joins and loops; a reassigned input;
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

// Lift work (v2.1): each step reads both values twice, so the fully inlined tree grows fourfold per
// step (the handlers of a Tigress VM). A lift must cut deep definitions without exploring that tree.
#define DAG_STEP a = (a ^ b) + 2 * (a & b); B(a); b = (b | a) - (b & ~a); B(b);
EXPORT uint32_t lift_dag(uint32_t x, uint32_t y) {
    uint32_t a = x, b = y;
    DAG_STEP DAG_STEP DAG_STEP DAG_STEP DAG_STEP DAG_STEP DAG_STEP DAG_STEP
    DAG_STEP DAG_STEP DAG_STEP DAG_STEP DAG_STEP DAG_STEP DAG_STEP DAG_STEP
    return a ^ b;
}

// Comparison results used as values (x86 setcc, ARM64 cset) inside MBA:
// b is x & 1 (bool_ne) or its complement (bool_eq), so both return x + y. In bool_wide, b is not
// x & 3, and nothing may treat it so.
EXPORT uint32_t bool_ne(uint32_t x, uint32_t y) {
    uint32_t t = x & 1; B(t);
    uint32_t b = t != 0; B(b);
    return ((x | b) ^ y) + 2 * ((x | b) & y);
}
EXPORT uint32_t bool_eq(uint32_t x, uint32_t y) {
    uint32_t t = x & 1; B(t);
    uint32_t b = t == 0; B(b);
    return ((x ^ y) + 2 * (x & y)) + b + (x & 1) - 1;
}
EXPORT uint32_t bool_wide(uint32_t x, uint32_t y) {
    uint32_t t = x & 3; B(t);
    uint32_t b = t != 0; B(b);
    return ((x | b) ^ y) + 2 * ((x | b) & y);
}

// A value used twice and kept (a Tigress VM's handlers): a is stored and also combined, so a rewrite
// of the return over a's definition would copy it; over a itself, (a ^ z) + 2 * (a & z) is a + z.
EXPORT uint32_t shared_value(uint32_t x, uint32_t y, uint32_t z, uint32_t *out) {
    uint32_t a = (x * y) - (x | y) + (x & 7); B(a);
    *out = a;
    return (a ^ z) + 2 * (a & z);
}

// OLLVM: an arithmetic definition and a bitwise use, each unmixed; merging them made one
// mixed expression. Over a itself the use is a ^ 0x00ff00ff.
EXPORT uint32_t split_mix(uint32_t x, uint32_t y) {
    uint32_t a = x * 7 + y; B(a);
    return (a & 0xff00ff00) | (~a & 0x00ff00ff);
}

// Three-address MBA: single-use temporaries, each unmixed in three-address MLIL, that HLIL shows as one
// mixed statement. A smaller mixed result, (x + y) | z, may replace it.
EXPORT uint32_t mixed_chain(uint32_t x, uint32_t y, uint32_t z) {
    uint32_t a = x ^ y; B(a);
    uint32_t b = x & y; B(b);
    return (a + 2 * b) | z;
}
