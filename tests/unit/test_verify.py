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
    _without_z3(monkeypatch)
    # A true non-linear identity, so the signature proof does not apply and only Z3 could prove it.
    a = p("(x & y) * (x | y) + (x & ~y) * (~x & y)")
    assert check_equivalent(a, p("x * y")) is Verdict.TESTED


def _without_z3(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "z3":
            raise ImportError("no z3")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_linear_mba_pairs_are_proven_by_signature_without_z3(monkeypatch):
    _without_z3(monkeypatch)
    a = p("(x ^ y) + 2 * (x & y) + 3 * ((x | y) - (x & ~y) - y)")
    assert check_equivalent(a, p("x + y")) is Verdict.PROVEN


def test_linear_mba_pairs_are_refuted_by_signature():
    assert check_equivalent(p("x | y"), p("x + y"), samples=0, use_z3=False) is Verdict.REFUTED


def test_signature_proof_needs_both_sides_linear():
    # x * y and x & y agree on every 0/1 input but differ in general.
    assert check_equivalent(p("x * y"), p("x & y"), samples=0, use_z3=False) is Verdict.TESTED


def test_signature_proof_needs_full_width_variables():
    # zx32(q) is not a 32-bit variable, so the 0/1 signature does not determine the expression.
    a = parse("zx32(q) & 1", 32, {"q": 8})
    assert check_equivalent(a, parse("zx32(q)", 32, {"q": 8}), samples=0, use_z3=False) is Verdict.TESTED


def test_signature_verdicts_agree_with_z3():
    from binja_d810.semantic.verify import _signature_proof, _z3_prove

    rng = random.Random(12)
    terms = ["x", "y", "z", "(x & y)", "(x | z)", "(y ^ z)", "(~x & y)", "(x & y & z)", "~(x | y)", "5"]
    decided = 0
    for _ in range(60):
        a, b = (p(" + ".join(f"{rng.choice([1, 2, -1, 3])} * {rng.choice(terms)}" for _ in range(3)), 16)
                for _ in range(2))
        if rng.random() < 0.5:
            b = a  # make some pairs equal so both verdicts are exercised
        widths = {v.name: v.width for v in variables(a) + variables(b)}
        by_z3, _ = _z3_prove(a, b, widths, 500)
        if by_z3 is not None:
            decided += 1
            assert _signature_proof(a, b) is by_z3, (to_str(a), to_str(b))
    assert decided >= 30


def test_each_thread_gets_its_own_z3_context():
    # Binary Ninja runs the activity on parallel analysis threads; sharing Z3's default context
    # crashed it in Z3_mk_context_rc.
    import threading

    from binja_d810.semantic.verify import _z3_context

    seen = []
    worker = threading.Thread(target=lambda: seen.append(_z3_context()))
    worker.start()
    worker.join()
    seen.append(_z3_context())
    assert seen[0] is not seen[1]
    assert _z3_context() is seen[1]


def test_concurrent_z3_checks_agree():
    from concurrent.futures import ThreadPoolExecutor

    cases = [(p("x << 1"), p("2 * x"), Verdict.PROVEN), (p("x << 1"), p("3 * x"), Verdict.REFUTED)] * 40
    with ThreadPoolExecutor(8) as pool:
        verdicts = list(pool.map(lambda c: check_equivalent(c[0], c[1], samples=0), cases))
    assert verdicts == [expected for _, _, expected in cases]


def test_default_z3_budget_is_500_ms():
    import inspect

    assert inspect.signature(check_equivalent).parameters["z3_timeout_ms"].default == 500


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
