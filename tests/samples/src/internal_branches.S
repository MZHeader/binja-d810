// An indirect jump back into its own function can be lifted as a tail call.
// The direct edge makes the shared target belong to the function. The pointer
// stays writable so the target must be checked before restoring the back edge.
// Controls: a real call to a shared internal return block, and a written pointer.
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
#if defined(__ELF__)
.type \name, @function
#endif
\name:
.endm
.macro endfunction name
#if defined(__ELF__)
.size \name, .-\name
#endif
.endm

#if defined(__aarch64__)
function SYM(internal_branch)
    cbz w0, LOCAL(branch_target)
    adrp x8, LOCAL(branch_pointer)@PAGE
    ldr x8, [x8, LOCAL(branch_pointer)@PAGEOFF]
    br x8
LOCAL(branch_target):
    add w0, w0, #3
    ret
endfunction SYM(internal_branch)

function SYM(internal_call)
    cbz w0, LOCAL(call_target)
    stp x29, x30, [sp, #-16]!
    adrp x8, LOCAL(call_pointer)@PAGE
    ldr x8, [x8, LOCAL(call_pointer)@PAGEOFF]
    blr x8
    ldp x29, x30, [sp], #16
    ret
LOCAL(call_target):
    add w0, w0, #5
    ret
endfunction SYM(internal_call)

function SYM(written_internal_branch)
    cbz w0, LOCAL(written_target)
    adrp x8, LOCAL(written_pointer)@PAGE
    ldr x8, [x8, LOCAL(written_pointer)@PAGEOFF]
    br x8
LOCAL(written_target):
    add w0, w0, #7
    ret
endfunction SYM(written_internal_branch)

function SYM(set_internal_pointer)
    adrp x8, LOCAL(written_pointer)@PAGE
    str x0, [x8, LOCAL(written_pointer)@PAGEOFF]
    ret
endfunction SYM(set_internal_pointer)
#elif defined(__x86_64__)
#ifdef _WIN32
#define ARG %ecx
#define PTRARG %rcx
#else
#define ARG %edi
#define PTRARG %rdi
#endif
function SYM(internal_branch)
    test ARG, ARG
    jz LOCAL(branch_target)
    mov LOCAL(branch_pointer)(%rip), %rax
    jmp *%rax
LOCAL(branch_target):
    lea 3(ARG), %eax
    ret
endfunction SYM(internal_branch)

function SYM(internal_call)
    test ARG, ARG
    jz LOCAL(call_target)
    sub $8, %rsp
    mov LOCAL(call_pointer)(%rip), %rax
    call *%rax
    add $8, %rsp
    ret
LOCAL(call_target):
    lea 5(ARG), %eax
    ret
endfunction SYM(internal_call)

function SYM(written_internal_branch)
    test ARG, ARG
    jz LOCAL(written_target)
    mov LOCAL(written_pointer)(%rip), %rax
    jmp *%rax
LOCAL(written_target):
    lea 7(ARG), %eax
    ret
endfunction SYM(written_internal_branch)

function SYM(set_internal_pointer)
    mov PTRARG, LOCAL(written_pointer)(%rip)
    ret
endfunction SYM(set_internal_pointer)
#else
function SYM(internal_branch)
    mov 4(%esp), %ecx
    test %ecx, %ecx
    jz LOCAL(branch_target)
    mov LOCAL(branch_pointer), %eax
    jmp *%eax
LOCAL(branch_target):
    lea 3(%ecx), %eax
    ret
endfunction SYM(internal_branch)

function SYM(internal_call)
    mov 4(%esp), %ecx
    test %ecx, %ecx
    jz LOCAL(call_target)
    mov LOCAL(call_pointer), %eax
    call *%eax
    ret
LOCAL(call_target):
    lea 5(%ecx), %eax
    ret
endfunction SYM(internal_call)

function SYM(written_internal_branch)
    mov 4(%esp), %ecx
    test %ecx, %ecx
    jz LOCAL(written_target)
    mov LOCAL(written_pointer), %eax
    jmp *%eax
LOCAL(written_target):
    lea 7(%ecx), %eax
    ret
endfunction SYM(written_internal_branch)

function SYM(set_internal_pointer)
    mov 4(%esp), %eax
    mov %eax, LOCAL(written_pointer)
    ret
endfunction SYM(set_internal_pointer)
#endif

.data
.p2align 3
LOCAL(branch_pointer):
#ifdef __i386__
    .long LOCAL(branch_target)
#else
    .quad LOCAL(branch_target)
#endif
LOCAL(call_pointer):
#ifdef __i386__
    .long LOCAL(call_target)
#else
    .quad LOCAL(call_target)
#endif
LOCAL(written_pointer):
#ifdef __i386__
    .long LOCAL(written_target)
#else
    .quad LOCAL(written_target)
#endif
