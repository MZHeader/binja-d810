// Private-register helpers in branched callers. These fixtures are analyzed, never executed.
#include <stdint.h>
#ifdef _WIN32
#define F __declspec(dllexport) __attribute__((naked, noinline, used))
#else
#define F __attribute__((naked, noinline, used, visibility("default")))
#endif
#if defined(__APPLE__) || (defined(_WIN32) && defined(__i386__))
#define S(n) "_" #n
#else
#define S(n) #n
#endif

#if defined(__aarch64__)
#define ENTER "stp x29, x30, [sp, #-48]!\nmov x29, sp\n"
#define LEAVE "ldp x29, x30, [sp], #48\nret\n"
#define INPUT "mov w14, w0\nmov w11, #37\nmov w9, #13\nmov w10, #23\n"
#define LEAF "ror w11, w11, #4\nstr w9, [sp, #16]\nmadd w11, w11, w9, w10\nret\n"
#define CALL "bl " S(cfg_leaf) "\n"
#define TEST "cbz w14, 1f\n"
#define TEST_READ "ldr w15, [sp, #16]\ncbz w15, 1f\n"
#define JUMP "b 2f\n"
#define LOOP "cbnz w14, 3b\n"
#define ZERO "str wzr, [sp, #16]\n"
#define ONE "mov w15, #1\nstr w15, [sp, #16]\n"
#define BYTE "strb wzr, [sp, #16]\n"
#define OTHER "mov w11, #53\n"
#define RESULT "mov w0, w11\n"
#define XOR "eor w11, w11, #1\n"
#define READ_ADD "ldr w12, [sp, #16]\nadd w0, w11, w12\n"
#define ESCAPE "add x0, sp, #16\n"
#define CORRUPT_RETURN "str xzr, [sp, #8]\n"
#define BAD_LEAF "mov x30, #0\nret\n"
#elif defined(__x86_64__)
#define ENTER "subq $40, %rsp\n"
#define LEAVE "addq $40, %rsp\nret\n"
#ifdef _WIN32
#define ARG "movl %ecx, %r8d\n"
#else
#define ARG "movl %edi, %r8d\n"
#endif
#define INPUT ARG "movl $37, %r11d\nmovl $13, %r9d\n"
#define LEAF "rorl $4, %r11d\nmovl %r9d, 24(%rsp)\nimull %r9d, %r11d\naddl $23, %r11d\nret\n"
#define CALL "call " S(cfg_leaf) "\n"
#define TEST "testl %r8d, %r8d\njz 1f\n"
#define TEST_READ "cmpl $0, 16(%rsp)\njz 1f\n"
#define JUMP "jmp 2f\n"
#define LOOP "testl %r8d, %r8d\njnz 3b\n"
#define ZERO "movl $0, 16(%rsp)\n"
#define ONE "movl $1, 16(%rsp)\n"
#define BYTE "movb $0, 16(%rsp)\n"
#define OTHER "movl $53, %r11d\n"
#define RESULT "movl %r11d, %eax\n"
#define XOR "xorl $1, %r11d\n"
#define READ_ADD RESULT "addl 16(%rsp), %eax\n"
#define ESCAPE "leaq 16(%rsp), %rax\n"
#define CORRUPT_RETURN "movq $0, 40(%rsp)\n"
#define BAD_LEAF "movq $0, (%rsp)\nret\n"
#else
#define ENTER "subl $24, %esp\n"
#define LEAVE "addl $24, %esp\nret\n"
#define INPUT "movl 28(%esp), %edx\nmovl $37, %eax\nmovl $13, %ecx\n"
#define LEAF "rorl $4, %eax\nmovl %ecx, 12(%esp)\nimull %ecx, %eax\naddl $23, %eax\nret\n"
#define CALL "call " S(cfg_leaf) "\n"
#define TEST "testl %edx, %edx\njz 1f\n"
#define TEST_READ "cmpl $0, 8(%esp)\njz 1f\n"
#define JUMP "jmp 2f\n"
#define LOOP "testl %edx, %edx\njnz 3b\n"
#define ZERO "movl $0, 8(%esp)\n"
#define ONE "movl $1, 8(%esp)\n"
#define BYTE "movb $0, 8(%esp)\n"
#define OTHER "movl $53, %eax\n"
#define RESULT ""
#define XOR "xorl $1, %eax\n"
#define READ_ADD "addl 8(%esp), %eax\n"
#define ESCAPE "leal 8(%esp), %eax\n"
#define CORRUPT_RETURN "movl $0, 24(%esp)\n"
#define BAD_LEAF "movl $0, (%esp)\nret\n"
#endif

F void cfg_leaf(void) { __asm__(LEAF); }
F void cfg_bad_return_leaf(void) { __asm__(BAD_LEAF); }
F uint32_t cfg_after(uint32_t x) {
    __asm__(ENTER INPUT CALL TEST ONE JUMP "1:\n" ZERO "2:\n" READ_ADD LEAVE);
}
#define DIAMOND TEST ONE JUMP "1:\n" ZERO "2:\n"
#define FOUR DIAMOND DIAMOND DIAMOND DIAMOND
F uint32_t cfg_diamonds(uint32_t x) {
    __asm__(ENTER INPUT CALL FOUR FOUR FOUR FOUR READ_ADD LEAVE);
}
F uint32_t cfg_before(uint32_t x) {
    __asm__(ENTER INPUT TEST OTHER "1:\n" CALL RESULT LEAVE);
}
F uint32_t cfg_returns(uint32_t x) {
    __asm__(ENTER INPUT CALL TEST XOR RESULT LEAVE "1:\n" RESULT LEAVE);
}
F uint32_t cfg_arms(uint32_t x) {
    __asm__(ENTER INPUT TEST OTHER CALL JUMP "1:\n" CALL "2:\n" RESULT LEAVE);
}
F uint32_t cfg_one_live(uint32_t x) {
    __asm__(ENTER INPUT CALL TEST ONE "1:\n" READ_ADD LEAVE);
}
F uint32_t cfg_partial(uint32_t x) {
    __asm__(ENTER INPUT CALL TEST ONE JUMP "1:\n" BYTE "2:\n" READ_ADD LEAVE);
}
F uint32_t cfg_arms_live(uint32_t x) {
    __asm__(ENTER INPUT TEST OTHER CALL JUMP "1:\n" CALL "2:\n" READ_ADD LEAVE);
}
F uint32_t cfg_condition_read(uint32_t x) {
    __asm__(ENTER INPUT CALL TEST_READ XOR "1:\n" RESULT LEAVE);
}
F uint32_t cfg_loop(uint32_t x) {
    __asm__(ENTER INPUT "3:\n" CALL LOOP RESULT LEAVE);
}
F uintptr_t cfg_escape(uint32_t x) {
    __asm__(ENTER INPUT CALL TEST ESCAPE LEAVE "1:\n" RESULT LEAVE);
}
F uint32_t cfg_unbalanced(uint32_t x) {
    __asm__(ENTER INPUT CALL TEST RESULT LEAVE "1:\n" RESULT "ret\n");
}
F uint32_t cfg_returnchanged(uint32_t x) {
    __asm__(ENTER INPUT CALL TEST CORRUPT_RETURN "1:\n" RESULT LEAVE);
}
