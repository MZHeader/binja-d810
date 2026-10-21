// v4.2 fixtures: hand-flattened state machines (a sparse compare tree, a dense switch, nested whiles, a machine
// with a counted loop) and ordinary code D-810 must leave alone. B(s) at every dispatch hands the state to an
// empty asm that may change it, so the compiler cannot thread the dispatcher away itself; the binary keeps the
// value unchanged, which is what an analysis sees. Every state constant is made up for these fixtures, with a
// byte that is not printable (Binary Ninja writes a store of 4 printable bytes as a strncpy call at -O0).
// Conditional transitions read what the handlers computed, so the compiler cannot hoist them out of the loop.
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

// A handler's visible work, in the order the machine runs it. Exported and storing k, so every build keeps
// the calls, names them and passes k (with an empty body, Binary Ninja sees an arm64 ret and drops them).
EXPORT_DATA volatile int marked;
EXPORT int mark(int k) { B(k); marked = k; return k; }
EXPORT_DATA uint32_t *peeked;
LOCAL void peek(uint32_t *p) { KEEP(p); peeked = p; }  // may read or write *p, later too: p escapes

// ---- Flattened ----

EXPORT int fl_tree(int x) {  // 5 sparse states, one transition picked by r > x
    uint32_t s = 0x7a3c19e5u;
    int r = 0;
    for (;;) {
        B(s);
        if (s == 0x7a3c19e5u) { r += mark(1); s = 0x1b4d8c27u; }
        else if (s == 0x1b4d8c27u) { r += mark(2); s = r > x ? 0x5e91a3f0u : 0x33c6d218u; }
        else if (s == 0x5e91a3f0u) { r += mark(3); s = 0x33c6d218u; }
        else if (s == 0x33c6d218u) { r += mark(4); s = 0x6f2b9c41u; }
        else if (s == 0x6f2b9c41u) { return r + mark(5); }
        else { return -1; }
    }
}

EXPORT int fl_dense(int x) {  // 7 consecutive states: a switch compilers emit as a jump table
    uint32_t s = 0x40;
    int r = x;
    for (;;) {
        B(s);
        switch (s) {
        case 0x40: r += mark(11); s = 0x43; break;
        case 0x41: r += mark(12); s = 0x44; break;
        case 0x42: r += mark(13); s = 0x46; break;
        case 0x43: r += mark(14); s = 0x45; break;
        case 0x44: r += mark(15); s = 0x42; break;
        case 0x45: r += mark(16); s = 0x41; break;
        case 0x46: return r + mark(17);
        default: return -1;
        }
    }
}

EXPORT int fl_nested(int x) {  // Hodur-style: one while per state inside the dispatch loop
    uint32_t s = 0x2d81f6a3u;
    int r = 0;
    for (;;) {
        B(s);
        while (s == 0x2d81f6a3u) { r += mark(21); s = 0x49e07b12u; B(s); }
        while (s == 0x49e07b12u) { r += mark(22); s = ((r ^ x) & 1) ? 0x0c5a9e84u : 0x71f3d250u; B(s); }
        while (s == 0x0c5a9e84u) { r += mark(23); s = 0x71f3d250u; B(s); }
        if (s == 0x71f3d250u) { return r + mark(24); }
    }
}

EXPORT int fl_loop(int n) {  // a counted loop inside one state
    uint32_t s = 0x58c2e13bu;
    int r = 0, i = 0;
    for (;;) {
        B(s);
        if (s == 0x58c2e13bu) { r += mark(31); i = 0; s = 0x26b9d4f7u; }
        else if (s == 0x26b9d4f7u) {
            if (i < n) { r += mark(40 + i); i++; } else { s = 0x4e7a0d93u; }
        }
        else if (s == 0x4e7a0d93u) { r += mark(32); s = 0x1c95b2e6u; }
        else if (s == 0x1c95b2e6u) { r += mark(33); s = 0x6a17c05eu; }
        else if (s == 0x6a17c05eu) { return r + mark(34); }
        else { return -1; }
    }
}

EXPORT int fl_large_loop(int x) {  // recovering the dispatcher must not require unrolling this handler
    uint32_t s = 0x5187a321u;
    uint32_t r = (uint32_t)x;
    for (;;) {
        B(s);
        if (s == 0x5187a321u) {
            for (uint32_t i = 0; i < 96; ++i) {
                B(i);
                r = (r ^ i) + 7;
            }
            s = 0x3b65d902u;
        }
        else if (s == 0x3b65d902u) { r += mark(91); s = 0x6c194e53u; }
        else if (s == 0x6c194e53u) { return r + mark(92); }
        else { return -1; }
    }
}

static int (*lazy)(int);  // resolved on first use, as malware resolves its imports
LOCAL int twice(int v) { B(v); return v * 2; }
EXPORT int fl_glue(int x) {  // a handler picks the next state, then shared code with a branch of its own runs
    uint32_t s = 0x3e6a1d97u;
    int r = 0;
    for (;;) {
        B(s);
        if (s == 0x3e6a1d97u) {  // the pick reads a value the shared code reads again
            int v = mark(51);
            s = 0x17f4a6c9u;
            if (v > x) s = 0x52c8b0e3u;
            KEEP(&lazy);
            if (!lazy) lazy = twice;
            r += lazy(v) + v;
        }
        else if (s == 0x52c8b0e3u) { r += mark(52); s = 0x6d09e3b5u; }
        else if (s == 0x17f4a6c9u) { r ^= mark(53) * 3; s = 0x6d09e3b5u; }  // unlike 52: not merged into one call
        else if (s == 0x6d09e3b5u) { return r + mark(54); }
        else { return -1; }
    }
}

// ---- Negatives: no state machine, or a state the function does not own ----

EXPORT int plain_for(int n) {
    int r = 0;
    for (int i = 0; i < 10; i++) r += mark(i);
    return r + n;
}

EXPORT int found_flag(const int *a, int n, int k) {
    int found = 0;
    for (int i = 0; i < n; i++) {
        if (a[i] == k) found = 1;
        B(found);
    }
    if (found == 1) return mark(41);
    return mark(42);
}

EXPORT_DATA volatile uint32_t g_state;

EXPORT int global_state(int x) {  // every read of the state is a load: another thread may change it
    int r = 0;
    g_state = 0x13a7c5e9u;
    for (;;) {
        uint32_t s = g_state;
        if (s == 0x13a7c5e9u) { r += mark(51); g_state = 0x4c0e8b36u; }
        else if (s == 0x4c0e8b36u) { r += mark(52); g_state = x > 0 ? 0x7d52f1a4u : 0x2e9b6c03u; }
        else if (s == 0x7d52f1a4u) { r += mark(53); g_state = 0x2e9b6c03u; }
        else if (s == 0x2e9b6c03u) { return r + mark(54); }
        else { return -1; }
    }
}

EXPORT int escaped_state(int x) {  // peek may change the state after every transition
    uint32_t s = 0x6b1d93c2u;
    int r = 0;
    peek(&s);
    for (;;) {
        if (s == 0x6b1d93c2u) { r += mark(61); s = 0x1f84a6d0u; peek(&s); }
        else if (s == 0x1f84a6d0u) { r += mark(62); s = x > 0 ? 0x57c3e91bu : 0x0ad6f274u; peek(&s); }
        else if (s == 0x57c3e91bu) { r += mark(63); s = 0x0ad6f274u; peek(&s); }
        else if (s == 0x0ad6f274u) { return r + mark(64); }
        else { return -1; }
    }
}
