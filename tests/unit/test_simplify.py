import time

import pytest

from binja_d810.ir.expr import BinOp, Var, is_mba, node_count, variables
from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse
from binja_d810.rules.base import Rewrite
from binja_d810.rules.registry import load_rules
from binja_d810.semantic.backend import default_backends
from binja_d810.semantic.linear_mba import LinearMBABackend
from binja_d810.semantic.verify import Verdict, check_equivalent
from binja_d810.simplify import Limits, simplify_expression

RULES = load_rules()
LINEAR = [LinearMBABackend()]


def ir(text, width=32):
    return parse(text, width)


def test_tier1_rule_fires():
    out = simplify_expression(ir("(a | b) - (a & b)"), RULES, [])
    assert out.rewrite.replacement == normalize(ir("a ^ b"))
    assert out.rewrite.source and not out.rewrite.source.startswith("tier2:")


def test_tier2_handles_what_no_pattern_matches():
    e = ir("(x ^ y) + 2 * (x & y) + 3 * ((x | y) - (x & ~y) - y)")
    assert simplify_expression(e, [], []).rewrite is None
    out = simplify_expression(e, [], LINEAR)
    assert out.rewrite is not None and out.rewrite.source == "tier2:linear"
    assert check_equivalent(e, out.rewrite.replacement) is Verdict.PROVEN


def test_mba_nested_in_non_linear_context():
    e = ir("((a ^ b) + 2 * (a & b)) * c")
    out = simplify_expression(e, RULES, LINEAR)
    assert out.rewrite is not None and not is_mba(out.rewrite.replacement)


def test_non_mba_is_untouched():
    assert simplify_expression(ir("a + b * 3"), RULES, LINEAR).rewrite is None


def test_variable_limit_skips_tier2_only():
    called = []

    class Spy:
        name = "spy"

        def simplify(self, e, deadline=None):
            called.append(e)
            return None

    e = ir("(a ^ b) + (c ^ d) + (e ^ f) + 2 * (a & b)")
    out = simplify_expression(e, [], [Spy()], Limits(max_vars=4))
    assert called and all(len(variables(x)) <= 4 for x in called)
    assert out.skipped and out.skipped.startswith("too complex")


def test_failing_rule_is_isolated():
    class Exploding:
        name = "Exploding"

        def match(self, e):
            raise RuntimeError("boom")

    out = simplify_expression(ir("(a | b) - (a & b)"), [Exploding(), *RULES], [])
    assert ("Exploding", "RuntimeError: boom") in out.errors
    assert out.rewrite is not None


def test_refuted_backend_result_is_rejected():
    class Liar:
        name = "liar"

        def simplify(self, e, deadline=None):
            return Var("a", e.width)

    out = simplify_expression(ir("(a ^ b) + 2 * (a & b)"), [], [Liar()])
    assert out.rewrite is None
    assert any(source == "tier2:liar" for source, _ in out.errors)


def test_huge_expressions_are_skipped_quickly():
    e = ir("(a ^ b) + 2 * (a & b)")
    for _ in range(9):  # balanced doubling: ~5000 nodes, shallow
        e = BinOp("add", e, e, 32)
    start = time.perf_counter()
    out = simplify_expression(e, RULES, LINEAR)
    assert time.perf_counter() - start < 1.0
    assert out.rewrite is None and out.skipped.startswith("too complex")


def test_deep_chain_does_not_recurse():
    e = Var("a", 32)
    for i in range(3000):
        e = BinOp("xor" if i % 2 else "add", e, Var("b", 32), 32)
    out = simplify_expression(e, RULES, LINEAR)
    assert out.skipped.startswith("too complex")


def test_tier2_time_budget_bounds_slow_backends():
    # Within the node limit, a failing backend is retried on every nested MBA subtree;
    # with CoBRA on a 199-node chain that was 98 calls and ~31 s without a budget.
    class Slow:
        name = "slow"

        def simplify(self, e, deadline=None):
            time.sleep(0.05)
            return None

    e = Var("a", 32)
    for i in range(60):
        e = BinOp(("add", "xor")[i % 2], e, Var("b", 32), 32)
    start = time.perf_counter()
    out = simplify_expression(e, [], [Slow()], Limits(tier2_seconds=0.2))
    assert time.perf_counter() - start < 0.6
    assert out.skipped and out.skipped.startswith("too complex") and "time budget" in out.skipped


def test_small_rewrites_of_ordinary_code_are_not_applied():
    # CRC's branchless mask: CoBRA returns C * (crc & 1), correct and one node smaller (6 -> 5)
    # but still mixed. Results must remove the mix or shrink the expression by at least 25%.
    crc = ir("0xedb88320 & (0 - (crc & 1))")
    cobra_answer = normalize(ir("0xedb88320 * (crc & 1)"))

    class CobraLike:
        name = "cobra"

        def simplify(self, e, deadline=None):
            return cobra_answer if e == normalize(crc) else None

    assert simplify_expression(crc, [], [CobraLike()]).rewrite is None


