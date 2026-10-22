"""Register views and simultaneous summary substitution, independent of a calling convention.

Each storage location starts as an unconstrained bit vector. Reads and writes use explicit
offsets and widths, including the architecture's implicit extension on a partial write.
Unknown extensions and mismatched widths fail instead of inventing upper bits.
"""

from dataclasses import dataclass

from ..ir.expr import BinOp, Const, Expr, UnOp, Var, mask, substitute, variables
from ..ir.normalize import normalize


@dataclass(frozen=True)
class Register:
    storage: str
    width: int
    offset: int = 0
    extend: str = "preserve"


class State:
    def __init__(self, widths):
        self.widths = dict(widths)
        self.values = {name: Var(name, width) for name, width in widths.items()}
        self.written = set()
        self.live_in = set()

    def read(self, reg: Register) -> Expr:
        value = self.values[reg.storage]
        if reg.width <= 0 or reg.offset < 0 or reg.offset + reg.width > value.width:
            raise ValueError("invalid register view")
        self.live_in.update(v.name for v in variables(value))
        if reg.offset:
            value = BinOp("lshr", value, Const(reg.offset, value.width), value.width)
        if reg.width != value.width:
            value = UnOp("trunc", value, reg.width)
        return normalize(value)

    def write(self, reg: Register, value: Expr) -> None:
        whole = self.widths[reg.storage]
        if value.width != reg.width or reg.offset < 0 or reg.offset + reg.width > whole:
            raise ValueError("register write width mismatch")
        if reg.extend not in ("preserve", "zero", "sign"):
            raise ValueError("unknown implicit register extension")
        if whole == value.width:
            result = value
        elif reg.extend in ("zero", "sign"):
            if reg.offset:
                raise ValueError("extending non-low register")
            result = UnOp("zx" if reg.extend == "zero" else "sx", value, whole)
        else:
            field = mask(reg.width) << reg.offset
            kept = BinOp("and", self.values[reg.storage], Const(~field, whole), whole)
            result = UnOp("zx", value, whole)
            if reg.offset:
                result = BinOp("shl", result, Const(reg.offset, whole), whole)
            result = BinOp("or", kept, result, whole)
        self.values[reg.storage] = normalize(result)
        self.written.add(reg.storage)

    def outputs(self) -> dict[str, Expr]:
        """Every written register is an output; no ABI-based liveness assumption."""
        return {name: self.values[name] for name in sorted(self.written)}

    def apply(self, outputs: dict[str, Expr]) -> None:
        """Bind a callee's input registers against one snapshot before writing any output."""
        for name, expression in outputs.items():
            if expression.width != self.widths[name]:
                raise ValueError("summary output width mismatch")
            for var in variables(expression):
                if var.name not in self.values or var.width != self.values[var.name].width:
                    raise ValueError("summary input width mismatch")
        pending = {name: substitute(expression, self.values) for name, expression in outputs.items()}
        for name, value in pending.items():
            self.live_in.update(v.name for v in variables(value))
            self.write(Register(name, self.widths[name]), value)
