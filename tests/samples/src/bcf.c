// v3.1 fixtures: OLLVM-style bogus control flow (an obfuscated export) and the shapes around it.
// Intermediates pass through empty asm barriers so the compiler keeps the structure.
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#endif

// OLLVM's opaque-predicate inputs: globals the compiler cannot see through.
EXPORT uint32_t bcf_x, bcf_y;

EXPORT void bcf_sink(uint32_t v) { B(v); bcf_y = v; }

// One masked-constant encoding step: (t & m) | (~t & ~m), which is t ^ ~m. Each step reads t
// twice, so the chain is small as shared values and doubles in size as a tree at every step.
#define STEP(t, m) do { uint32_t u_ = (t) & (m); B(u_); uint32_t v_ = ~(t) & ~(m); B(v_); (t) = u_ | v_; B(t); } while (0)

// x * (x - 1) is even; its low bit passes through 12 encoding steps (K undoes them). Always a + 1.
EXPORT uint32_t bcf_chain(uint32_t a) {
    uint32_t x = bcf_x, y = bcf_y; B(x); B(y);
    uint32_t t = x * (x - 1); B(t);
    STEP(t, 0x628ed16bu);
    STEP(t, 0x9d712e94u);
    STEP(t, 0x75deac2du);
    STEP(t, 0x8a2153d2u);
    STEP(t, 0xc0aace2eu);
    STEP(t, 0x3f5531d1u);
    STEP(t, 0x90414451u);
    STEP(t, 0x653ab10eu);
    STEP(t, 0x188dec13u);
    STEP(t, 0xe77213ecu);
    STEP(t, 0x803573ebu);
    STEP(t, 0x2472629du);
    if (((t ^ 0x513ce429u) & 1) == 0 || y < 10) return a + 1;
    return a ^ 0x5a;
}

// The predicate's value computed on two paths and merged: each arm's k is 0, so the merged k is 0.
EXPORT uint32_t bcf_merge(uint32_t a, uint32_t c) {
    uint32_t x = bcf_x; B(x);
    uint32_t k;
    if (c & 1) { uint32_t t = x * (x - 1); B(t); k = t & 1; B(k); }
    else { uint32_t t = x * (x + 1); B(t); k = t & 1; B(k); }
    if (k == 0) return a + 1;
    return a ^ 0x5a;
}

// A value proven constant (1) and used by arithmetic and a call: both uses should read 1.
EXPORT uint32_t const_use(uint32_t a) {
    uint32_t x = bcf_x; B(x);
    uint32_t s = x | 0x10; B(s);
    uint32_t k = s != 0x0f;
    bcf_sink(k);
    return a + k * 3;
}

// glibc's pointer alignment: q is used twice; p - q is -8 | p, which must not
// make the arithmetic line mixed.
EXPORT uint32_t align_gate(uint32_t p, uint32_t n) {
    uint32_t q = (p + 8) & ~7u; B(q);
    bcf_sink(q);
    return (n + p - q - 8) >> 3;
}

// A proven constant read after a merge: SSA form numbers instructions differently (phi nodes), so
// a read must be found through its own instruction, never by index.
EXPORT uint32_t const_after_merge(uint32_t a, uint32_t c) {
    uint32_t x = bcf_x; B(x);
    uint32_t s = x | 0x10; B(s);
    uint32_t k = s != 0x0f;
    uint32_t m = (c & 1) ? a * 3 : a + 7; B(m);
    bcf_sink(m);
    bcf_sink(k);
    return m;
}

// glibc's pointer alignment, again: d = p - q is its own instruction, used once by arithmetic that
// HLIL shows as one statement with it. d is -8 | p, which must not make that statement mixed.
EXPORT uint32_t align_gate2(uint32_t p, uint32_t n) {
    uint32_t q = (p + 8) & ~7u; B(q);
    bcf_sink(q);
    uint32_t d = p - q; B(d);
    return (n + d - 8) >> 3;
}

// The same inside a larger statement (v3.2): p - q sits beside a call's result, so HLIL's root is
// p - q alone and the statement is bcf_id(n) + (p - q). -8 | p must not make it mixed.
EXPORT uint32_t bcf_id(uint32_t v) { B(v); return v; }

EXPORT uint32_t align_call(uint32_t p, uint32_t n) {
    uint32_t q = (p + 8) & ~7u; B(q);
    bcf_sink(q);
    return bcf_id(n) + (p - q);
}

// v3.5: a read unused in the original code stays when a rewrite in the same function makes the pass
// remove what the rewrite left dead. The barrier reads v, which Binary Ninja does not see: a load nothing uses.
EXPORT uint32_t bcf_v;

EXPORT uint32_t dead_read(uint32_t a, uint32_t b) {
    uint32_t v = bcf_v; B(v);
    uint32_t t = a ^ b; B(t);
    uint32_t u = a & b; B(u);
    return t + 2 * u;
}

// v3.5: a read through a computed address that a fold leaves unused stays: such a read may fault.
EXPORT uint32_t bcf_table[4];

EXPORT uint32_t computed_read(uint32_t a, uint32_t c) {
    uint32_t x = bcf_table[a & 3]; B(x);
    uint32_t t = x * (x - 1); B(t);
    if ((t & 1) == 0) return c + 1;
    return c ^ 0x5a;
}
