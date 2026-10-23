"""An IL-independent interpreter. Unknown values/effects cancel the whole proposed region.

The native adapter must establish region entry, readonly inputs, local-buffer identity, live-outs
and a single exit before replacing any code. This core supplies exact bytes, not that CFG proof.
"""

from dataclasses import dataclass, field

from ..deadline import Deadline
from ..ir.evaluate import evaluate
from ..ir.expr import BinOp, Const, Expr, UnOp, Var, tree_size, variables


@dataclass(frozen=True)
class Limits:
    instructions: int = 4096
    nodes: int = 2000
    steps: int = 10000
    trips: int = 256
    buffer_bytes: int = 4096
    buffers: int = 16
    work: int = 40000


@dataclass(frozen=True)
class Address:
    buffer: str
    offset: int
    width: int


@dataclass(frozen=True)
class AddressOf:
    destination: str
    buffer: str
    offset: Expr
    width: int


@dataclass(frozen=True)
class LoadIndirect:
    destination: str
    address: Expr
    size: int


@dataclass(frozen=True)
class StoreIndirect:
    address: Expr
    value: Expr


@dataclass(frozen=True)
class Assign:
    destination: str
    value: Expr


@dataclass(frozen=True)
class Store:
    buffer: str
    offset: Expr
    value: Expr


@dataclass(frozen=True)
class Load:
    destination: str
    buffer: str
    offset: Expr
    size: int


@dataclass(frozen=True)
class Branch:
    condition: Expr
    yes: int
    no: int


@dataclass(frozen=True)
class Jump:
    target: int


@dataclass(frozen=True)
class Stop:
    pass


@dataclass
class Result:
    buffers: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)
    visited: frozenset = frozenset()
    steps: int = 0
    reason: str = ""


