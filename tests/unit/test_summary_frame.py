"""Byte overlaps and endian order must agree with an independent byte-array model."""

import random

import pytest

from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import Const, Var
from binja_d810.summaries.frame import Frame


@pytest.mark.parametrize("little", (True, False))
def test_partial_overwrites_and_loads(little):
    rng = random.Random(910)
    order = "little" if little else "big"
    for _ in range(30):
        frame = Frame(32, little)
        memory = bytearray(rng.randbytes(32))
        frame.write(-16, Const(int.from_bytes(memory, order), 256))
        for _ in range(15):
            size = rng.choice((1, 2, 4, 8))
            at = rng.randrange(33 - size)
            value = rng.getrandbits(size * 8)
            frame.write(at - 16, Const(value, size * 8))
            memory[at:at + size] = value.to_bytes(size, order)
            for n in (1, 2, 4, 8):
                read = rng.randrange(33 - n)
                assert evaluate(frame.read(read - 16, n), {}) == int.from_bytes(memory[read:read + n], order)


def test_a_frame_read_before_write_remains_a_live_input():
    frame = Frame(8)
    incoming = frame.read(4, 4)
    frame.write(4, Const(7, 32))
    assert frame.inputs == {4, 5, 6, 7}
    assert evaluate(incoming, {f"stack:{n:+d}": n for n in range(4, 8)}) == 0x07060504
    assert evaluate(frame.read(4, 4), {}) == 7


def test_symbolic_scratch_round_trip_preserves_value():
    frame = Frame(8)
    frame.write(-8, Var("input", 64))
    for value in (0, 1, 0xffffffffffffffff, 0x17a932fbc2a56d81):
        assert evaluate(frame.read(-8, 8), {"input": value}) == value
    assert not frame.inputs


def test_frame_limit_counts_distinct_bytes_not_the_offset():
    frame = Frame(4)
    frame.write(50000, Const(1, 32))
    frame.write(50000, Const(2, 32))
    with pytest.raises(ValueError, match="frame byte limit"):
        frame.read(-50000, 1)
    assert evaluate(frame.read(50000, 4), {}) == 2
