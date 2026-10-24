"""Read leaf semantics before ABI inference discards nonstandard register results.

Frame accesses are represented explicitly, not declared pure. The caller must prove that
scratch writes cannot escape or be read after the call before a summary can be applied.
Implicit flag writes are retained in the source body; flag reads are not summarized here.
"""

from dataclasses import dataclass

from binaryninja import Endianness, ILInstructionAttribute as Attr, LowLevelILOperation as Op

from ...deadline import Deadline
from ...ir.evaluate import evaluate
from ...ir.expr import BinOp, Const, Expr, UnOp, Var, distinct_nodes, to_signed, variables
from ...ir.normalize import normalize
from ...summaries.frame import Frame
from ...summaries.state import Register, State
from ...semantic.verify import Verdict, check
from ..memory import fixed_load_is_safe
from .calls import CALL_MODELS, return_address, target


@dataclass(frozen=True)
class Limits:
    instructions: int = 64
    nodes: int = 2000
    frame_bytes: int = 256
    depth: int = 4
    caller_instructions: int = 4096
    proof_ms: int = 500
    sites: int = 64
    red_zone_bytes: int = 128
    frame_work: int = 40000
    frame_cfg: bool = True


@dataclass
class Summary:
    function: object
    lifted: object
    outputs: dict[str, Expr]
    inputs: frozenset[str]
    frame_outputs: dict[int, Expr]
    frame_inputs: frozenset[int]
    flag_writes: frozenset[str]
    stack_return: bool
    body: tuple
    expanded_instructions: int


@dataclass(frozen=True)
class Nested:
    node: object
    summary: Summary
    return_address: int


@dataclass(frozen=True)
class Result:
    summary: Summary | None = None
    reason: str = ""


_BINARY = {Op.LLIL_ADD: "add", Op.LLIL_SUB: "sub", Op.LLIL_MUL: "mul", Op.LLIL_AND: "and",
           Op.LLIL_OR: "or", Op.LLIL_XOR: "xor", Op.LLIL_LSL: "shl", Op.LLIL_LSR: "lshr",
           Op.LLIL_ASR: "ashr", Op.LLIL_ROL: "rol", Op.LLIL_ROR: "ror"}
_UNARY = {Op.LLIL_NEG: "neg", Op.LLIL_NOT: "not", Op.LLIL_ZX: "zx", Op.LLIL_SX: "sx",
          Op.LLIL_LOW_PART: "trunc"}
_COMPARE = {Op.LLIL_CMP_E: "eq", Op.LLIL_CMP_NE: "ne", Op.LLIL_CMP_ULT: "ult", Op.LLIL_CMP_ULE: "ule",
            Op.LLIL_CMP_UGT: "ugt", Op.LLIL_CMP_UGE: "uge", Op.LLIL_CMP_SLT: "slt", Op.LLIL_CMP_SLE: "sle",
            Op.LLIL_CMP_SGT: "sgt", Op.LLIL_CMP_SGE: "sge"}
_EXTEND = {"NoExtend": "preserve", "ZeroExtendToFullWidth": "zero", "SignExtendToFullWidth": "sign"}


def frame_offset(expr, stack: str, width: int):
    """An exact modular displacement from the incoming stack pointer."""
    if expr == Var(stack, width):
        return 0
    if isinstance(expr, BinOp) and expr.op in ("add", "sub"):
        if isinstance(expr.right, Const):
            left = frame_offset(expr.left, stack, width)
            if left is not None:
                return to_signed(left + (expr.right.value if expr.op == "add" else -expr.right.value), width)
        if expr.op == "add" and isinstance(expr.left, Const):
            right = frame_offset(expr.right, stack, width)
            if right is not None:
                return to_signed(right + expr.left.value, width)
    return None


