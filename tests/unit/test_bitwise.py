import pytest

from binja_d810.ir.expr import node_count
from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse
from binja_d810.semantic.bitwise import BitwiseBackend
from binja_d810.semantic.verify import Verdict, check_equivalent

CASES = [  # OLLVM substitutions with constants, and constant interactions atoms cannot see
    ("(x & 0x5a) | (x ^ 0x5a)", "x | 0x5a"),
    ("(x ^ ~0x5a) & x", "x & 0x5a"),
    ("(~x & 0x5a) | (x & ~0x5a)", "x ^ 0x5a"),
    ("(x | 0xf0) & 0x0f", "x & 0x0f"),
    ("(~x & 0x55) | (x & 0xaa)", "(x ^ 0x55) & 0xff"),
    ("(x ^ (y ^ 1)) & x & 1", "x & y & 1"),
    ("((x + y) & 0x5a) | ((x + y) ^ 0x5a)", "(x + y) | 0x5a"),  # an atom
]


@pytest.mark.parametrize("width", [8, 16, 32, 64])
@pytest.mark.parametrize("text,expected", CASES)
def test_substitutions_collapse(text, expected, width):
    e = parse(text, width)
    found = BitwiseBackend().simplify(e)
    assert found is not None and node_count(found) <= node_count(normalize(parse(expected, width)))
    assert check_equivalent(e, found) is Verdict.PROVEN


def test_one_bit_root():
    e = parse("(a ^ (b ^ a)) & a", 1)
    assert BitwiseBackend().simplify(e) == normalize(parse("a & b", 1))


def test_extensions_are_atoms():
    e = parse("(zx32(c) & y) | (zx32(c) ^ y)", 32, {"c": 8})
    assert BitwiseBackend().simplify(e) == normalize(parse("zx32(c) | y", 32, {"c": 8}))


@pytest.mark.parametrize("text", [
    "x + y",                                   # not a bitwise root
    "(a & 0xf0) | (b & 0x0f)",                 # already minimal: a bit merge
    "(a & b) | (c & d) | (e ^ (a & e))",       # five names
    "(x & 1) | (x & 2) | (x & 4) | (x & 8) | (x & 16)",  # five constants
])
def test_left_alone(text):
    assert BitwiseBackend().simplify(parse(text, 32)) is None
