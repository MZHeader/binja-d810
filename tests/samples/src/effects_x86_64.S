// Effect-policy fixtures (spec section 5.2) as exact x86-64 code: an MBA that is 0 feeds an
// instruction with a memory operand, so the candidate root contains the load.
    .intel_syntax noprefix
    .text
    .globl eff_load_and_zero, eff_load_add
    .type eff_load_and_zero, @function
    .type eff_load_add, @function
// uint32_t eff_load_and_zero(uint32_t x, uint32_t y, const uint32_t *p): ((x^y) - (x|y) + (x&y)) & *p
eff_load_and_zero:
    mov eax, edi
    xor eax, esi
    mov ecx, edi
    or ecx, esi
    sub eax, ecx
    mov ecx, edi
    and ecx, esi
    add eax, ecx
    and eax, [rdx]
    ret
// uint32_t eff_load_add(uint32_t x, uint32_t y, const uint32_t *p): ((x^y) + 2*(x&y)) + *p
eff_load_add:
    mov eax, edi
    xor eax, esi
    mov ecx, edi
    and ecx, esi
    lea eax, [rax + 2*rcx]
    add eax, [rdx]
    ret
