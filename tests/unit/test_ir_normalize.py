import random

from exprgen import random_expr

from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import BinOp, Const, UnOp, Var
from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse

a, b = Var("a", 32), Var("b", 32)


def n(text, width=32, widths=None):
    return normalize(parse(text, width, widths))


def test_shift_by_constant_becomes_multiply():
    assert n("a << 3") == BinOp("mul", Const(8, 32), a, 32)


def test_not_neg_canonical_forms():
    assert n("a ^ -1") == UnOp("not", a, 32)
    assert n("0 - a") == UnOp("neg", a, 32)
    assert n("-1 * a") == UnOp("neg", a, 32)
    assert n("~~a") == a


def test_add_sub_with_negatives():
    assert n("a + -5") == BinOp("sub", a, Const(5, 32), 32)
    assert n("a - -5") == BinOp("add", Const(5, 32), a, 32)
    assert n("a + -b") == BinOp("sub", a, b, 32)


def test_identities_and_folding():
    assert n("(a & -1) | 0") == a
    assert n("a ^ a") == Const(0, 32)
    assert n("a - a") == Const(0, 32)
    assert n("3 * 7 + 1") == Const(22, 32)


def test_commutative_ordering_puts_constants_first():
    assert n("a + 1") == BinOp("add", Const(1, 32), a, 32)
    assert n("b & a") == n("a & b")


def test_truncation_pushdown():
    assert n("trunc32(zx64(a) + zx64(b))", 32, {"a": 32, "b": 32}) == BinOp("add", a, b, 32)
    assert n("trunc16(sx64(a))", 16, {"a": 32}) == UnOp("trunc", a, 16)


def test_normalize_is_idempotent_and_exact():
    rng = random.Random(7)
    ops = ("add", "sub", "mul", "and", "or", "xor", "neg", "not", "shl")
    for _ in range(300):
        e = random_expr(rng, ops=ops)
        once = normalize(e)
        assert normalize(once) == once
        for _ in range(10):
            env = {name: rng.getrandbits(32) for name in "abc"}
            assert evaluate(once, env) == evaluate(e, env)


def test_normalize_preserves_semantics_at_8_and_16_bits():
    rng = random.Random(8)
    for width in (8, 16):
        for _ in range(150):
            inner = random_expr(rng, width=32)
            narrow = UnOp("trunc", inner, width)
            once = normalize(narrow)
            assert once.width == width
            for _ in range(10):
                env = {name: rng.getrandbits(32) for name in "abc"}
                assert evaluate(once, env) == evaluate(narrow, env)


def test_constants_of_one_associative_operation_are_combined():
    # x86 masks a shift count (cl & 0x1f) that the source already masked: (n & 31) & 31.
    for text, expected in [("((32 - r) & 31) & 31", "(32 - r) & 31"), ("(a | 3) | 12", "a | 15"),
                           ("(a ^ 5) ^ 5", "a"), ("(a + 3) + 4", "a + 7"), ("(a * 3) * 5", "a * 15")]:
        assert normalize(parse(text, 32)) == normalize(parse(expected, 32)), text


def test_redundant_shift_count_mask_is_not_worth_a_rewrite():
    # Ordinary rotate code on i686 -O0 (regression.c reg_rotl): only the mask changes.
    from binja_d810.semantic.backend import default_backends
    from binja_d810.simplify import simplify_expression

    e = parse("x >> (((32 - r) & 31) & 31)", 32)
    assert simplify_expression(e, [], default_backends()).rewrite is None