def test_normalization_alone_is_not_a_rewrite():
    # Ordinary code like CRC's 0u - (crc & 1u) must stay untouched, and canonical forms such as
    # x + ~~y already read cleanly in Binary Ninja's HLIL; only rules and Tier 2 rewrite.
    assert simplify_expression(ir("0 - (crc & 1)"), [], []).rewrite is None
    assert simplify_expression(ir("x + ~~y"), [], []).rewrite is None


def test_equal_size_result_that_is_no_longer_mba_is_accepted():
    # PredOr2_Rule_1: 3 & ~(x * x) -> 2 | (1 & ~x), both 6 nodes, but the multiplication is gone.
    out = simplify_expression(ir("3 & ~(x * x)"), RULES, [])
    assert out.rewrite is not None and not is_mba(out.rewrite.replacement)


def test_rules_that_undo_each_other_do_not_starve_other_rewrites():
    # BnotOr_FactorRule_1 and CstSimplificationRule17 swap 3 & ~(x * x) and ~(-4 | x * x); left
    # unchecked they used up the whole rewrite budget before the right-hand side got its turn.
    by_name = {r.name: r for r in RULES}
    add_rule = next(r for r in RULES if r.match(normalize(ir("(a ^ b) + 2 * (a & b)"))))
    rules = [by_name["BnotOr_FactorRule_1"], by_name["CstSimplificationRule17"], add_rule]
    e = ir("(3 & ~(x * x)) - ((a ^ b) + 2 * (a & b))")
    out = simplify_expression(e, rules, [])
    assert out.rewrite is not None
    assert node_count(out.rewrite.replacement) < node_count(normalize(e))


def test_tier2_output_is_simplified_further():
    poly = ir("(x & y) * (x | y) + (x & ~y) * (~x & y) + (x | y) - (x & y)")
    partial = normalize(ir("(-2) * (x & y) + (y + (x + x * y))"))  # what CoBRA returns for poly

    class Partial:
        name = "partial"

        def simplify(self, e, deadline=None):
            correct = check_equivalent(e, partial, use_z3=False) is not Verdict.REFUTED
            return partial if correct and node_count(partial) < node_count(e) else None

    out = simplify_expression(poly, [], [Partial(), LinearMBABackend()])
    assert out.rewrite is not None
    assert node_count(out.rewrite.replacement) <= node_count(normalize(ir("x * y + (x ^ y)")))


def test_result_is_always_smaller():
    for text in ["(a | b) - (a & b)", "(x ^ y) + 2 * (x & y)", "(x & y) + (x & ~y)"]:
        out = simplify_expression(ir(text), RULES, LINEAR)
        assert node_count(out.rewrite.replacement) < node_count(ir(text))


def test_normalization_that_removes_the_mix_is_a_rewrite():
    # i686 -O2 HLIL: 2 * ((x | z) + (y | z) - ((z | x) + (z | y))) + (x | y): the two sums are the same
    # once canonicalized, so normalization alone leaves x | y. That is deobfuscation, not churn.
    outcome = simplify_expression(ir("2 * ((x | z) + (y | z) - ((z | x) + (z | y))) + (x | y)"), [], [])
    assert outcome.rewrite is not None and outcome.rewrite.source == "normalize"
    assert outcome.rewrite.replacement == normalize(ir("x | y"))


def test_normalization_that_keeps_the_mix_is_still_not_a_rewrite():
    assert simplify_expression(ir("(x & y) + ~~z"), [], []).rewrite is None


def test_linear_mba_over_atoms_is_simplified_beyond_the_variable_limit():
    # Six leaf variables, two atoms: the variable limit is CoBRA's; the linear backend bounds its
    # own atom count.
    e = ir("((a + b + c) ^ (d - e + f)) + 2 * (~(a + b + c) | (d - e + f))")
    out = simplify_expression(e, [], LINEAR, Limits(max_vars=4))
    assert out.rewrite is not None and not is_mba(out.rewrite.replacement)
    assert out.rewrite.verdict is Verdict.PROVEN


def test_linear_mba_over_nested_sums_is_fully_simplified():
    # The root has too many atoms until its children are simplified: each (u ^ v) + 2 * (u & v)
    # is u + v, after which the root is a linear MBA over three sums.
    e = ir("(((a ^ b) + 2 * (a & b)) ^ ((c ^ d) + 2 * (c & d))) + 2 * (((a ^ b) + 2 * (a & b)) & ((c ^ d) + 2 * (c & d)))"
           " + ((e ^ f) + 2 * (e & f)) + ((g ^ h) + 2 * (g & h)) - ((e | g) - (e & ~g))")
    out = simplify_expression(e, [], LINEAR, Limits(max_vars=4))
    assert out.rewrite is not None and not is_mba(out.rewrite.replacement), out


