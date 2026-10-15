// Partial-register fixtures (spec section 6.1) as exact x86-64 code (System V: edi = x, esi = y).
// Each computes the MBA (x ^ y) + 2 * (x & y) == x + y on part of a register, keeping the rest.
    .intel_syntax noprefix
    .text
    .globl part_al, part_ah, part_ax, part_eax_zx, part_overlap, part_sx
    .type part_al, @function
    .type part_ah, @function
    .type part_ax, @function
    .type part_eax_zx, @function
    .type part_overlap, @function
    .type part_sx, @function
// uint8_t part_al(uint8_t x, uint8_t y) == (x + y) & 0xff
part_al:
    mov eax, edi
    xor al, sil
    mov ecx, edi
    and cl, sil
    add cl, cl
    add al, cl
    ret
// uint32_t part_ah(uint32_t x, uint32_t y) == (x & 0xffff00ff) | ((((x >> 8) + (y >> 8)) & 0xff) << 8)
part_ah:
    mov eax, edi
    mov ecx, esi
    mov edx, edi
    xor ah, ch
    and dh, ch
    add dh, dh
    add ah, dh
    ret
// uint32_t part_ax(uint32_t x, uint32_t y) == (x & 0xffff0000) | ((x + y) & 0xffff)
part_ax:
    mov eax, edi
    xor ax, si
    mov ecx, edi
    and cx, si
    add cx, cx
    add ax, cx
    ret
// uint64_t part_eax_zx(uint64_t x, uint64_t y) == (x + y) & 0xffffffff (32-bit ops zero-extend)
part_eax_zx:
    mov eax, edi
    xor eax, esi
    mov ecx, edi
    and ecx, esi
    lea eax, [rax + 2*rcx]
    ret
// uint32_t part_overlap(uint32_t x, uint32_t y) == ((x & 0xffffff00) | ((x + y) & 0xff)) + y
part_overlap:
    mov eax, edi
    mov ecx, edi
    xor cl, sil
    mov edx, edi
    and dl, sil
    add dl, dl
    add cl, dl
    mov al, cl
    add eax, esi
    ret
// int32_t part_sx(int8_t x, int8_t y) == sign-extended (x + y) & 0xff
part_sx:
    mov eax, edi
    xor al, sil
    mov ecx, edi
    and cl, sil
    add cl, cl
    add al, cl
    movsx eax, al
    ret
