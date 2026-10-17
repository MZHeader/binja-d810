from binja_d810.deadline import Deadline
from binja_d810.ir.expr import Const
from binja_d810.ir.parse import parse
from binja_d810.semantic.constancy import constant_value
from binja_d810.semantic.verify import Verdict
from binja_d810.simplify import simplify_expression


def c(text, width=1):
    return parse(text, width, {"x": 32, "y": 32, "z": 32, "b": 8})


def test_parity_and_mba_equalities_are_proven_constant():
    for text, value in [("eq((x * (x + 1)) & 1, 0)", 1), ("ne((x ^ y) + 2 * (x & y), x + y)", 0),
                        ("ne(x | 16, 15)", 1), ("ult(x & 7, 8)", 1), ("ne((x * x) & 3, 2)", 1)]:
        value_and_check = constant_value(c(text), Deadline.after(10))
        assert value_and_check is not None, text
        assert value_and_check[0] == value and value_and_check[1].verdict is Verdict.PROVEN, (text, value_and_check)


def test_non_constant_conditions_are_not_constant():
    for text in ["eq(x & 1, 0)", "ugt(x, 5)", "eq(x | 1, 3)", "ne((x * y) & 1, 0)"]:
        assert constant_value(c(text), Deadline.after(10)) is None, text


def test_a_condition_constant_on_samples_but_not_always_is_never_folded():
    # Random 32-bit samples almost never hit x & 0xffff == 0x1234; a proof (or a model) decides.
    out = simplify_expression(c("ne(x & 0xffff, 0x1234)"), [], [])
    assert out.rewrite is None


def test_small_inputs_are_proven_exhaustively():
    value_and_check = constant_value(parse("ult(zx32(b) & 7, 8)", 1, {"b": 8}), Deadline.after(10))
    assert value_and_check[0] == 1 and value_and_check[1].verdict is Verdict.PROVEN


def test_simplify_folds_proven_constant_conditions_and_values():
    out = simplify_expression(c("eq((x * (x + 1)) & 1, 0)"), [], [])
    assert out.rewrite is not None and out.rewrite.replacement == Const(1, 1)
    assert "constancy" in out.rewrite.source and out.rewrite.verdict is Verdict.PROVEN
    value = simplify_expression(c("(x * (x - 1)) & 1", 32), [], [])
    assert value.rewrite is not None and value.rewrite.replacement == Const(0, 32)


def test_an_unproven_constant_is_reported_and_left_alone():
    from binja_d810.semantic.verify import Check

    out = simplify_expression(c("eq((x * (x + 1)) & 1, 0)"), [], [],
                              check=lambda a, b: Check(Verdict.TESTED, "sampling", "z3 timeout"))
    assert out.rewrite is None and out.skipped == "constant on every sample, not proven"


def test_conditions_decided_by_form_or_bounds_are_folded_and_proven():
    for text, value in [("eq(x, x)", 1), ("ne(x, x)", 0), ("ugt(zx32(trunc8(x)), 0xff)", 0),
                        ("ule(zx32(trunc8(x)), 0xff)", 1), ("uge(zx32(b), 0x100)", 0)]:
        out = simplify_expression(c(text), [], [])
        assert out.rewrite is not None and out.rewrite.replacement == Const(value, 1), (text, out)
        assert out.rewrite.verdict is Verdict.PROVEN, (text, out.rewrite)