def test_tier2_steps_are_proven_once_at_the_end(monkeypatch):
    # Each accepted Tier 2 step is checked by sampling (and signatures) only: the final check of
    # original against result is the proof, so Z3 runs once, not once per step.
    from binja_d810.semantic import verify

    calls = []
    real = verify._z3_prove
    monkeypatch.setattr(verify, "_z3_prove", lambda *a, **k: calls.append(1) or real(*a, **k))
    e = ir("(((x + 1) ^ x) & 1) + y")  # y + 1: (x + 1) ^ x always has bit 0 set

    class Oracle:
        name = "oracle"

        def simplify(self, node, deadline=None):
            return normalize(ir("y + 1")) if node == normalize(e) else None

    out = simplify_expression(e, [], [Oracle()])
    assert out.rewrite is not None and out.rewrite.verdict is Verdict.PROVEN
    assert len(calls) == 1


def test_other_backends_are_not_asked_about_linear_mbas_the_linear_backend_could_not_shrink():
    # The linear backend decides linear MBAs by their signature; on 102 such real-sample queries
    # CoBRA never found anything smaller and spent 1.6 s.
    called = []

    class Spy:
        name = "spy"

        def simplify(self, e, deadline=None):
            called.append(e)
            return None

    out = simplify_expression(ir("(a & b) + c"), [], [LinearMBABackend(), Spy()])
    assert out.rewrite is None and called == []
    simplify_expression(ir("(a * b) ^ (a + c)"), [], [LinearMBABackend(), Spy()])
    assert called  # not a linear MBA: other backends still run


def test_tier1_results_are_reused_through_a_shared_cache():
    calls = []

    class Counting:
        name = "counting"

        def match(self, e):
            calls.append(e)
            return None

    cache: dict = {}
    e = ir("(a ^ b) + 2 * (a & b)")
    first = simplify_expression(e, [Counting()], LINEAR, tier1_cache=cache)
    seen = len(calls)
    second = simplify_expression(e, [Counting()], LINEAR, tier1_cache=cache)
    assert seen > 0 and len(calls) == seen
    assert first.rewrite.replacement == second.rewrite.replacement


def test_cached_tier1_results_carry_the_references_of_the_expression_asked_about():
    from binja_d810.ir.expr import substitute

    old, new = object(), object()
    cache: dict = {}
    e = ir("(a | b) - (a & b)")
    simplify_expression(substitute(e, {"a": Var("a", 32, ref=old)}), RULES, [], tier1_cache=cache)
    out = simplify_expression(substitute(e, {"a": Var("a", 32, ref=new)}), RULES, [], tier1_cache=cache)
    assert next(v for v in variables(out.rewrite.replacement) if v.name == "a").ref is new


@pytest.mark.parametrize("width", [8, 16, 32, 64])
def test_xor_cancellation_simplifies(width):
    out = simplify_expression(ir("x ^ (y ^ x)", width), RULES, LINEAR)
    assert out.rewrite is not None and out.rewrite.replacement == ir("y", width)


@pytest.mark.parametrize("width", [8, 32])
def test_one_bit_substitution_simplifies(width):
    out = simplify_expression(ir("(x ^ (y ^ 1)) & x & 1", width), RULES, LINEAR)
    assert out.rewrite is not None and node_count(out.rewrite.replacement) <= 5
    assert check_equivalent(out.rewrite.original, out.rewrite.replacement) is Verdict.PROVEN


def test_plain_mask_is_left_alone():
    assert simplify_expression(ir("(x & 0xff00) ^ y"), RULES, LINEAR).rewrite is None


def test_constant_interaction_simplifies_through_default_backends():
    out = simplify_expression(ir("(~x & 0x55) | (x & 0xaa)"), RULES, default_backends())
    assert out.rewrite is not None and node_count(out.rewrite.replacement) <= 5  # (x ^ 0x55) & 0xff


class Mixing:
    name = "mixing"

    def simplify(self, e, deadline=None):
        return ir("255 + (0xff00 & x)")


def test_tier2_never_mixes_a_bitwise_root():
    # Equivalent and smaller, but arithmetic where there was none: CoBRA's answer to this one.
    out = simplify_expression(ir("(x & 0xff00) | ((x | 0xff) & 0xf0ff)"), [], [Mixing()])
    assert out.rewrite is None or not is_mba(out.rewrite.replacement)
