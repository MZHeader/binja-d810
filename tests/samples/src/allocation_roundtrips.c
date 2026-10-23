#include <stdint.h>
#include <stddef.h>
#ifdef _WIN32
#define F __declspec(dllexport) __attribute__((noinline,used))
#define I __declspec(dllimport)
#else
#define F __attribute__((noinline,used,visibility("default")))
#define I
#endif
I void *malloc(size_t size);
I void free(void *pointer);
#define B(x) __asm__ volatile("" : "+r"(x) : : "memory")

F void *heap_allocate(size_t size) { return malloc(size); }
F void heap_release(void *pointer) { free(pointer); }
F void *heap_small(void) { return heap_allocate(sizeof(uint32_t)); }
F void heap_drop(void *p) { heap_release(p); }

F uint32_t heap_roundtrip(uint32_t x) {
    uint32_t *p=heap_allocate(sizeof(uint32_t)); B(p);
    *p=x^0x51u; B(p);
    uint32_t value=*p; B(value);
    heap_release(p);
    return value;
}
F uint32_t heap_box(uint32_t x) {
    uint32_t *p=heap_small(); B(p);
    *p=x*7+3; B(p);
    uint32_t value=*p; B(value);
    heap_drop(p);
    return value;
}
F uint32_t heap_partial(uint32_t x) {
    uint32_t *p=heap_allocate(sizeof(uint32_t)); B(p);
    *p=x; B(p);
    ((uint8_t *)p)[1]=0x67; B(p);
    uint32_t value=*p; B(value);
    heap_release(p);
    return value;
}
void *volatile heap_saved;
F uint32_t heap_escape(uint32_t x) {
    uint32_t *p=heap_allocate(sizeof(uint32_t)); B(p);
    heap_saved=p;
    *p=x^0x51; B(p);
    uint32_t value=*p; B(value);
    heap_release(p);
    return value;
}
F uint32_t heap_failure(uint32_t x) {
    uint32_t *p=heap_allocate(sizeof(uint32_t)); B(p);
    if (!p) return 17;
    *p=x^0x51; B(p);
    uint32_t value=*p; B(value);
    heap_release(p);
    return value;
}
F uint32_t heap_merge(uint32_t x) {
    uint32_t *p=heap_allocate(sizeof(uint32_t)); B(p);
    uint32_t *q=(x&1) ? p : (uint32_t *)heap_saved; B(q);
    *q=x; B(q);
    uint32_t value=*q; B(value);
    heap_release(p);
    return value;
}
F uint32_t heap_observed_bits(uint32_t x) {
    uint32_t *p=heap_allocate(sizeof(uint32_t)); B(p);
    *p=x; B(p);
    uint32_t value=*p; B(value);
    uintptr_t address=(uintptr_t)p; B(address);
    heap_release(p);
    return value^(uint32_t)(address&15);
}
