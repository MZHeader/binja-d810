// Branchless selects through the carry flag (v4.2), as exact x86-64 code (System V: edi = x, esi = y).
// neg sets CF when its operand is non-zero; sbb r, r is then 0 or -1.
    .intel_syntax noprefix
    .text
    .globl sel_sbb, sel_adc, sel_cmp
    .type sel_sbb, @function
    .type sel_adc, @function
    .type sel_cmp, @function
// uint32_t sel_sbb(uint32_t x) == x != 0 ? 0x30 : 0x20
sel_sbb:
    mov eax, edi
    neg eax
    sbb eax, eax
    and eax, 0x10
    add eax, 0x20
    ret
// uint32_t sel_adc(uint32_t x, uint32_t y) == y + (x != 0)
sel_adc:
    mov eax, esi
    neg edi
    adc eax, 0
    ret
// uint32_t sel_cmp(uint32_t x) == x != 0: sel_sbb's select compared with its non-zero arm
sel_cmp:
    mov eax, edi
    neg eax
    sbb eax, eax
    and eax, 0x10
    add eax, 0x20
    cmp eax, 0x30
    sete al
    movzx eax, al
    ret
