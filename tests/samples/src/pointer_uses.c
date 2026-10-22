// Pointer carriers: returning a pointer is safe only when every caller use is safe.
#include <stdint.h>
#define KEEP(p) __asm__ volatile("" : : "r"(p) : "memory")
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#define LOCAL EXPORT
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#define LOCAL __attribute__((noinline, used, visibility("hidden")))
#endif
#define OBJECT(name) static struct { uint8_t bytes[16]; uint32_t key; } name = {{3}, 0x537ac921u}
#define RESULT(name) (x ^ (0x648bda32u - name.key))
#define ANCHOR(name) do { KEEP(&name); if (*(volatile uint8_t *)name.bytes != 3) return 0; } while (0)
static volatile uint8_t padding[16] = {7};
EXPORT uint8_t read_padding(void) { return padding[0]; }

LOCAL uint32_t *pass_pointer(uint32_t *p) { return p; }
LOCAL uint32_t *forward_pointer(uint32_t *p) { return pass_pointer(p); }
LOCAL uint8_t *offset_pointer(uint8_t *p) { return p + 3; }
volatile void *saved_pointer;
LOCAL void save_pointer(void *p) { saved_pointer = p; }

LOCAL uint32_t read_indirect(uint32_t **p) { return **p; }
LOCAL uint32_t *get_indirect(uint32_t **p) { return *p; }
LOCAL uint32_t local_read(uint32_t *p) {
    uint32_t *volatile saved = p; KEEP(&saved); return read_indirect((uint32_t **)&saved);
}
LOCAL uint32_t *local_return(uint32_t *p) {
    uint32_t *volatile saved = p; KEEP(&saved); return get_indirect((uint32_t **)&saved);
}
LOCAL void local_write(uint32_t *p, uint32_t x) {
    uint32_t *volatile saved = p; KEEP(&saved); x += read_indirect((uint32_t **)&saved); *saved = x;
}
LOCAL void local_escape(uint32_t *p) {
    uint32_t *volatile saved = p; KEEP(&saved); save_pointer((void *)&saved);
}
LOCAL void write_indirect(uint32_t **p) { **p = 7; }
LOCAL void local_forward(uint32_t *p) {
    uint32_t *volatile saved = p; KEEP(&saved); write_indirect((uint32_t **)&saved);
}
LOCAL uintptr_t local_partial(uint32_t *p) {
    uint32_t *volatile saved = p; KEEP(&saved);
    uint32_t n = read_indirect((uint32_t **)&saved);
    return n + *(volatile uint16_t *)&saved;
}
LOCAL uint32_t local_partial_write(uint32_t *p) {
    uint32_t *volatile saved = p; KEEP(&saved);
    uint32_t n = read_indirect((uint32_t **)&saved);
    *(volatile uint8_t *)&saved = 0;
    return n + *saved;
}

OBJECT(read_data);
EXPORT uint32_t return_read(uint32_t x) {
    ANCHOR(read_data); x += *pass_pointer(&read_data.key); return RESULT(read_data);
}
OBJECT(chain_data);
EXPORT uint32_t return_chain(uint32_t x) {
    ANCHOR(chain_data); x += *forward_pointer(&chain_data.key); return RESULT(chain_data);
}
OBJECT(offset_data);
EXPORT uint32_t return_offset(uint32_t x) {
    ANCHOR(offset_data); x += *offset_pointer(offset_data.bytes); return RESULT(offset_data);
}
OBJECT(local_read_data);
EXPORT uint32_t stored_local_read(uint32_t x) {
    ANCHOR(local_read_data); x += local_read(&local_read_data.key); return RESULT(local_read_data);
}
OBJECT(local_return_data);
EXPORT uint32_t stored_local_return(uint32_t x) {
    ANCHOR(local_return_data); x += *local_return(&local_return_data.key); return RESULT(local_return_data);
}

OBJECT(write_data);
EXPORT uint32_t return_write(uint32_t x) {
    ANCHOR(write_data); *pass_pointer(&write_data.key) = x; ANCHOR(write_data); return RESULT(write_data);
}
OBJECT(escape_data);
EXPORT uint32_t return_escape(uint32_t x) {
    ANCHOR(escape_data); save_pointer(forward_pointer(&escape_data.key)); return RESULT(escape_data);
}
OBJECT(top_data);
EXPORT uint32_t *return_top(void) { ANCHOR(top_data); return forward_pointer(&top_data.key); }
EXPORT uint32_t return_top_reader(uint32_t x) { ANCHOR(top_data); return RESULT(top_data); }
OBJECT(mixed_data);
EXPORT uint32_t return_mixed(uint32_t x) {
    ANCHOR(mixed_data); x += *pass_pointer(&mixed_data.key);
    *pass_pointer(&mixed_data.key) = x; ANCHOR(mixed_data); return RESULT(mixed_data);
}
OBJECT(indirect_data);
EXPORT uint32_t return_indirect(uint32_t x, uint32_t *(*fn)(uint32_t *)) {
    ANCHOR(indirect_data); x += *fn(&indirect_data.key); return RESULT(indirect_data);
}

OBJECT(local_write_data);
EXPORT uint32_t stored_local_write(uint32_t x) {
    ANCHOR(local_write_data); local_write(&local_write_data.key, x); return RESULT(local_write_data);
}
OBJECT(local_escape_data);
EXPORT uint32_t stored_local_escape(uint32_t x) {
    ANCHOR(local_escape_data); local_escape(&local_escape_data.key); return RESULT(local_escape_data);
}
OBJECT(local_forward_data);
EXPORT uint32_t stored_local_forward(uint32_t x) {
    ANCHOR(local_forward_data); local_forward(&local_forward_data.key); return RESULT(local_forward_data);
}
OBJECT(partial_data);
EXPORT uint32_t stored_partial(uint32_t x) {
    ANCHOR(partial_data); x += local_partial(&partial_data.key); return RESULT(partial_data);
}
OBJECT(partial_write_data);
EXPORT uint32_t stored_partial_write(uint32_t x) {
    ANCHOR(partial_write_data); x += local_partial_write(&partial_write_data.key); return RESULT(partial_write_data);
}
