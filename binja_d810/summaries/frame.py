"""Byte-exact symbolic frame slots; no assumptions about declarations or alias flags."""

from ..ir.expr import BinOp, Const, UnOp, Var
from ..ir.normalize import normalize


class Frame:
    def __init__(self, max_bytes: int, little_endian: bool = True):
        self.max_bytes, self.little_endian = max_bytes, little_endian
        self.bytes = {}
        self.inputs = set()
        self.written = set()
        self.whole = {}  # retain whole stored values instead of repeatedly splitting/rejoining them

    def _check(self, offset: int, size: int):
        if size <= 0 or size > self.max_bytes:
            raise ValueError("frame byte limit")
        added = sum(offset + i not in self.bytes for i in range(size))
        if len(self.bytes) + added > self.max_bytes:
            raise ValueError("frame byte limit")

    def read(self, offset: int, size: int):
        self._check(offset, size)
        if (offset, size) in self.whole:
            return self.whole[offset, size]
        parts = []
        for i in range(size):
            at = offset + i
            if at not in self.bytes:
                self.bytes[at] = Var(f"stack:{at:+d}", 8)
                self.inputs.add(at)
            part = self.bytes[at]
            if size > 1:
                part = UnOp("zx", part, size * 8)
                shift = 8 * (i if self.little_endian else size - i - 1)
                if shift:
                    part = BinOp("shl", part, Const(shift, size * 8), size * 8)
            parts.append(part)
        result = parts[0]
        for part in parts[1:]:
            result = BinOp("or", result, part, size * 8)
        result = normalize(result)
        self.whole[offset, size] = result
        return result

    def write(self, offset: int, value):
        if value.width % 8:
            raise ValueError("non-byte frame write")
        size = value.width // 8
        self._check(offset, size)
        for start, length in list(self.whole):
            if start < offset + size and offset < start + length:
                del self.whole[start, length]
        for i in range(size):
            shift = 8 * (i if self.little_endian else size - i - 1)
            part = BinOp("lshr", value, Const(shift, value.width), value.width) if shift else value
            if size > 1:
                part = UnOp("trunc", part, 8)
            self.bytes[offset + i] = normalize(part)
            self.written.add(offset + i)
        self.whole[offset, size] = normalize(value)

    def outputs(self):
        return {offset: self.bytes[offset] for offset in sorted(self.written)}
