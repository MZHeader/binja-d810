// Partial-register fixtures (spec section 6.1) for 32-bit x86 (cdecl: x at [esp+4], y at [esp+8]).
    .intel_syntax noprefix
    .text
    .globl part32_al, part32_ah
    .type part32_al, @function
    .type part32_ah, @function
// uint8_t part32_al(uint8_t x, uint8_t y) == (x + y) & 0xff
part32_al:
    mov eax, [esp + 4]
    mov ecx, [esp + 8]
    mov edx, eax
    xor al, cl
    and dl, cl
    add dl, dl
    add al, dl
    ret
// uint32_t part32_ah(uint32_t x, uint32_t y) == (x & 0xffff00ff) | ((((x >> 8) + (y >> 8)) & 0xff) << 8)
part32_ah:
    mov eax, [esp + 4]
    mov ecx, [esp + 8]
    mov edx, eax
    xor ah, ch
    and dh, ch
    add dh, dh
    add ah, dh
    ret
