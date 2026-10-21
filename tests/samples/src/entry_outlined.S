// Outlined dispatchers with an undeclared callback register. Only one machine's
// handlers preserve it; another uses a volatile register, and a third overwrites it.
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
#define SAVED x21
#define VOLATILE x9
.macro dispatch name, h1, h2, h3, reg
function \name
    cmp w0, #11
    b.eq \h1
    cmp w0, #22
    b.eq \h2
    cmp w0, #33
    b.eq \h3
    cmp w0, #44
    b.ne LOCAL(default)\@
    br \reg
LOCAL(default)\@:
    mov x0, #0
    ret
endfunction \name
.endm
.macro handler name, dest, state, reg, clobber=0
function \name
.if \clobber
    mov \reg, #0
.endif
    mov w0, #\state
    b \dest
endfunction \name
.endm
#else
#if defined(__x86_64__)
#define SAVED %r12
#define VOLATILE %r10
#ifdef _WIN32
#define ARG %ecx
#else
#define ARG %edi
#endif
#else
#define SAVED %esi
#define VOLATILE %edx
#define ARG 4(%esp)
#endif
.macro dispatch name, h1, h2, h3, reg
function \name
    cmpl $11, ARG
    je \h1
    cmpl $22, ARG
    je \h2
    cmpl $33, ARG
    je \h3
    cmpl $44, ARG
    jne LOCAL(default)\@
    jmp *\reg
LOCAL(default)\@:
    xor %eax, %eax
    ret
endfunction \name
.endm
.macro handler name, dest, state, reg, clobber=0
function \name
.if \clobber
    xor \reg, \reg
.endif
    movl $\state, ARG
    jmp \dest
endfunction \name
.endm
#endif

dispatch SYM(entry_dispatch), SYM(entry_h1), SYM(entry_h2), SYM(entry_h3), SAVED
handler SYM(entry_h1), SYM(entry_dispatch), 22, SAVED
handler SYM(entry_h2), SYM(entry_dispatch), 33, SAVED
handler SYM(entry_h3), SYM(entry_forward), 44, SAVED
function SYM(entry_forward)
#if defined(__aarch64__)
    b SYM(entry_dispatch)
#else
    jmp SYM(entry_dispatch)
#endif
endfunction SYM(entry_forward)

dispatch SYM(volatile_dispatch), SYM(volatile_h1), SYM(volatile_h2), SYM(volatile_h3), VOLATILE
handler SYM(volatile_h1), SYM(volatile_dispatch), 22, VOLATILE
handler SYM(volatile_h2), SYM(volatile_dispatch), 33, VOLATILE
handler SYM(volatile_h3), SYM(volatile_dispatch), 44, VOLATILE

dispatch SYM(clobber_dispatch), SYM(clobber_h1), SYM(clobber_h2), SYM(clobber_h3), SAVED
handler SYM(clobber_h1), SYM(clobber_dispatch), 22, SAVED, 1
handler SYM(clobber_h2), SYM(clobber_dispatch), 33, SAVED
handler SYM(clobber_h3), SYM(clobber_dispatch), 44, SAVED
