import time

import pytest

from binja_d810.ir.expr import BinOp
from binja_d810.ir.parse import parse
from binja_d810.semantic.low_bits import low_bits_value

WIDTHS = [8, 16, 32, 64]
NEVER = [  # the classic number-theoretic predicates: never equal, whatever the inputs
    ("7 * y * y - 1", "x * x"),
    ("(x * x) & 3", "2"),
    ("x * x + 3", "udiv(2, y * y + 1)"),
    ("x * x", "8 * y + 2"),
    ("(x * (x + 1)) & 1", "1"),
]


def cmp(op, left, right, width=32):
    return parse(f"{op}({left}, {right})", width)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("left,right", NEVER)
def test_classic_predicates_are_never_true(left, right, width):
    assert low_bits_value(cmp("eq", left, right, width)) == 0
    assert low_bits_value(cmp("ne", left, right, width)) == 1


@pytest.mark.parametrize("width", WIDTHS)
def test_identity_on_low_bits(width):
    assert low_bits_value(cmp("eq", "(x * (x + 1)) & 1", "0", width)) == 1


@pytest.mark.parametrize("op,left,right", [
    ("eq", "7 * y * y - 1", "x * x + 6"),  # y = 1, x = 0
    ("eq", "x * x", "y * y + 1"),          # x = 1, y = 0
    ("eq", "(x * x) >> 3", "5"),           # a right shift: an atom with a full domain
    ("eq", "sdiv(x * x, 3)", "y"),
    ("eq", "x + y", "3"),                  # no product of two non-constants
    ("ult", "x * x", "2"),                 # not eq/ne
])
def test_left_undecided(op, left, right):
    assert low_bits_value(cmp(op, left, right)) is None


def test_operations_that_do_not_commute_are_atoms():
    # x * x >> 31 is 0 or 1: an atom over {0, 1}. Twice it plus one is odd, 2 * y * y even.
    assert low_bits_value(cmp("eq", "((x * x) >> 31) * 2 + 1", "2 * y * y")) == 0


def test_shared_values_are_not_expanded():
    e = parse("x * x", 32)
    for _ in range(40):
        e = BinOp("add", e, e, 32)  # a DAG of 2**40 tree nodes
    started = time.perf_counter()
    # 2**40 * x * x is 0 at 32 bits, and y * y + 2 is never 0: decided, each shared value evaluated once
    assert low_bits_value(BinOp("eq", e, parse("y * y + 2", 32), 1)) == 0
    assert time.perf_counter() - started < 0.5
