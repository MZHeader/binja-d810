"""Shim for d810.hexrays.hexrays_helpers. "mop" values are binja_d810 IR expressions."""

from binja_d810.ir.expr import Const, UnOp, mask
from binja_d810.ir.normalize import normalize


def equal_mops_ignore_size(a, b) -> bool:
    if isinstance(a, Const) and isinstance(b, Const):
        return a.value == b.value
    return normalize(a) == normalize(b)


def equal_bnot_mop(a, b) -> bool:
    if isinstance(a, Const) and isinstance(b, Const):
        return a.width == b.width and a.value == (~b.value & mask(b.width))
    return normalize(a) == normalize(UnOp("not", b, b.width))
