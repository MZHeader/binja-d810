// Arithmetic summaries must discover register and frame inputs from the body, not the ABI.
#include <stdint.h>
#ifdef _WIN32
#define F __declspec(dllexport) __attribute__((noinline, used))
#define H F
#else
#define F __attribute__((noinline, used, visibility("default")))
#define H __attribute__((noinline, used, visibility("hidden")))
#endif
#ifdef __APPLE__
#define S(name) "_" #name
#else
#define S(name) #name
#endif
#define B(value) __asm__ volatile("" : "+r"(value))

H uint32_t cal_rotate(uint32_t a) { return (a >> 5) | (a << 27); }
H uint32_t cal_mix(uint32_t a, uint32_t b, uint32_t c) { return a * b + c; }
H uint32_t cal_chain(uint32_t x) { return cal_mix(cal_rotate(x), 29, 11) ^ 0x623417a9u; }
F uint32_t cal_nested(uint32_t x) { return cal_chain(x) ^ 0x5du; }
F uint32_t cal_constant(void) { return cal_chain(37) ^ 0x27u; }

volatile uint32_t cal_observed;
void *volatile cal_escaped;
H uint32_t cal_write(uint32_t x) { cal_observed = x; return x ^ 13; }
H uint32_t cal_mutable(void) { return cal_observed; }
H uint32_t cal_loop(uint32_t x) { while (x > 7) { B(x); x = (x >> 1) ^ 5; } return x; }
H uint32_t cal_indirect(uint32_t (*f)(uint32_t), uint32_t x) { return f(x) ^ 19; }
H uint32_t cal_tail(uint32_t x) { return cal_rotate(x); }
H void cal_sink(void *p) { cal_escaped = p; }
F uint32_t cal_keeps_write(uint32_t x) { return cal_write(x) ^ 29; }
F uint32_t cal_keeps_mutable(void) { return cal_mutable() ^ 37; }
F uint32_t cal_keeps_loop(uint32_t x) { return cal_loop(x) ^ 11; }

