// A saved register is declared only by the dispatcher. Handlers carry it back
// implicitly, with the state in an incoming stack slot. Controls change the
// register or the stack frame before entering the handler.
#if defined(__APPLE__) || (defined(_WIN32) && defined(__i386__))
#define SYM(name) _##name
#else
#define SYM(name) name
#endif
#ifdef __APPLE__
#define LOCAL(name) L##name
#else
#define LOCAL(name) .L##name
#endif
.text
.macro function name
.p2align 4
.globl \name
#ifdef __ELF__
.type \name, @function
#endif
\name:
.endm
.macro endfunction name
#ifdef __ELF__
.size \name, .-\name
#endif
.endm

#if defined(__aarch64__)
.macro dispatch name, h1, h2, h3, changed=0, shifted=0
function \name
.if \shifted
    sub sp, sp, #16
.endif
    ldr w8, [sp, #0x20]
    cmp w8, #11
    b.ne LOCAL(second)\@
.if \changed
    eor x21, x21, #1
.endif
    b \h1
LOCAL(second)\@:
    cmp w8, #22
    b.eq \h2
    cmp w8, #33
    b.eq \h3
    mov x0, x21
.if \shifted
    add sp, sp, #16
.endif
    ret
endfunction \name
.endm
.macro handler name, dest, state
function \name
    mov w8, #\state
    str w8, [sp, #0x20]
    b \dest
endfunction \name
.endm
#else
#ifdef __x86_64__
#define STACK %rsp
#define SAVED %r12
#define RESULT %rax
#else
#define STACK %esp
#define SAVED %esi
#define RESULT %eax
#endif
.macro dispatch name, h1, h2, h3, changed=0, shifted=0
function \name
.if \shifted
    sub $16, STACK
.endif
    cmpl $11, 0x20(STACK)
    jne LOCAL(second)\@
.if \changed
    xor $1, SAVED
.endif
    jmp \h1
LOCAL(second)\@:
    cmpl $22, 0x20(STACK)
    je \h2
    cmpl $33, 0x20(STACK)
    je \h3
    mov SAVED, RESULT
.if \shifted
    add $16, STACK
.endif
    ret
endfunction \name
.endm
.macro handler name, dest, state
function \name
    movl $\state, 0x20(STACK)
    jmp \dest
endfunction \name
.endm
#endif

dispatch SYM(incoming_dispatch), SYM(incoming_h1), SYM(incoming_h2), SYM(incoming_h3)
handler SYM(incoming_h1), SYM(incoming_dispatch), 22
handler SYM(incoming_h2), SYM(incoming_dispatch), 33
handler SYM(incoming_h3), SYM(incoming_dispatch), 44

dispatch SYM(changed_dispatch), SYM(changed_h1), SYM(changed_h2), SYM(changed_h3), 1
handler SYM(changed_h1), SYM(changed_dispatch), 22
handler SYM(changed_h2), SYM(changed_dispatch), 33
handler SYM(changed_h3), SYM(changed_dispatch), 44

dispatch SYM(shifted_dispatch), SYM(shifted_h1), SYM(shifted_h2), SYM(shifted_h3), 0, 1
handler SYM(shifted_h1), SYM(shifted_dispatch), 22
handler SYM(shifted_h2), SYM(shifted_dispatch), 33
handler SYM(shifted_h3), SYM(shifted_dispatch), 44
