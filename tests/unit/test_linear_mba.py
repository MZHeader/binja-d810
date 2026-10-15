import random

import pytest

from binja_d810.ir.expr import node_count
from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse
from binja_d810.semantic.linear_mba import LinearMBABackend, is_linear_mba
from binja_d810.semantic.verify import Verdict, check_equivalent

backend = LinearMBABackend()


def ir(text, width=32):
    return normalize(parse(text, width))


@pytest.mark.parametrize(
    "obfuscated, expected",
    [
        ("(x ^ y) + 2 * (x & y)", "x + y"),
        ("(x | y) - (x & y)", "x ^ y"),
        ("-x - 1", "~x"),
        ("(x | y) - (x & y) - (x ^ y) + 5", "5"),
        ("(x ^ y) + 2 * (x & y) + 3 * ((x | y) - (x & ~y) - y)", "x + y"),
        ("(x & y) + (x & ~y)", "x"),
    ],
)
def test_simplifies_known_linear_mbas(obfuscated, expected):
    result = backend.simplify(ir(obfuscated))
    assert result is not None
    assert check_equivalent(result, ir(expected)) is Verdict.PROVEN
    assert node_count(result) <= node_count(ir(expected))


@pytest.mark.parametrize("text", ["x * y", "(x & 0xff) + y", "(x + y) & z", "(x ^ y) >> 1"])
def test_rejects_non_linear(text):
    assert not is_linear_mba(ir(text))
    assert backend.simplify(ir(text)) is None


def test_refuses_too_many_variables():
    e = ir("(a ^ b) + (c ^ d) + (e ^ f) + (g ^ h) + 2 * (a & b)")
    assert LinearMBABackend(max_vars=6).simplify(e) is None


def test_returns_none_when_not_smaller():
    assert backend.simplify(ir("x + y")) is None


def test_random_linear_mbas_simplify_to_equivalent_smaller_forms():
    rng = random.Random(11)
    terms = ["x", "y", "z", "(x & y)", "(x | z)", "(y ^ z)", "(~x & y)", "(x & y & z)", "~(x | y)"]
    for width in (32, 64):
        for _ in range(40):
            text = " + ".join(f"{rng.choice([1, 2, 3, -1, -2, 7])} * {rng.choice(terms)}" for _ in range(6))
            e = ir(text, width)
            result = backend.simplify(e)
            if result is not None:
                # Z3 rarely proves MBA equivalences within budget (that is what MBA is for), but finds
                # counterexamples fast; 100 ms keeps this test at seconds instead of ~90 s at 2000 ms.
                assert check_equivalent(e, result, z3_timeout_ms=100) is not Verdict.REFUTED
                assert node_count(result) < node_count(e)
