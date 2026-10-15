// Shift-boundary fixtures (spec section 6.5): ARM64 variable shifts take the count modulo the
// register width (LSLV/LSRV/ASRV).
    .text
    .globl _lsl64_by65, _lsr32_by32, _lsl32_var, _lsr32_var, _asr32_var, _lsl64_var, _lsl32_by33, _lsr64_by65, _asr32_by32
    .p2align 2
_lsl32_var:  lsl w0, w0, w1
             ret
_lsr32_var:  lsr w0, w0, w1
             ret
_asr32_var:  asr w0, w0, w1
             ret
_lsl64_var:  lsl x0, x0, x1
             ret
_lsl32_by33: mov w0, #1
             mov w1, #33
             lsl w0, w0, w1
             ret
_lsr64_by65: mov x0, #0x8000000000000000
             mov x1, #65
             lsr x0, x0, x1
             ret
_asr32_by32: mov w0, #0x80000000
             mov w1, #32
             asr w0, w0, w1
             ret
_lsl64_by65: mov x0, #1
             mov x1, #65
             lsl x0, x0, x1
             ret
_lsr32_by32: mov w0, #0x80000000
             mov w1, #32
             lsr w0, w0, w1
             ret
