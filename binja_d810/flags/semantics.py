"""Integer subtraction flags. Carry polarity is the only architecture-specific rule here."""

from ..ir.expr import BinOp, Const, UnOp
from ..ir.normalize import normalize

# IL architecture names, not instruction encodings or sample signatures.
CARRY_IS_NO_BORROW = {"aarch64": True, "x86_64": False, "x86": False}


def bit(op, a, b):
    return BinOp(op, a, b, 1)


def sign(value):
    return bit("slt", value, Const(0, value.width))


def add_overflow(a, b):
    result = BinOp("add", a, b, a.width)
    return bit("and", bit("eq", sign(a), sign(b)), bit("ne", sign(a), sign(result)))


def subtract_overflow_bits(a, b):
    difference = BinOp("sub", a, b, a.width)
    changed = BinOp("and", BinOp("xor", a, b, a.width), BinOp("xor", a, difference, a.width), a.width)
    return bit("ne", BinOp("and", changed, Const(1 << (a.width - 1), a.width), a.width), Const(0, a.width))


def subtract_overflow_wide(a, b):
    wide = a.width + 1
    exact = BinOp("sub", UnOp("sx", a, wide), UnOp("sx", b, wide), wide)
    truncated = UnOp("sx", BinOp("sub", a, b, a.width), wide)
    return bit("ne", exact, truncated)


def subtraction(a, b, architecture):
    difference = BinOp("sub", a, b, a.width)
    return {
        "n": sign(difference), "z": bit("eq", a, b),
        "v": bit("and", bit("ne", sign(a), sign(b)), bit("ne", sign(a), sign(difference))),
        "c": bit("uge" if CARRY_IS_NO_BORROW[architecture] else "ult", a, b),
    }


def condition(flags, comparison, architecture):
    n, z, v, c = (flags[k] for k in ("n", "z", "v", "c"))
    less = UnOp("not", c, 1) if CARRY_IS_NO_BORROW[architecture] else c
    signed = bit("ne", n, v)
    return {"eq": z, "ne": UnOp("not", z, 1),
            "slt": signed, "sle": bit("or", z, signed),
            "sge": UnOp("not", signed, 1), "sgt": UnOp("not", bit("or", z, signed), 1),
            "ult": less, "ule": bit("or", less, z),
            "uge": UnOp("not", less, 1), "ugt": UnOp("not", bit("or", less, z), 1)}[comparison]


def choose(guard, yes, no):
    """A boolean select, kept in the existing fixed-width IR."""
    if guard.width != 1 or yes.width != 1 or no.width != 1:
        raise ValueError("flag selection requires boolean values")
    if yes == no:
        return yes
    if no == Const(0, 1):
        return normalize(bit("and", guard, yes))
    if no == Const(1, 1):
        return normalize(bit("or", UnOp("not", guard, 1), yes))
    if yes == Const(1, 1):
        return normalize(bit("or", guard, no))
    if yes == Const(0, 1):
        return normalize(bit("and", UnOp("not", guard, 1), no))
    return normalize(bit("or", bit("and", guard, yes), bit("and", UnOp("not", guard, 1), no)))
