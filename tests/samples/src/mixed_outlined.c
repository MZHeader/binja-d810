// A dispatcher mixing direct self-calls with a separate handler that calls back.
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

// ---- A machine mixing direct self calls with an outlined handler ----
struct mixed_frame { uint64_t r; };
typedef uint64_t (*mixed_step_t)(uint32_t, struct mixed_frame *);
LOCAL uint64_t mixed_dispatch(uint32_t s, struct mixed_frame *f);
static mixed_step_t volatile mixed_back = mixed_dispatch;
LOCAL uint64_t mixed_handler(uint32_t s, struct mixed_frame *f) {
    f->r += (uint64_t)mark(82);
    return mixed_back(0x623d1945u, f);
}
LOCAL uint64_t mixed_dispatch(uint32_t s, struct mixed_frame *f) {
    B(s);
    switch (s) {
    case 0x173b5d29u: f->r += (uint64_t)mark(81); return mixed_back(0x4c816e37u, f);
    case 0x4c816e37u: return mixed_handler(s, f);
    case 0x623d1945u: f->r += (uint64_t)mark(83); return mixed_back(0x2a758c61u, f);
    case 0x2a758c61u: return f->r + (uint64_t)mark(84);
    default: return 0;
    }
}
EXPORT uint64_t mixed_machine(uint64_t x) {
    struct mixed_frame f = {x};
    return mixed_dispatch(0x173b5d29u, &f);
}
