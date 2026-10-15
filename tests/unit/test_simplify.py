import time

from binja_d810.ir.expr import BinOp, Var, is_mba, node_count, variables
from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse
from binja_d810.rules.base import Rewrite
from binja_d810.rules.registry import load_rules
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

        def simplify(self, e):
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

        def simplify(self, e):
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

        def simplify(self, e):
            time.sleep(0.05)
            return None

    e = Var("a", 32)
    for i in range(60):
        e = BinOp(("add", "xor")[i % 2], e, Var("b", 32), 32)
    start = time.perf_counter()
    out = simplify_expression(e, [], [Slow()], Limits(tier2_seconds=0.2))
    assert time.perf_counter() - start < 0.6
    assert out.skipped and out.skipped.startswith("too complex") and "time budget" in out.skipped


def test_result_is_always_smaller():
    for text in ["(a | b) - (a & b)", "(x ^ y) + 2 * (x & y)", "(x & y) + (x & ~y)"]:
        out = simplify_expression(ir(text), RULES, LINEAR)
        assert node_count(out.rewrite.replacement) < node_count(ir(text))
