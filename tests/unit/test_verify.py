import builtins
import random

import z3
from exprgen import DEFAULT_OPS, random_expr

from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import to_str, variables
from binja_d810.ir.parse import parse
from binja_d810.semantic.verify import Verdict, check_equivalent, sample_envs, to_z3


def p(text, width=32):
    return parse(text, width)


def test_equivalent_expressions_are_proven():
    assert check_equivalent(p("(x ^ y) + 2 * (x & y)"), p("x + y")) is Verdict.PROVEN


def test_inequivalent_expressions_are_refuted_by_sampling():
    assert check_equivalent(p("(x | y) + (x & y)"), p("x - y"), use_z3=False) is Verdict.REFUTED


def test_z3_catches_what_sampling_misses():
    # ((d - 1) & ~d) >> 31 is 1 only when d == 0, so b differs from a only when x == 0x12345678
    a = p("x")
    b = p("x + ((((x ^ 0x12345678) - 1) & ~(x ^ 0x12345678)) >> 31)")
    assert check_equivalent(a, b, samples=0) is Verdict.REFUTED


def test_without_z3_sampling_result_is_tested(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "z3":
            raise ImportError("no z3")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert check_equivalent(p("(x ^ y) + 2 * (x & y)"), p("x + y")) is Verdict.TESTED


def test_width_mismatch_is_refuted():
    assert check_equivalent(p("x", 32), p("x", 64)) is Verdict.REFUTED


def test_extensions_and_shifts_translate_to_z3():
    a = parse("trunc8(zx32(q) + 0)", 8, {"q": 8})
    assert check_equivalent(a, parse("q", 8, {"q": 8})) is Verdict.PROVEN
    assert check_equivalent(p("sar(x, 31)"), p("-(x >> 31)")) is Verdict.PROVEN


def test_z3_semantics_match_evaluate():
    # A proof is only as good as this agreement; shift amounts >= width are the risky part.
    rng = random.Random(9)
    cases = [p("x << 33"), p("sar(x, y)"), p("x >> (y + 40)"), parse("zx32(q) << q", 32, {"q": 8})]
    cases += [random_expr(rng, ops=DEFAULT_OPS + ("shl",)) for _ in range(100)]
    for e in cases:
        widths = {v.name: v.width for v in variables(e)}
        symbols = {name: z3.BitVec(name, w) for name, w in widths.items()}
        term = to_z3(e, symbols)
        for env in sample_envs(widths, 12, seed=1):
            pairs = [(symbols[n], z3.BitVecVal(env[n], widths[n])) for n in widths]
            assert z3.simplify(z3.substitute(term, *pairs)).as_long() == evaluate(e, env), to_str(e)
