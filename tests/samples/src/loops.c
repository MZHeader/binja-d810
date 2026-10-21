// v4.3 fixtures: junk loops. A loop that only steps its own counter until it meets a value, with nothing reading
// the counter afterwards, does nothing but spend time; an odd step meets every value of the counter's width, so
// the loop ends. The empty asm keeps the compiler from deleting a loop it cannot see through (the binary keeps no
// trace of it). Every constant is made up for these fixtures.
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#define EXPORT_DATA __declspec(dllexport)
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#define EXPORT_DATA __attribute__((visibility("default")))
#endif

EXPORT_DATA volatile int marked;
EXPORT int mark(int k) { B(k); marked = k; return k; }

EXPORT int junk_loop(int x) {
    mark(1);
    uint32_t i = (uint32_t)x;
    do { i += 0x2f1bu; B(i); } while (i != 0x5d3a71c4u);
    return mark(2);
}

// Controls: the counter is read after the loop, which only ends on its low byte, so the value it ends on is not
// known; an even step may never meet the value.
EXPORT int junk_read(int x) {
    mark(3);
    uint32_t i = (uint32_t)x;
    do { i += 0x2f1bu; B(i); } while ((uint8_t)i != 0xc4u);
    return mark(4) + (int)i;
}

EXPORT int junk_even(int x) {
    mark(5);
    uint32_t i = (uint32_t)x;
    do { i += 0x2f1au; B(i); } while (i != 0x5d3a71c4u);
    return mark(6);
}

// The exit test runs only on the iterations an opaque copy of the counter lets it; the body holds a loop of its
// own. An odd step alone does not end either.
EXPORT int junk_skip(int x) {
    mark(7);
    uint32_t i = (uint32_t)x;
    for (;;) {
        i += 0x2f1bu;
        uint32_t j = i;
        B(j);
        if (!(j & 0x100u) && i == 0x5d3a70c4u) break;
        B(i);
    }
    return mark(8);
}

EXPORT int junk_inner(int x) {
    mark(9);
    uint32_t i = (uint32_t)x;
    do {
        i += 0x2f1bu;
        uint32_t k = i;
        do { k += 0x2u; B(k); } while (k != 0x11u);
    } while (i != 0x5d3a71c4u);
    return mark(10);
}
