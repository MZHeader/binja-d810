// Empty asm barriers keep scratch traffic in the binary; they emit no runtime operations.
#include <stdint.h>
#ifdef _WIN32
#define F __declspec(dllexport) __attribute__((noinline, used))
#else
#define F __attribute__((noinline, used, visibility("default")))
#endif
#define M(value) __asm__ volatile("" : "+m"(value))
#define P(value) __asm__ volatile("" : "+r"(value) : : "memory")

void *volatile scratch_capture;
F void scratch_sink(void *p) { scratch_capture = p; }

F uint32_t scratch_dead(uint32_t x) {
    uint32_t slot = x + 3; M(slot);
    slot = x ^ 7; M(slot);
    return x + 11;
}

F uint32_t scratch_roundtrip(uint32_t x) {
    uint32_t slot = x;
    uint32_t *p = &slot; P(p);
    *p = x ^ 0x31;
    uint32_t result = *p;
    slot = 23; M(slot);
    return result;
}

F uint32_t scratch_encoded_address(uint32_t x) {
    uint32_t slot;
    uintptr_t address = (uintptr_t)&slot;
    uintptr_t a = address ^ 0x3du; P(a);
    uintptr_t b = address & 0x3du; P(b);
    uintptr_t p = a + 2 * b - 0x3du; P(p);
    *(uint32_t *)p = x ^ 0x51u;
    uint32_t result = *(uint32_t *)p;
    slot = 29; M(slot);
    return result;
}

F uint32_t scratch_read(uint32_t x) {
    uint32_t slot = x * 9; M(slot);
    return slot + 1;
}

F uint32_t scratch_branch(uint32_t x) {
    uint32_t slot = x; M(slot);
    if (x & 1) { slot = 31; M(slot); }
    else { slot = 47; M(slot); }
    slot = 3; M(slot);
    return x ^ 19;
}

F uint32_t scratch_partial(uint32_t x) {
    uint32_t slot = x;
    uint8_t *p = (uint8_t *)&slot; P(p);
    p[0] = 0x6b; M(slot);
    return slot;
}

F uint32_t scratch_loop(uint32_t x) {
    uint32_t slot;
    for (uint32_t i = 0; i != 5; ++i) { slot = x + i; M(slot); }
    return x ^ 41;
}

F uint32_t scratch_escape(uint32_t x) {
    uint32_t slot = x;
    scratch_sink(&slot);
    return slot ^ x;
}

F uint32_t scratch_unknown(uint32_t x, void (*f)(uint32_t)) {
    uint32_t slot = x; M(slot);
    f(x);
    M(slot);
    return slot;
}