#if defined(__aarch64__)
#define ENTER "stp x29, x30, [sp, #-48]!\nmov x29, sp\n"
#define LEAVE "ldp x29, x30, [sp], #48\nret\n"
#define INPUTS "mov w11, #37\nmov w9, #13\nmov w10, #23\n"
H __attribute__((naked)) void cal_private_leaf(void) {
    __asm__("ror w11, w11, #4\nstr w9, [sp, #16]\nmadd w11, w11, w9, w10\nret");
}
F __attribute__((naked)) uint32_t cal_private(void) {
    __asm__(ENTER INPUTS "bl " S(cal_private_leaf) "\neor w0, w11, w9\n" LEAVE);
}
H __attribute__((naked)) void cal_private_second(void) {
    __asm__("ror w12, w11, #24\nstr w8, [sp, #20]\nldr w13, [sp, #20]\nadd w11, w12, w13\nret");
}
F __attribute__((naked)) uint32_t cal_private_chain(void) {
    __asm__(ENTER INPUTS "mov w8, #53\nbl " S(cal_private_leaf) "\nbl " S(cal_private_second)
            "\neor w0, w11, w9\n" LEAVE);
}
H __attribute__((naked)) void cal_flag_leaf(void) { __asm__("adds w11, w11, w10\nret"); }
F __attribute__((naked)) uint32_t cal_flag_result(void) {
    __asm__(ENTER "mov w11, #-1\nmov w10, #1\nbl " S(cal_flag_leaf) "\ncset w0, eq\n" LEAVE);
}
H __attribute__((naked)) void cal_frame_leaf(void) {
    __asm__("ldr w12, [sp, #16]\nstr w8, [sp, #20]\nldr w13, [sp, #20]\neor w11, w12, w13\nret");
}
F __attribute__((naked)) uint32_t cal_frame(void) {
    __asm__(ENTER "mov w8, #13\nmov w9, #37\nstr w9, [sp, #16]\nbl " S(cal_frame_leaf)
            "\nmov w0, w11\n" LEAVE);
}
F __attribute__((naked)) uint32_t cal_live_scratch(void) {
    __asm__(ENTER INPUTS "bl " S(cal_private_leaf) "\nldr w0, [sp, #16]\neor w0, w0, w11\n" LEAVE);
}
F __attribute__((naked)) uint32_t cal_partial_scratch(void) {
    __asm__(ENTER INPUTS "bl " S(cal_private_leaf)
            "\nstrb wzr, [sp, #16]\nldr w0, [sp, #16]\neor w0, w0, w11\n" LEAVE);
}
F __attribute__((naked)) uint32_t cal_overwritten_scratch(void) {
    __asm__(ENTER INPUTS "bl " S(cal_private_leaf)
            "\nstr wzr, [sp, #16]\nldr w0, [sp, #16]\neor w0, w0, w11\n" LEAVE);
}
F __attribute__((naked)) void cal_escaping_scratch(void) {
    __asm__(ENTER INPUTS "bl " S(cal_private_leaf) "\nadd x0, sp, #16\nbl " S(cal_sink) "\n" LEAVE);
}
#elif defined(__x86_64__)
#define ENTER "subq $40, %rsp\n"
#define LEAVE "addq $40, %rsp\nret\n"
#define INPUTS "movl $37, %r11d\nmovl $13, %r9d\nmovl $23, %r10d\n"
H __attribute__((naked)) void cal_private_leaf(void) {
    __asm__("rorl $4, %r11d\nmovl %r9d, 24(%rsp)\nimull %r9d, %r11d\naddl %r10d, %r11d\nret");
}
F __attribute__((naked)) uint32_t cal_private(void) {
    __asm__(ENTER INPUTS "call " S(cal_private_leaf) "\nmovl %r11d, %eax\nxorl %r9d, %eax\n" LEAVE);
}
H __attribute__((naked)) void cal_private_second(void) {
    __asm__("rorl $24, %r11d\nmovl %r8d, 28(%rsp)\nmovl 28(%rsp), %r10d\naddl %r10d, %r11d\nret");
}
F __attribute__((naked)) uint32_t cal_private_chain(void) {
    __asm__(ENTER INPUTS "movl $53, %r8d\ncall " S(cal_private_leaf) "\ncall " S(cal_private_second)
            "\nmovl %r11d, %eax\nxorl %r9d, %eax\n" LEAVE);
}
H __attribute__((naked)) void cal_flag_leaf(void) { __asm__("addl %r10d, %r11d\nret"); }
F __attribute__((naked)) uint32_t cal_flag_result(void) {
    __asm__(ENTER "movl $-1, %r11d\nmovl $1, %r10d\ncall " S(cal_flag_leaf) "\nsetz %al\nmovzbl %al, %eax\n" LEAVE);
}
H __attribute__((naked)) void cal_frame_leaf(void) {
    __asm__("movl 24(%rsp), %r11d\nmovl %r8d, 28(%rsp)\nmovl 28(%rsp), %r10d\nxorl %r10d, %r11d\nret");
}
F __attribute__((naked)) uint32_t cal_frame(void) {
    __asm__(ENTER "movl $13, %r8d\nmovl $37, 16(%rsp)\ncall " S(cal_frame_leaf) "\nmovl %r11d, %eax\n" LEAVE);
}
F __attribute__((naked)) uint32_t cal_live_scratch(void) {
    __asm__(ENTER INPUTS "call " S(cal_private_leaf) "\nmovl 16(%rsp), %eax\nxorl %r11d, %eax\n" LEAVE);
}
F __attribute__((naked)) uint32_t cal_partial_scratch(void) {
    __asm__(ENTER INPUTS "call " S(cal_private_leaf)
            "\nmovb $0, 16(%rsp)\nmovl 16(%rsp), %eax\nxorl %r11d, %eax\n" LEAVE);
}
F __attribute__((naked)) uint32_t cal_overwritten_scratch(void) {
    __asm__(ENTER INPUTS "call " S(cal_private_leaf)
            "\nmovl $0, 16(%rsp)\nmovl 16(%rsp), %eax\nxorl %r11d, %eax\n" LEAVE);
}
F __attribute__((naked)) void cal_escaping_scratch(void) {
#ifdef _WIN32
    __asm__(ENTER INPUTS "call " S(cal_private_leaf) "\nleaq 16(%rsp), %rcx\ncall " S(cal_sink) "\n" LEAVE);
#else
    __asm__(ENTER INPUTS "call " S(cal_private_leaf) "\nleaq 16(%rsp), %rdi\ncall " S(cal_sink) "\n" LEAVE);
#endif
}
#endif
