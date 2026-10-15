// Shift-boundary fixtures (spec section 6.5). The functions with constants let Binary Ninja's own
// dataflow compute the result; the *_var functions expose the lifted count expression.
    .intel_syntax noprefix
    .text
    .type shl8_imm9, @function
    .type shl16_imm17, @function
    .type shl32_imm33, @function
    .type sar8_imm9, @function
    .type shr8_imm9, @function
    .type shl8_var, @function
    .type shr8_var, @function
    .type sar8_var, @function
    .type shl32_var, @function
    .type shl64_var, @function
    .type shl8_by8, @function
    .type shl32_by33, @function
    .type shl64_by65, @function
    .type sar8_by8, @function
    .type shr16_by16, @function
    .globl shl8_imm9, shl16_imm17, shl32_imm33, sar8_imm9, shr8_imm9, shl8_var, shr8_var, sar8_var, shl32_var, shl64_var, shl8_by8, shl32_by33, shl64_by65, sar8_by8, shr16_by16
shl8_var:   mov eax, edi
            mov ecx, esi
            shl al, cl
            ret
shr8_var:   mov eax, edi
            mov ecx, esi
            shr al, cl
            ret
sar8_var:   mov eax, edi
            mov ecx, esi
            sar al, cl
            ret
shl32_var:  mov eax, edi
            mov ecx, esi
            shl eax, cl
            ret
shl64_var:  mov rax, rdi
            mov ecx, esi
            shl rax, cl
            ret
shl8_by8:   mov eax, 1
            mov ecx, 8
            shl al, cl
            ret
shl32_by33: mov eax, 1
            mov ecx, 33
            shl eax, cl
            ret
shl64_by65: mov eax, 1
            mov ecx, 65
            shl rax, cl
            ret
sar8_by8:   mov eax, 0x80
            mov ecx, 8
            sar al, cl
            ret
shr16_by16: mov eax, 0x8000
            mov ecx, 16
            shr ax, cl
            ret
shl8_imm9:  mov eax, 1
            shl al, 9
            ret
shl16_imm17: mov eax, 1
            shl ax, 17
            ret
shl32_imm33: mov eax, 1
            shl eax, 33
            ret
sar8_imm9:  mov eax, 0x80
            sar al, 9
            ret
shr8_imm9:  mov eax, 0x80
            shr al, 9
            ret
