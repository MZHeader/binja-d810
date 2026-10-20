// v4.3 fixtures: outlined flattening. A flattened function split into a dispatcher that switches over a
// parameter, handlers that are separate functions reading and writing the original locals through a frame
// pointer, and a way back through two code pointers in writable data: a handler calls through the first to a
// one-line stub, which calls through the second to the dispatcher. At -O2 the calls are tail branches; at -O0
// they are calls then returns. A self-calling dispatcher returns its own result through a code pointer. The
// pointers are volatile, so the compiler reads each one where it is called and cannot call its target directly
// (the binary keeps no trace of volatile). Every state constant is made up for these fixtures.
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
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

// ---- Handlers outlined from their dispatcher ----

struct frame { int r; int x; };
typedef int (*step_t)(uint32_t, struct frame *);
LOCAL int ol_dispatch(uint32_t s, struct frame *f);
LOCAL int ol_stub(uint32_t s, struct frame *f);
static step_t volatile ol_back = ol_dispatch;  // never written
static step_t volatile ol_return = ol_stub;  // never written

LOCAL int ol_stub(uint32_t s, struct frame *f) { return ol_back(s, f); }
LOCAL int ol_h1(uint32_t s, struct frame *f) { f->r += mark(1); return ol_return(0x1b4d8c27u, f); }
LOCAL int ol_h2(uint32_t s, struct frame *f) {
    f->r += mark(2);
    return ol_return(f->r > f->x ? 0x5e91a3f0u : 0x33c6d218u, f);
}
LOCAL int ol_h3(uint32_t s, struct frame *f) { f->r += mark(3); return ol_return(0x33c6d218u, f); }
LOCAL int ol_h4(uint32_t s, struct frame *f) { f->r += mark(4); return ol_return(0x6f2b9c41u, f); }
LOCAL int ol_shared(uint32_t s, struct frame *f) { f->x += mark(6); return ol_return(0x6f2b9c41u, f); }
LOCAL int ol_dispatch(uint32_t s, struct frame *f) {
    B(s);
    switch (s) {
    case 0x7a3c19e5u: return ol_h1(s, f);
    case 0x1b4d8c27u: return ol_h2(s, f);
    case 0x5e91a3f0u: return ol_h3(s, f);
    case 0x33c6d218u: return ol_h4(s, f);
    case 0x6f2b9c41u: return f->r + mark(5);
    case 0x0e57b3a9u: return ol_shared(s, f);  // also called directly: not the dispatcher's alone
    default: return -1;
    }
}
EXPORT int ol_machine(int x) {
    struct frame f = {0, x};
    return ol_dispatch(0x7a3c19e5u, &f);
}
EXPORT int ol_other(int x) {
    struct frame f = {0, x};
    return ol_shared(0, &f);
}

// ---- A dispatcher that calls itself through a code pointer ----

typedef int (*self_t)(uint32_t, int, int);
LOCAL int sc_dispatch(uint32_t s, int r, int x);
static self_t volatile sc_self = sc_dispatch;  // never written
LOCAL int sc_dispatch(uint32_t s, int r, int x) {
    B(s);
    switch (s) {
    case 0x2d81f6a3u: return sc_self(0x49e07b12u, r + mark(21), x);
    case 0x49e07b12u: r += mark(22); return sc_self(((r ^ x) & 1) ? 0x0c5a9e84u : 0x71f3d250u, r, x);
    case 0x0c5a9e84u: return sc_self(0x71f3d250u, r + mark(23), x);
    case 0x71f3d250u: return r + mark(24);
    default: return -1;
    }
}
EXPORT int sc_machine(int x) { return sc_dispatch(0x2d81f6a3u, 0, x); }

// ---- Must stay as they are: ordinary recursion, and a state machine passing the address of its own local ----

typedef int (*count_t)(int, int);
LOCAL int rc_count(int n, int acc);
static count_t volatile rc_self = rc_count;  // never written
LOCAL int rc_count(int n, int acc) {
    B(n);
    if (n == 0)
        return acc;
    return rc_self(n - 1, acc + mark(n));
}
EXPORT int rc_machine(int x) { return rc_count(x, 0); }

typedef int (*frame_self_t)(uint32_t, int *, int);
LOCAL int lf_dispatch(uint32_t s, int *acc, int x);
static frame_self_t volatile lf_self = lf_dispatch;  // never written
LOCAL int lf_dispatch(uint32_t s, int *acc, int x) {
    int local = *acc;
    B(s);
    switch (s) {
    case 0x6b1d93c2u: local += mark(61); return lf_self(0x1f84a6d0u, &local, x);
    case 0x1f84a6d0u: local += mark(62); return lf_self(x > 0 ? 0x57c3e91bu : 0x0ad6f274u, &local, x);
    case 0x57c3e91bu: local += mark(63); return lf_self(0x0ad6f274u, &local, x);
    case 0x0ad6f274u: return local + mark(64);
    default: return -1;
    }
}
EXPORT int lf_machine(int x) {
    int acc = 0;
    return lf_dispatch(0x6b1d93c2u, &acc, x);
}
