from binja_d810.deadline import Deadline
from binja_d810.ir.expr import Const, to_str
from binja_d810.ir.normalize import normalize
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


def test_equality_with_a_small_value_is_not_constant_on_samples():
    # Random 32-bit z almost never equals a 3-bit value; small and equal samples do (a loop
    # condition in predicates.c: ((i ^ x) & 7) != y).
    from binja_d810.semantic.constancy import sampled_constant

    for text in ["eq((x ^ y) & 7, z)", "ne((x ^ y) & 7, z)", "ult(x & 0xff, y)", "eq(x & y, z)", "eq(x | y, 0)",
                 "eq(x & y, (-1))"]:
        assert sampled_constant(c(text)) is None, text


def test_a_refuted_constant_falls_back_to_the_simplified_condition():
    # Every sample says 1 (random sums almost never hit 0x1234); the proof refutes that, and the
    # linear backend's x + y still stands, proven.
    from binja_d810.semantic.linear_mba import LinearMBABackend

    out = simplify_expression(c("ne(((x ^ y) + 2 * (x & y)) & 0xffff, 0x1234)"), [], [LinearMBABackend()])
    assert out.rewrite is not None and out.rewrite.verdict is Verdict.PROVEN, out
    assert out.rewrite.replacement == normalize(c("ne((x + y) & 0xffff, 0x1234)")), to_str(out.rewrite.replacement)
    assert "constancy" not in out.rewrite.source and not out.errors, out


def _bcf_condition(depth=30):
    """((t ^ K) & 1) == 0 with t the 30-step chain over x * (x - 1): always 1, 2^30 tree nodes."""
    from test_dag import MASKS, chain

    from binja_d810.ir.expr import BinOp

    k = 0
    for m in MASKS[:depth]:
        k ^= ~m & 0xFFFFFFFF
    t = BinOp("xor", chain(depth), Const(k, 32), 32)
    return BinOp("eq", BinOp("and", t, Const(1, 32), 32), Const(0, 32), 1)


def test_constants_over_shared_values_are_proven_without_expanding_the_tree():
    import time

    from binja_d810.ir.expr import BinOp
    from binja_d810.semantic.constancy import fold_shared

    started = time.perf_counter()
    found = fold_shared(_bcf_condition(), Deadline.after(10))
    assert found is not None and found[0] == 1 and found[1].verdict is Verdict.PROVEN, found
    varying = BinOp("eq", BinOp("and", _bcf_condition().left.left, Const(2, 32), 32), Const(0, 32), 1)
    assert fold_shared(varying, Deadline.after(10)) is None
    assert time.perf_counter() - started < 3


def test_an_unproven_constant_is_not_proven_again_in_the_same_function_run():
    # x-tunnel.bin 0x42a673: a 17-node condition spent its 2 s proof in both rounds of a pass.
    from binja_d810.semantic.verify import Check

    calls = []

    def check(a, b):
        calls.append(b)
        return Check(Verdict.TESTED, "sampling", "z3 timeout")

    cache: dict = {}
    for _ in range(3):
        out = simplify_expression(c("eq((x * (x + 1)) & 1, 0)"), [], [], check=check, tier1_cache=cache)
        assert out.rewrite is None and out.skipped == "constant on every sample, not proven", out
    assert len(calls) == 1, calls


def test_fold_shared_evaluates_a_variable_free_expression():
    # A per-path variant whose merged values were all constants (spec v3.2 goal 3): exact by evaluation.
    from binja_d810.semantic.constancy import fold_shared
    from binja_d810.ir.expr import BinOp, Const

    found = fold_shared(BinOp("xor", Const(1, 1), Const(0, 1), 1), Deadline.never())
    assert found is not None and found[0] == 1 and found[1].verdict is Verdict.PROVEN


def test_fold_shared_samples_a_few_dozen_times_before_the_proof(monkeypatch):
    # v3.3: sampling only guesses the value (and rejects ordinary roots after a sample or two); the proof
    # decides. snagit.dll's sub_180009da0 spent 13.5 of 20 s evaluating shared values 277 times each.
    from binja_d810.semantic import constancy

    samples = []
    real = constancy.evaluate_shared_batch
    monkeypatch.setattr(constancy, "evaluate_shared_batch",
                        lambda e, envs: (samples.append(len(envs)), real(e, envs))[1])
    found = constancy.fold_shared(_bcf_condition(), Deadline.never())
    assert found is not None and found[1].verdict is Verdict.PROVEN
    assert 0 < sum(samples) <= 64, samples