def run(program, buffers, *, entry=0, limits=Limits(), little_endian=True, deadline=Deadline.never()):
    if type(entry) is not int or any(type(index) is not int or index < 0 for index in program):
        return Result(reason="constant region instruction identity is invalid")
    if len(program) > limits.instructions or len(buffers) > limits.buffers:
        return Result(reason="constant region size limit")
    if any(type(size) is not int or size <= 0 for size in buffers.values()) \
            or sum(buffers.values()) > limits.buffer_bytes:
        return Result(reason="constant region buffer limit")
    memory = {name: [None] * size for name, size in buffers.items()}
    values, widths, counts, visited = {}, {}, {}, set()
    pc, steps = entry, 0
    work = 0
    order = "little" if little_endian else "big"

    def calculate(expression):
        nonlocal work
        work += 1
        if work > limits.work or deadline.expired():
            raise ValueError("constant evaluation expression work limit")
        if isinstance(expression, Const):
            return expression.value
        if isinstance(expression, Var):
            return values[expression.name]
        if isinstance(expression, UnOp):
            value = calculate(expression.operand)
            if isinstance(value, Address):
                raise ValueError("local pointer bits are not known")
            return evaluate(UnOp(expression.op, Const(value, expression.operand.width), expression.width), {})
        a, b = calculate(expression.left), calculate(expression.right)
        if isinstance(a, Address) or isinstance(b, Address):
            if isinstance(a, Address) and isinstance(b, Address):
                if a.buffer != b.buffer or a.width != b.width:
                    raise ValueError("unproved relationship between local pointers")
                if expression.op in ("eq", "ne"):
                    return int((a.offset == b.offset) == (expression.op == "eq"))
                if expression.op == "sub" and expression.width == a.width:
                    return (a.offset - b.offset) & ((1 << a.width) - 1)
                raise ValueError("local pointer ordering or bits are not known")
            if expression.op == "add" and isinstance(b, Address):
                a, b = b, a
            if isinstance(a, Address) and expression.width == a.width and expression.op in ("add", "sub"):
                return Address(a.buffer, (a.offset + (b if expression.op == "add" else -b)) & ((1 << a.width) - 1), a.width)
            raise ValueError("local pointer bits are not known")
        return evaluate(BinOp(expression.op, Const(a, expression.left.width), Const(b, expression.right.width), expression.width), {})

    def expr(expression):
        if deadline.expired() or tree_size(expression) > limits.nodes:
            raise ValueError("constant expression budget exhausted")
        for variable in variables(expression):
            if variable.name not in values:
                raise ValueError("constant region has an unknown input")
            if widths[variable.name] != variable.width:
                raise ValueError("constant variable width is ambiguous")
        return calculate(expression)

    def integer(expression):
        value = expr(expression)
        if isinstance(value, Address):
            raise ValueError("local pointer is observed as data")
        return value

    def span(name, offset, size):
        if name not in memory or offset < 0 or size <= 0 or offset + size > len(memory[name]):
            raise ValueError("constant buffer access out of bounds")
        return memory[name]

    try:
        while True:
            if steps >= limits.steps or deadline.expired():
                raise ValueError("constant evaluation step budget exhausted")
            counts[pc] = counts.get(pc, 0) + 1
            if counts[pc] > limits.trips:
                raise ValueError("constant evaluation trip limit")
            if pc not in program:
                raise ValueError("constant region has an unknown exit")
            node = program[pc]
            steps += 1
            visited.add(pc)
            if isinstance(node, Stop):
                return Result({name: tuple(data) for name, data in memory.items()}, dict(values),
                              frozenset(visited), steps)
            if isinstance(node, Assign):
                values[node.destination] = expr(node.value)
                widths[node.destination] = node.value.width
            elif isinstance(node, AddressOf):
                if node.buffer not in memory or node.width < 1 or len(memory[node.buffer]) > 1 << node.width:
                    raise ValueError("local pointer extent is unproved")
                values[node.destination] = Address(node.buffer, integer(node.offset) & ((1 << node.width) - 1), node.width)
                widths[node.destination] = node.width
            elif isinstance(node, Store):
                if node.value.width % 8:
                    raise ValueError("constant store is not byte-sized")
                offset, size = integer(node.offset), node.value.width // 8
                data = span(node.buffer, offset, size)
                data[offset:offset + size] = integer(node.value).to_bytes(size, order)
            elif isinstance(node, Load):
                offset = integer(node.offset)
                data = span(node.buffer, offset, node.size)[offset:offset + node.size]
                if None in data:
                    raise ValueError("constant load has uninitialized bytes")
                values[node.destination] = int.from_bytes(data, order)
                widths[node.destination] = node.size * 8
            elif isinstance(node, (LoadIndirect, StoreIndirect)):
                address = expr(node.address)
                if not isinstance(address, Address):
                    raise ValueError("constant region accesses unknown memory")
                size = node.size if isinstance(node, LoadIndirect) else node.value.width // 8
                if isinstance(node, StoreIndirect) and node.value.width % 8:
                    raise ValueError("constant store is not byte-sized")
                data = span(address.buffer, address.offset, size)
                if isinstance(node, StoreIndirect):
                    data[address.offset:address.offset + size] = integer(node.value).to_bytes(size, order)
                else:
                    selected = data[address.offset:address.offset + size]
                    if None in selected:
                        raise ValueError("constant load has uninitialized bytes")
                    values[node.destination] = int.from_bytes(selected, order)
                    widths[node.destination] = size * 8
            elif isinstance(node, Branch):
                if node.condition.width != 1:
                    raise ValueError("constant branch is not boolean")
                pc = node.yes if integer(node.condition) else node.no
                continue
            elif isinstance(node, Jump):
                pc = node.target
                continue
            else:
                raise ValueError("constant region contains an unknown effect")
            pc += 1
    except KeyError:
        return Result(steps=steps, reason="constant region has an unknown input")
    except (ValueError, ArithmeticError, RecursionError) as exc:
        return Result(steps=steps, reason=str(exc) or "constant expression depth unsupported")
