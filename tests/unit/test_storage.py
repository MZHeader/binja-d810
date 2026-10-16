"""Backing-storage model for field reads and partial writes (spec section 6.1)."""

import itertools

import pytest

from binja_d810.binja import storage
from binja_d810.ir.evaluate import evaluate
from binja_d810.ir.expr import BinOp, Const, UnOp, Var, walk
from binja_d810.ir.normalize import normalize
from binja_d810.semantic.linear_mba import LinearMBABackend
from binja_d810.semantic.verify import Verdict, check_equivalent
from binja_d810.simplify import simplify_expression


@pytest.mark.parametrize("offset,size,storage_size,ok", [
    (0, 1, 8, True), (0, 4, 8, True), (4, 4, 8, True), (7, 1, 8, True), (1, 1, 2, True),
    (7, 2, 8, False), (0, 9, 8, False), (-1, 1, 8, False), (8, 1, 8, False), (0, 0, 8, False),
])
def test_contained(offset, size, storage_size, ok):
    assert storage.contained(offset, size, storage_size) is ok


def test_field_read_extracts_the_bytes_exhaustively():
    b = Var("b", 16)
    for offset, width in ((0, 8), (8, 8), (0, 16)):
        e = storage.field_read(b, offset, width)
        assert e.width == width
        for value in range(1 << 16):
            assert evaluate(e, {"b": value}) == (value >> offset) & ((1 << width) - 1)


def test_field_read_omits_no_op_steps():
    b = Var("b", 64)
    assert storage.field_read(b, 0, 64) == b
    assert storage.field_read(b, 0, 8) == UnOp("trunc", b, 8)
    assert storage.field_read(b, 8, 8) == UnOp("trunc", BinOp("lshr", b, Const(8, 64), 64), 8)


@pytest.mark.parametrize("offset", [0, 8])
def test_preserving_partial_write_exhaustively(offset):
    old, value = Var("old", 16), Var("v", 8)
    merged = storage.merge_partial_write(old, value, offset)
    assert merged.width == 16
    for o, v in itertools.product(range(0, 1 << 16, 7), range(256)):
        expected = (o & ~(0xFF << offset) & 0xFFFF) | (v << offset)
        assert evaluate(merged, {"old": o, "v": v}) == expected


def test_full_width_write_is_the_value():
    assert storage.merge_partial_write(Var("old", 32), Var("v", 32), 0) == Var("v", 32)


def test_partial_write_must_fit():
    with pytest.raises(ValueError):
        storage.merge_partial_write(Var("old", 16), Var("v", 8), 12)


def test_no_upper_bits_are_invented():
    # A field read wider than the declared type is the backing value's own bits, never a zx of the
    # declared-width variable: the read of x.w0 for a uint8_t x must depend on the upper bits.
    backing = Var("b", 64)
    wide = storage.field_read(backing, 0, 32)
    assert not any(isinstance(n, UnOp) and n.op == "zx" for n in walk(wide))
    assert evaluate(wide, {"b": 0x1234_5678}) == 0x1234_5678


def test_reads_of_one_backing_value_are_related():
    # safety_u8 at -O2 on ARM64: x8_1 = y ^ x (1-byte vars), x9 = y.w1 & x.w0 (4-byte field reads of
    # the same registers), x8 = x8_1 + low_part.1(x9 << 1). With one backing value per register
    # version this is the MBA (y ^ x) + 2 * (y & x) on the low bytes, which simplifies to x + y.
    bx, by = Var("@x0#0", 64), Var("@x1#0", 64)
    x, y = storage.field_read(bx, 0, 8), storage.field_read(by, 0, 8)
    x9 = BinOp("and", storage.field_read(by, 0, 32), storage.field_read(bx, 0, 32), 32)
    root = BinOp("add", BinOp("xor", y, x, 8), UnOp("trunc", BinOp("shl", x9, Const(1, 8), 32), 8), 8)
    outcome = simplify_expression(root, [], [LinearMBABackend()])
    assert outcome.rewrite is not None
    assert check_equivalent(outcome.rewrite.replacement, normalize(BinOp("add", x, y, 8))) is Verdict.PROVEN


def test_as_field_recognizes_lowerable_reads():
    b = Var("b", 64)
    assert storage.as_field(storage.field_read(b, 8, 8)) == (b, 8, 8)
    assert storage.as_field(storage.field_read(b, 0, 32)) == (b, 0, 32)
    assert storage.as_field(b) == (b, 0, 64)
    assert storage.as_field(Var("plain", 8)) == (Var("plain", 8), 0, 8)
    assert storage.as_field(BinOp("add", b, b, 64)) is None


@pytest.mark.parametrize("offset,width", [(8, 64), (56, 16), (-8, 8)])
def test_field_read_must_fit(offset, width):
    with pytest.raises(ValueError):
        storage.field_read(Var("b", 64), offset, width)


@pytest.mark.parametrize("read,write,kind", [
    ((5, 1), (5, 1), "inside"), ((4, 1), (4, 2), "inside"), ((5, 1), (4, 2), "inside"),
    ((7, 1), (5, 1), "disjoint"), ((0, 4), (4, 4), "disjoint"), ((4, 4), (0, 4), "disjoint"),
    ((3, 2), (4, 1), "partial"), ((0, 8), (5, 1), "partial"),
])
def test_read_over_write(read, write, kind):
    # x86 -O0 spills bytes into one stack slot (var_8:7.b = y; var_8:6.b = x; ... var_8:5.b): a read
    # inside the last write reads the written value, a disjoint read reads the previous version.
    assert storage.read_over_write(*read, *write) == kind


def test_wider_operands_of_modular_operations_are_truncated_exactly():
    # HLIL folds lea into x * 5 with an 8-byte constant under a 4-byte multiply. For + - * & | ^ the
    # low w bits of the result depend only on the low w bits of the operands.
    import random

    for op in ("add", "sub", "mul", "and", "or", "xor"):
        wide = BinOp(op, Var("a", 64), Var("b", 64), 64)
        narrow = BinOp(op, storage.fit(Var("a", 64), 32), storage.fit(Var("b", 64), 32), 32)
        rng = random.Random(op)
        for _ in range(200):
            env = {"a": rng.getrandbits(64), "b": rng.getrandbits(64)}
            assert evaluate(narrow, env) == evaluate(wide, env) & 0xFFFFFFFF
    assert storage.fit(Const(5, 64), 32) == Const(5, 32)
    assert storage.fit(Var("a", 8), 32) is None  # a narrower operand needs an extension the IL did not give


def test_visible_size_counts_a_field_read_as_one_node():
    b = Var("@rax#0", 64)
    read = storage.field_read(b, 8, 8)  # rax.ah: one IL node once lowered
    assert storage.visible_size(BinOp("add", read, storage.field_read(Var("@rdx#0", 64), 8, 8), 8)) == 3
    assert storage.visible_size(BinOp("add", Var("x", 32), Const(1, 32), 32)) == 3
