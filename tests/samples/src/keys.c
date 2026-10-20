// v4.3 fixtures: constants encoded against key globals that no visible code writes (constant encryption, as
// Hikari, Pluto and O-MVLL emit it), and the globals D-810 must leave alone: an exported one, a written one,
// one whose address escapes, one holding an address (relocated), and settings that are only compared, plainly or
// after an operation with a constant. KEEP hands a key's address to an empty asm that may write memory: otherwise
// clang makes a never-written static constant at -O2. B() on the MBA form's halves stops clang from folding (n | c) + (n & c) back to n + c.
// Every key and constant is made up for these fixtures.
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
#define KEEP(p) __asm__ volatile("" : : "r"(p) : "memory")
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#define EXPORT_DATA __declspec(dllexport)
#define LOCAL EXPORT
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#define EXPORT_DATA __attribute__((visibility("default")))
#define LOCAL __attribute__((noinline, used, visibility("hidden")))
#endif

EXPORT_DATA volatile int marked;
EXPORT int mark(int k) { B(k); marked = k; return k; }
EXPORT_DATA void *sink_store;
EXPORT void sink_ptr(void *p) { B(p); sink_store = p; }

static uint32_t key32 = 0x5a17c3e9u;  // never written
static uint64_t key64 = 0x1f2e3d4c5b6a7988ull;  // never written

// ---- Constants encoded against a key ----

EXPORT uint32_t key_xor(uint32_t x) { KEEP(&key32); return x ^ (0x6b28d4fau - key32); }  // x ^ 0x11111111
EXPORT uint32_t key_mba(uint32_t x) {  // x + (0x1234 - key32), the subtraction in an MBA form
    KEEP(&key32);
    uint32_t n = -key32, a = n | 0x1234u, b = n & 0x1234u;
    B(a);
    B(b);
    return x + a + b;
}
EXPORT uint32_t key_shift(uint32_t x) { KEEP(&key32); return x << ((key32 + 0xa5e83c1cu) & 31); }  // x << 5
EXPORT uint64_t key_mul64(uint64_t x) { KEEP(&key64); return x * (key64 ^ 0x1f2e3d4c5b6a798bull); }  // x * 3

// ---- A flattened machine whose states are encoded against the key ----

EXPORT int key_states(int x) {
    KEEP(&key32);
    uint32_t s = 0x7a3c19e5u - key32;
    int r = 0;
    for (;;) {
        B(s);
        if (s == 0x7a3c19e5u - 0x5a17c3e9u) { r += mark(11); s = 0x1b4d8c27u - key32; }
        else if (s == 0x1b4d8c27u - 0x5a17c3e9u) { r += mark(12); s = (r > x ? 0x5e91a3f0u : 0x33c6d218u) - key32; }
        else if (s == 0x5e91a3f0u - 0x5a17c3e9u) { r += mark(13); s = 0x33c6d218u - key32; }
        else if (s == 0x33c6d218u - 0x5a17c3e9u) { return r + mark(14); }
        else { return -1; }
    }
}

// ---- Must stay as they are ----

EXPORT_DATA uint32_t key_exported = 0x5a17c3e9u;  // another module may write it
EXPORT uint32_t exported_key(uint32_t x) { return x ^ (0x6b28d4fau - key_exported); }

static uint32_t key_written = 0x5a17c3e9u;
EXPORT uint32_t written_key(uint32_t x) { KEEP(&key_written); return x ^ (0x6b28d4fau - key_written); }
EXPORT void set_key(uint32_t v) { KEEP(&key_written); key_written = v; }

static uint32_t key_escaped = 0x5a17c3e9u;  // sink_ptr may write it, later too
EXPORT uint32_t escaped_key(uint32_t x) { sink_ptr(&key_escaped); return x ^ (0x6b28d4fau - key_escaped); }

static uintptr_t key_address = (uintptr_t)&marked;  // an address: the loader relocates it
EXPORT uintptr_t address_key(uintptr_t x) { KEEP(&key_address); return x ^ (0x6b28d4fau - key_address); }

static uint32_t config_level = 7;  // never written, but only compared: a setting, not a key
EXPORT int level_test(int x) {
    KEEP(&config_level);
    if (config_level == 7)
        return x + 1;
    return x - 1;
}
static uint32_t key_unset;  // zero until something writes it: the loader's fill, not a key the program chose
EXPORT uint32_t unset_key(uint32_t x) { KEEP(&key_unset); return x ^ (0x6b28d4fau - key_unset); }
static uint32_t config_mode = 0x5a17c3e9u;  // never written; (mode ^ c) == 1 is mode == c ^ 1: a setting too
EXPORT int key_branch(int x) {
    KEEP(&config_mode);
    if ((config_mode ^ 0x5a17c3e8u) == 1)
        return x + mark(1);
    return x - mark(2);
}
