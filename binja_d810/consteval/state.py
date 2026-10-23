"""Concrete scalar/byte state with explicit unknown bits and symbolic local addresses."""

from dataclasses import dataclass

from ..ir.evaluate import evaluate
from ..ir.expr import BinOp, Const, UnOp
from .program import Address


@dataclass(frozen=True)
class Cell:
    value: object
    width: int
    known: int


def binary(op, a, b, a_width, b_width, width):
    if isinstance(a, Address) or isinstance(b, Address):
        if isinstance(a, Address) and isinstance(b, Address):
            if a.buffer != b.buffer or a.width != b.width:
                raise ValueError("unproved relationship between local pointers")
            if op in ("eq", "ne"):
                return int((a.offset == b.offset) == (op == "eq"))
            if op == "sub" and width == a.width:
                return (a.offset - b.offset) & ((1 << width) - 1)
            raise ValueError("local pointer ordering or bits are unknown")
        if op == "add" and isinstance(b, Address):
            a, b = b, a
        if isinstance(a, Address) and width == a.width and op in ("add", "sub"):
            return Address(a.buffer, (a.offset + (b if op == "add" else -b)) & ((1 << width) - 1), width)
        raise ValueError("local pointer bits are unknown")
    return evaluate(BinOp(op, Const(a, a_width), Const(b, b_width), width), {})


def unary(op, value, inner_width, width):
    if isinstance(value, Address):
        if inner_width == width:
            return value
        raise ValueError("local pointer bits are unknown")
    if inner_width == width and op in ("zx", "sx", "trunc"):
        return value
    return evaluate(UnOp(op, Const(value, inner_width), width), {})


class State:
    def __init__(self, size, little_endian=True):
        self.memory = [None] * size
        self.cells = {}
        self.written = set()
        self.order = "little" if little_endian else "big"

    def assign(self, key, value, width):
        if width < 1:
            raise ValueError("unknown scalar width")
        if isinstance(value, Address) and value.width != width:
            raise ValueError("pointer width mismatch")
        self.cells[key] = Cell(value if isinstance(value, Address) else value & ((1 << width) - 1), width, (1 << width) - 1)

    def field(self, key, value, offset, width, whole_width):
        if offset < 0 or offset + width > whole_width or isinstance(value, Address):
            raise ValueError("unsupported partial scalar write")
        old = self.cells.get(key, Cell(0, whole_width, 0))
        if isinstance(old.value, Address):
            raise ValueError("partial pointer write")
        mask = ((1 << width) - 1) << offset
        self.cells[key] = Cell((old.value & ~mask) | ((value << offset) & mask),
                               max(old.width, whole_width), old.known | mask)

    def read(self, key, width, offset=0):
        cell = self.cells.get(key)
        mask = ((1 << width) - 1) << offset
        if cell is None or offset < 0 or offset + width > cell.width or cell.known & mask != mask:
            raise ValueError("unknown scalar bits")
        if isinstance(cell.value, Address):
            if offset or width != cell.width:
                raise ValueError("local pointer bits are unknown")
            return cell.value
        return (cell.value >> offset) & ((1 << width) - 1)

    def span(self, offset, size):
        if offset < 0 or size <= 0 or offset + size > len(self.memory):
            raise ValueError("local access outside the bounded frame")

    def store(self, offset, value, size):
        self.span(offset, size)
        if isinstance(value, Address):
            raise ValueError("pointer bytes are unknown")
        self.memory[offset:offset + size] = (value & ((1 << (8 * size)) - 1)).to_bytes(size, self.order)
        self.written.update(range(offset, offset + size))

    def load(self, offset, size):
        self.span(offset, size)
        data = self.memory[offset:offset + size]
        if None in data:
            raise ValueError("uninitialized local bytes")
        return int.from_bytes(data, self.order)