class Lifter:
    def __init__(self, func, limits: Limits, deadline: Deadline, factory=None, depth=0):
        self.func, self.arch, self.limits, self.deadline = func, func.arch, limits, deadline
        self.factory, self.depth = factory, depth
        self.state = State({name: info.size * 8 for name, info in self.arch.regs.items()
                            if name == info.full_width_reg})
        self.frame = Frame(limits.frame_bytes, self.arch.endianness == Endianness.LittleEndian)
        self.visits = 0
        self.flags = set()

    def tick(self):
        self.visits += 1
        if self.deadline.expired():
            raise ValueError("time budget exhausted")
        if self.visits > self.limits.nodes:
            raise ValueError("summary node limit")

    def register(self, reg, size):
        name = str(reg)
        info = self.arch.regs.get(name)
        if info is None or size <= 0 or size > info.size:
            raise ValueError("unsupported register view")
        extend = _EXTEND.get(info.extend.name)
        if extend is None:
            raise ValueError("unknown register extension")
        return Register(info.full_width_reg, size * 8, info.offset * 8, extend)

    def expr(self, node):
        self.tick()
        if node.attributes & {Attr.SrcInstructionUsesPointerAuth, Attr.ILPreventDeadStoreElimination,
                              Attr.ILPreventAliasAnalysis}:
            raise ValueError("callee expression has protected effects")
        op, bits = node.operation, node.size * 8
        if node.flags:
            written = self.arch.flags_written_by_flag_write_type.get(node.flags)
            if written is None:
                raise ValueError("unknown flag write")
            self.flags.update(written)
        if op in (Op.LLIL_CONST, Op.LLIL_CONST_PTR):
            return Const(node.constant, bits)
        if op == Op.LLIL_REG:
            return self.state.read(self.register(node.src, node.size))
        if op in _BINARY or op in _COMPARE:
            left, right = self.expr(node.left), self.expr(node.right)
            return BinOp(_BINARY.get(op) or _COMPARE[op], left, right, 1 if op in _COMPARE else bits)
        if op in _UNARY:
            value = self.expr(node.src)
            return value if value.width == bits and op in (Op.LLIL_ZX, Op.LLIL_SX, Op.LLIL_LOW_PART) \
                else UnOp(_UNARY[op], value, bits)
        if op == Op.LLIL_LOAD:
            address = self.expr(node.src)
            offset = frame_offset(address, self.arch.stack_pointer, self.arch.address_size * 8)
            if offset is not None:
                return self.frame.read(offset, node.size)
            if variables(address):
                raise ValueError("load from unknown memory")
            at = evaluate(address, {})
            segment = self.func.view.get_segment_at(at)
            if not fixed_load_is_safe(self.func.view, at, node.size) or segment.writable \
                    or self.func.view.relocation_ranges_in_range(at, node.size):
                raise ValueError("load is not fixed read-only data")
            raw = self.func.view.read(at, node.size)
            if len(raw) != node.size:
                raise ValueError("incomplete read-only data")
            order = "little" if self.arch.endianness == Endianness.LittleEndian else "big"
            return Const(int.from_bytes(raw, order), bits)
        if op == Op.LLIL_POP:
            stack = self.state.read(self.register(self.arch.stack_pointer, self.arch.address_size))
            offset = frame_offset(stack, self.arch.stack_pointer, self.arch.address_size * 8)
            if offset is None:
                raise ValueError("unknown stack position")
            value = self.frame.read(offset, node.size)
            self.state.write(self.register(self.arch.stack_pointer, self.arch.address_size),
                             BinOp("add", stack, Const(node.size, stack.width), stack.width))
            return value
        raise ValueError(f"unsupported expression: {op.name}")

    def statement(self, node):
        self.tick()
        if node.attributes & {Attr.SrcInstructionUsesPointerAuth, Attr.ILPreventDeadStoreElimination,
                              Attr.ILPreventAliasAnalysis}:
            raise ValueError("callee statement has protected effects")
        if node.operation == Op.LLIL_NOP:
            return
        if node.operation in (Op.LLIL_REG, Op.LLIL_POP):
            self.expr(node)  # return operands retained by an already-substituted nested helper
            return
        if node.operation == Op.LLIL_CALL:
            if self.factory is None:
                raise ValueError("call needs a nested summary")
            result = self.factory.get(target(self.func, node), self.depth + 1)
            if result.summary is None:
                raise ValueError(f"nested summary: {result.reason}")
            nested = Nested(node, result.summary, return_address(self.func, node))
            self.execute(nested)
            return nested
        if node.operation == Op.LLIL_SET_REG:
            reg = self.register(node.dest, node.size)
            value = self.expr(node.src)
            if distinct_nodes(value) > self.limits.nodes:
                raise ValueError("summary expression limit")
            self.state.write(reg, value)
            return
        if node.operation == Op.LLIL_PUSH:
            value = self.expr(node.src)
            reg = self.register(self.arch.stack_pointer, self.arch.address_size)
            stack = self.state.read(reg)
            address = BinOp("sub", stack, Const(node.size, stack.width), stack.width)
            self.state.write(reg, address)
        elif node.operation == Op.LLIL_STORE:
            address, value = self.expr(node.dest), self.expr(node.src)
        else:
            raise ValueError(f"unsupported statement: {node.operation.name}")
        offset = frame_offset(address, self.arch.stack_pointer, self.arch.address_size * 8)
        if offset is None:
            raise ValueError("write outside the frame")
        self.frame.write(offset, value)

    def execute(self, entry):
        if not isinstance(entry, Nested):
            self.statement(entry)
            return
        summary = entry.summary
        mode = CALL_MODELS.get(self.arch.name)
        if mode is None or summary.stack_return != (mode == "stack"):
            raise ValueError("unsupported nested call-link semantics")
        size = self.arch.address_size
        value = Const(entry.return_address, size * 8)
        if summary.stack_return:
            reg = self.register(self.arch.stack_pointer, size)
            stack = self.state.read(reg)
            adjusted = BinOp("sub", stack, Const(size, size * 8), size * 8)
            offset = frame_offset(adjusted, self.arch.stack_pointer, size * 8)
            if offset is None:
                raise ValueError("unknown nested stack position")
            self.state.write(reg, adjusted)
            self.frame.write(offset, value)
        else:
            self.state.write(self.register(self.arch.link_reg, size), value)
        for child in summary.body:
            self.execute(child)
        self.expr(list(summary.lifted.instructions)[-1].dest)

    def summary(self):
        il = self.func.lifted_il_if_available
        if il is None:
            return Result(reason="callee lifted IL unavailable")
        if len(il) > self.limits.instructions:
            return Result(reason="callee instruction limit")
        instructions = list(il.instructions)
        if not instructions or instructions[-1].operation != Op.LLIL_RET:
            return Result(reason="callee has no single ordinary return")
        body, expanded = [], 1
        for node in instructions[:-1]:
            entry = self.statement(node)
            body.append(entry if isinstance(entry, Nested) else node)
            expanded += entry.summary.expanded_instructions + 1 if isinstance(entry, Nested) else 1
            if expanded > self.limits.instructions:
                return Result(reason="expanded callee instruction limit")
        end = instructions[-1]
        if end.attributes:
            return Result(reason="return has unmodeled attributes")
        stack_return = end.dest.operation == Op.LLIL_POP and end.dest.size == self.arch.address_size
        link_return = (self.arch.link_reg is not None and end.dest.operation == Op.LLIL_REG
                       and str(end.dest.src) == self.arch.link_reg
                       and check(self.expr(end.dest), Var(self.arch.link_reg, self.arch.address_size * 8),
                                 deadline=self.deadline, z3_timeout_ms=self.limits.proof_ms).verdict is Verdict.PROVEN)
        if not (stack_return or link_return):
            return Result(reason="unproved return target")
        if stack_return:
            # A RET/POP shape alone does not prove control returns to the caller. Check a
            # snapshot so the mechanical return-address read does not become a data input.
            returned = self.frame.copy().read(0, self.arch.address_size)
            incoming = Frame(self.limits.frame_bytes, self.frame.little_endian).read(0, self.arch.address_size)
            if check(returned, incoming, deadline=self.deadline,
                     z3_timeout_ms=self.limits.proof_ms).verdict is not Verdict.PROVEN:
                return Result(reason="callee stack return target is not preserved")
        stack = self.state.values[self.arch.stack_pointer]
        if check(stack, Var(self.arch.stack_pointer, self.arch.address_size * 8), deadline=self.deadline,
                 z3_timeout_ms=self.limits.proof_ms).verdict is not Verdict.PROVEN:
            return Result(reason="unbalanced frame")
        if any(name != self.arch.stack_pointer and any(v.name == self.arch.stack_pointer for v in variables(value))
               for name, value in self.state.outputs().items()):
            return Result(reason="callee returns a frame-derived address")
        return Result(Summary(self.func, il, self.state.outputs(), frozenset(self.state.live_in),
                              self.frame.outputs(), frozenset(self.frame.inputs), frozenset(self.flags), stack_return,
                              tuple(body), expanded))


class Factory:
    """Snapshots and summaries are retained only for one immutable caller analysis pass."""
    def __init__(self, limits, deadline):
        self.limits, self.deadline = limits, deadline
        self.cache, self.active = {}, set()

    def get(self, func, depth=0):
        if depth >= self.limits.depth:
            return Result(reason="callee summary depth limit")
        if func.start in self.active:
            return Result(reason="recursive callee")
        key = (func.start, depth)
        if key not in self.cache:
            self.active.add(func.start)
            try:
                self.cache[key] = Lifter(func, self.limits, self.deadline, self, depth).summary()
            except (ValueError, KeyError, TypeError) as exc:
                self.cache[key] = Result(reason=str(exc))
            finally:
                self.active.remove(func.start)
        return self.cache[key]


def summarize(func, limits=Limits(), deadline=Deadline.never()):
    return Factory(limits, deadline).get(func)
