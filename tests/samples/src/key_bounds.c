// Adjacent mutable buffers and encoded keys. All values are synthetic.
#include <stdint.h>
#define KEEP(p) __asm__ volatile("" : : "r"(p) : "memory")
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#define LOCAL EXPORT
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#define LOCAL __attribute__((noinline, used, visibility("hidden")))
#endif
#define OBJECT(name) static struct { volatile uint8_t bytes[32]; uint32_t key; } name = {{1}, 0x537ac921u}
#define RESULT(name) (x ^ (0x648bda32u - name.key))
#define TOUCH(name) do { KEEP(&name); x += name.bytes[0]; } while (0)
static volatile uint8_t page_padding[16] = {7};
EXPORT uint8_t read_padding(void) { return page_padding[0]; }

OBJECT(loop_data);
EXPORT uint32_t bounded_loop(uint32_t x) {
    TOUCH(loop_data);
    _Pragma("clang loop unroll(disable)")
    for (uint32_t i = 0; i < 28; ++i) {
        loop_data.bytes[i] ^= 7;
        __asm__ volatile("" : "+r"(i)); // retain the unsigned comparison at -O2
    }
    return RESULT(loop_data);
}
OBJECT(mask_data);
EXPORT uint32_t bounded_mask(uint32_t x) {
    KEEP(&mask_data);
    mask_data.bytes[x & 31] = 7;
    return RESULT(mask_data);
}
OBJECT(boolean_data);
EXPORT uint32_t bounded_boolean_loop(uint32_t x) {
    TOUCH(boolean_data);
    uint32_t i = 0;
    for (;;) {
        boolean_data.bytes[i] = 9;
        ++i;
        unsigned again = i < 28;
        __asm__ volatile("" : "+r"(again));
        if (!(again & 1)) break;
    }
    return RESULT(boolean_data);
}
OBJECT(overlap_data);
EXPORT uint32_t overlap_last(uint32_t x) {
    TOUCH(overlap_data);
    _Pragma("clang loop unroll(disable)")
    for (uint32_t i = 0; i <= 32; ++i) {
        ((volatile uint8_t *)&overlap_data)[i] = 7;
        __asm__ volatile("" : "+r"(i));
    }
    return RESULT(overlap_data);
}
OBJECT(wide_data);
EXPORT uint32_t overlap_width(uint32_t x) {
    KEEP(&wide_data);
    *(volatile uint64_t *)((uint8_t *)&wide_data + 28) = x;
    KEEP(&wide_data);
    return RESULT(wide_data);
}
OBJECT(unknown_data);
EXPORT uint32_t unbounded_index(uint32_t x) {
    TOUCH(unknown_data);
    ((volatile uint8_t *)&unknown_data)[x] = 7;
    return RESULT(unknown_data);
}
OBJECT(initial_data);
EXPORT uint32_t unknown_initial(uint32_t x) {
    TOUCH(initial_data);
    uint32_t i = x;
    do { ((volatile uint8_t *)&initial_data)[i] = 7; } while (++i < 28);
    return RESULT(initial_data);
}

LOCAL uint32_t read_bytes(const volatile uint8_t *p) { return p[0] + p[7]; }
LOCAL uint32_t forward_bytes(const volatile uint8_t *p) { return read_bytes(p) + 1; }
LOCAL void write_bytes(volatile uint8_t *p) { p[32] = 7; }
volatile const uint8_t *saved_pointer;
LOCAL void save_bytes(const volatile uint8_t *p) { saved_pointer = p; }
LOCAL const volatile uint8_t *return_bytes(const volatile uint8_t *p) { return p; }
LOCAL uint32_t mixed_bytes(const volatile uint8_t *p, volatile uint8_t *q) { q[32] = 7; return p[0]; }

OBJECT(read_data);
EXPORT uint32_t readonly_call(uint32_t x) { KEEP(&read_data); x += read_bytes(read_data.bytes); return RESULT(read_data); }
OBJECT(forward_data);
EXPORT uint32_t readonly_forward(uint32_t x) { KEEP(&forward_data); x += forward_bytes(forward_data.bytes); return RESULT(forward_data); }
OBJECT(write_data);
EXPORT uint32_t written_callee(uint32_t x) { KEEP(&write_data); write_bytes(write_data.bytes); return RESULT(write_data); }
OBJECT(save_data);
EXPORT uint32_t saved_callee(uint32_t x) { KEEP(&save_data); save_bytes(save_data.bytes); return RESULT(save_data); }
OBJECT(return_data);
EXPORT uint32_t returned_callee(uint32_t x) { KEEP(&return_data); save_bytes(return_bytes(return_data.bytes)); return RESULT(return_data); }
OBJECT(mixed_data);
EXPORT uint32_t mixed_callee(uint32_t x) { KEEP(&mixed_data); x += mixed_bytes(mixed_data.bytes, mixed_data.bytes); return RESULT(mixed_data); }
OBJECT(indirect_data);
EXPORT uint32_t indirect_callee(uint32_t x, uint32_t (*fn)(const volatile uint8_t *)) {
    TOUCH(indirect_data); x += fn(indirect_data.bytes); return RESULT(indirect_data);
}
OBJECT(signed_data);
EXPORT uint32_t signed_index(uint32_t x) {
    TOUCH(signed_data);
    ((volatile uint8_t *)&signed_data)[(int8_t)x] = 7;
    return RESULT(signed_data);
}
OBJECT(reverse_data);
EXPORT uint32_t reverse_index(uint32_t x) {
    KEEP(&reverse_data);
    for (int32_t i = -33; i < 0; ++i) {
        ((volatile uint8_t *)&reverse_data + 33)[i] = 7;
        __asm__ volatile("" : "+r"(i));
    }
    return RESULT(reverse_data) ^ (0x21u - reverse_data.bytes[0]);
}
