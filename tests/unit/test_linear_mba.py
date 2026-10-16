import random

import pytest

from binja_d810.ir.expr import node_count, is_mba
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
        # Needs a subset of the single-variable terms (z) plus one bitwise remainder (x ^ y).
        ("x + y + z - 2 * (x & y)", "z + (x ^ y)"),
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


@pytest.mark.parametrize(
    "text, widths, expected, expected_widths",
    [
        # CoBRA's partial result for the poly corpus sample: x * y is an opaque term in a linear sum.
        ("(-2) * (x & y) + (y + (x + x * y))", None, "x * y + (x ^ y)", None),
        # A repeated non-linear term is the same atom, so it can cancel or combine.
        ("((x * y) ^ z) + 2 * ((x * y) & z) - z", None, "x * y", None),
        # Narrow operands inside an extension stay inside the atom.
        ("(zx32(p) ^ y) + 2 * (zx32(p) & y)", {"p": 8}, "zx32(p) + y", {"p": 8}),
    ],
)
def test_non_linear_subterms_are_treated_as_atoms(text, widths, expected, expected_widths):
    e = normalize(parse(text, 32, widths))
    result = backend.simplify(e)
    assert result is not None
    assert check_equivalent(e, result) is not Verdict.REFUTED
    assert node_count(result) <= node_count(normalize(parse(expected, 32, expected_widths)))


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


def test_arithmetic_operands_of_bitwise_operations_are_atoms():
    # tigress_vm: (A ^ B) + 2 * (~A | B) with A, B sums; as a whole, A ^ B is not one atom, or the
    # relation between A ^ B and ~A | B would be lost. It is B - A - 2.
    e = normalize(ir("((x + y) ^ (z - w)) + 2 * (~(x + y) | (z - w))"))
    result = backend.simplify(e)
    assert result is not None and not is_mba(result)
    assert check_equivalent(e, result) is Verdict.PROVEN
