// v3 fixtures: opaque predicates (conditions that never change, hidden from the compiler by empty
// asm barriers) and ordinary conditions that must stay. The dead branches return x ^ 0x5a.
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#endif

// x * (x + 1) is even.
EXPORT uint32_t opaque_parity(uint32_t x, uint32_t y) {
    uint32_t t = x * (x + 1); B(t);
    if ((t & 1) == 0) return x + y;
    return x ^ 0x5a;
}

// d810 PredSetnz: x | 0x10 always has bit 4 set, 0x0f never.
EXPORT uint32_t opaque_setnz(uint32_t x, uint32_t y) {
    uint32_t t = x | 0x10; B(t);
    if (t != 0x0f) return x + y;
    return x ^ 0x5a;
}

// An MBA-encoded equality: (x ^ y) + 2 * (x & y) == x + y.
EXPORT uint32_t opaque_mba(uint32_t x, uint32_t y) {
    uint32_t a = (x ^ y) + 2 * (x & y); B(a);
    uint32_t b = x + y; B(b);
    if (a == b) return x + y;
    return x ^ 0x5a;
}

// Ordinary: depends on x.
EXPORT uint32_t plain_condition(uint32_t x, uint32_t y) {
    uint32_t t = x & 1; B(t);
    if (t == 0) return x + y;
    return x ^ 0x5a;
}

// OLLVM's form: signed remainder of a product of consecutive integers.
EXPORT uint32_t mod2(uint32_t x, uint32_t y) {
    int32_t t = (int32_t)x * ((int32_t)x - 1); B(t);
    if (t % 2 == 0) return x + y;
    return x ^ 0x5a;
}

// A predicate over memory: whatever happens to the condition, the loads stay.
EXPORT uint32_t load_condition(uint32_t *p, uint32_t y) {
    if ((*p * (*p + 1)) & 1) return y ^ 0x5a;
    return y + 1;
}

// The mirrored form: the predicate guards the dead branch.
EXPORT uint32_t mirrored(uint32_t x, uint32_t y) {
    uint32_t t = x * (x + 1); B(t);
    if ((t & 1) != 0) return x ^ 0x5a;
    return x + y;
}

// Loop conditions over bitwise operations and products: they depend on the inputs and stay. HLIL
// gives a loop's condition size 0 (a boolean) over 4-byte operands (seen on real code, and on every build here).
EXPORT uint32_t loop_bitwise(uint32_t x, uint32_t y) {
    uint32_t acc = 0;
    for (uint32_t i = 0; ((i ^ x) & 7) != y; i++) {
        acc += i;
        if (i > 64) break;
    }
    return acc;
}

EXPORT uint32_t loop_masked(uint32_t x, uint32_t y) {
    uint32_t acc = 0;
    for (uint32_t i = 0; (i & 0xff) < y; i++) acc += i ^ x;
    return acc;
}

EXPORT uint32_t loop_product(uint32_t x, uint32_t y) {
    uint32_t acc = y;
    for (uint32_t i = 0; ((i * x) & 3) != 2; i++) {
        acc ^= i;
        if (i > 64) break;
    }
    return acc;
}
