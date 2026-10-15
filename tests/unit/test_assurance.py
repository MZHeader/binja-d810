"""Verification assurance (spec section 10): every verdict says how it was reached."""

import builtins

from binja_d810.ir.expr import Var
from binja_d810.ir.parse import parse
from binja_d810.semantic.linear_mba import LinearMBABackend
from binja_d810.semantic.verify import Check, Verdict, check
from binja_d810.simplify import simplify_expression


def p(text, width=32):
    return parse(text, width)


def _without_z3(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "z3":
            raise ImportError("no z3")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


POLY = "(x & y) * (x | y) + (x & ~y) * (~x & y)"


def test_linear_pairs_are_proven_by_signature():
    assert check(p("(x ^ y) + 2 * (x & y)"), p("x + y")) == Check(Verdict.PROVEN, "signature", "")


def test_non_linear_pairs_are_proven_by_z3():
    assert check(p("x * (y + 1)"), p("x * y + x")) == Check(Verdict.PROVEN, "z3", "")


def test_without_z3_a_non_linear_pair_is_only_tested(monkeypatch):
    _without_z3(monkeypatch)
    assert check(p(POLY, 8), p("x * y", 8)) == Check(Verdict.TESTED, "sampling", "z3 unavailable")


def test_disabled_z3_is_reported():
    assert check(p(POLY, 8), p("x * y", 8), use_z3=False) == Check(Verdict.TESTED, "sampling", "z3 disabled")


def test_proof_timeout_is_reported():
    hard = p("(x * y * z) ^ ((x | y) * (z & x)) ", 64)
    result = check(hard, p("((x | y) * (z & x)) ^ (z * x * y)", 64), z3_timeout_ms=1)
    assert result.verdict in (Verdict.TESTED, Verdict.PROVEN)
    if result.verdict is Verdict.TESTED:
        assert result == Check(Verdict.TESTED, "sampling", "z3 timeout")


def test_refutation_by_sampling():
    assert check(p("x + y"), p("x ^ y")) == Check(Verdict.REFUTED, "sampling", "")


def test_rewrite_carries_the_final_verdict():
    outcome = simplify_expression(p("(x ^ y) + 2 * (x & y) + z"), [], [LinearMBABackend()])
    assert outcome.rewrite is not None
    assert (outcome.rewrite.verdict, outcome.rewrite.method) == (Verdict.PROVEN, "signature")


def test_sampling_only_rewrite_is_reported_as_tested():
    outcome = simplify_expression(p(POLY, 8), [], [_Fixed(p("x * y", 8))],
                                  check=lambda a, b: check(a, b, use_z3=False))
    assert outcome.rewrite is not None
    assert (outcome.rewrite.verdict, outcome.rewrite.method, outcome.rewrite.reason) == \
        (Verdict.TESTED, "sampling", "z3 disabled")


class _Fixed:
    name = "fixed"

    def __init__(self, result):
        self.result = result

    def simplify(self, e, deadline=None):
        return self.result


def test_refuted_candidates_are_counted_and_never_applied():
    outcome = simplify_expression(p("(x ^ y) + 2 * (x & y)"), [], [_Fixed(Var("x", 32))])
    assert outcome.rewrite is None
    # Tier 2 offers the wrong answer for the whole expression and again for a subtree.
    assert outcome.refuted == 2 and all("refuted" in message for _, message in outcome.errors)


def test_legacy_verdict_only_check_still_works():
    outcome = simplify_expression(p("(x ^ y) + 2 * (x & y)"), [], [LinearMBABackend()],
                                  check=lambda a, b: Verdict.TESTED)
    assert outcome.rewrite is not None and outcome.rewrite.verdict is Verdict.TESTED


def test_linear_identities_written_with_shifts_and_extensions_are_proven():
    # HLIL on x64: (y ^ x) + (trunc32(zx64(y) & zx64(x)) << 1) is linear once normalized.
    from binja_d810.ir.expr import BinOp, Const, UnOp

    x, y = parse("x", 32), parse("y", 32)
    wide_and = BinOp("and", UnOp("zx", y, 64), UnOp("zx", x, 64), 64)
    e = BinOp("add", BinOp("xor", y, x, 32), BinOp("shl", UnOp("trunc", wide_and, 32), Const(1, 8), 32), 32)
    assert check(e, p("x + y")) == Check(Verdict.PROVEN, "signature", "")
