"""Prove caller-frame writes unobserved on every path through an acyclic caller.

Each block is evaluated once. Joins retain all possible byte writers and forget unequal
non-address values. Unknown effects, ambiguous addresses and loops fail closed.
"""

from dataclasses import dataclass
from binaryninja import ILInstructionAttribute as Attr, LowLevelILOperation as Op

from ...ir.expr import BinOp, Const, Var, variables
from ...summaries.control import Effects, TrackedFrame, join, order
from ...summaries.abi import red_zone
from ...summaries.frame import Frame
from ...semantic.verify import Verdict, check
from .lift import Lifter, _BINARY, _COMPARE, _UNARY, frame_offset


class Work:
    def __init__(self, limits, deadline):
        self.left, self.deadline = limits.frame_work, deadline

    def tick(self, amount=1):
        self.left -= amount
        if self.deadline.expired():
            raise ValueError("time budget exhausted")
        if self.left < 0:
            raise ValueError("caller-frame work limit")


@dataclass(frozen=True)
class Proof:
    accepted: frozenset = frozenset()
    reason: str = ""
    blocks: int = 0
    work: int = 0


class Caller(Lifter):
    def tick(self):
        self.work.tick()

    def condition(self, node):
        """Observe memory reads in conditions without guessing flag values or branch outcomes."""
        self.tick()
        if node.attributes & {Attr.SrcInstructionUsesPointerAuth, Attr.ILPreventDeadStoreElimination,
                              Attr.ILPreventAliasAnalysis}:
            raise ValueError("protected caller condition")
        if node.operation in (Op.LLIL_FLAG, Op.LLIL_FLAG_BIT, Op.LLIL_FLAG_COND, Op.LLIL_FLAG_GROUP):
            return
        if node.operation in _BINARY or node.operation in _COMPARE:
            self.condition(node.left); self.condition(node.right)
        elif node.operation in _UNARY or node.operation == Op.LLIL_BOOL_TO_INT:
            self.condition(node.src)
        elif node.operation in (Op.LLIL_REG, Op.LLIL_CONST, Op.LLIL_CONST_PTR, Op.LLIL_LOAD):
            self.expr(node)
        else:
            raise ValueError("caller condition has unmodeled effects")

    def statement(self, node):
        if node.operation in _BINARY or node.operation in _COMPARE or node.operation in _UNARY:
            self.expr(node)  # standalone comparisons may read the scratch bytes before setting flags
        elif node.operation == Op.LLIL_SET_FLAG:
            if node.attributes:
                raise ValueError("protected caller flag write")
            self.condition(node.src)
        else:
            super().statement(node)


def _graph(il, work):
    blocks, successors = {}, {}
    for block in il.basic_blocks:
        work.tick()
        if block.arch != il.arch:
            raise ValueError("caller architecture changes across blocks")
        if block.start == block.end:
            raise ValueError("empty caller block")
        blocks[block.start] = block
        end = il[block.end - 1]
        targets = tuple(sorted({edge.target.start for edge in block.outgoing_edges if edge.target is not None}))
        if any(edge.target is None for edge in block.outgoing_edges):
            raise ValueError("unresolved caller edge")
        if end.operation == Op.LLIL_IF:
            expected = {end.true, end.false}
        elif end.operation == Op.LLIL_GOTO:
            expected = {end.dest}
        elif end.operation == Op.LLIL_RET:
            expected = set()
        else:
            expected = {block.end}
        if set(targets) != expected:
            raise ValueError("caller control-flow edges are not proved")
        successors[block.start] = targets
    ordered, predecessors = order(successors, 0, work.tick)
    return blocks, successors, ordered, predecessors


def _return(lifter, end, limits, deadline):
    arch, frame = lifter.arch, lifter.frame
    stack_name, size = arch.stack_pointer, arch.address_size
    if lifter.state.values[stack_name] != Var(stack_name, size * 8):
        raise ValueError("unbalanced caller frame")
    if end.attributes:
        raise ValueError("caller return has unmodeled attributes")
    if arch.link_reg is not None:
        returned = lifter.expr(end.dest)
        if check(returned, Var(arch.link_reg, size * 8), deadline=deadline,
                 z3_timeout_ms=limits.proof_ms).verdict is not Verdict.PROVEN:
            raise ValueError("caller return target is not preserved")
    elif end.dest.operation != Op.LLIL_POP or end.dest.size != size:
        raise ValueError("caller has no ordinary stack return")
    else:
        returned = frame.read(0, size)
        incoming = Frame(limits.frame_bytes, frame.little_endian).read(0, size)
        if check(returned, incoming, deadline=deadline, z3_timeout_ms=limits.proof_ms).verdict is not Verdict.PROVEN:
            raise ValueError("caller stack return target is not preserved")
    # Every written register is an output; private conventions can return pointers anywhere.
    for name, value in lifter.state.outputs().items():
        lifter.tick()
        if name != stack_name and any(v.name == stack_name for v in variables(value)):
            raise ValueError("caller-frame address may escape in a register")


def prove(il, func, summaries, returns, limits, deadline):
    if len(il) > limits.caller_instructions:
        return Proof(reason="caller-frame instruction limit")
    work, effects = Work(limits, deadline), Effects()
    stack_name, size = func.arch.stack_pointer, func.arch.address_size
    reserve = red_zone(func.platform.name, func.arch.name, limits.red_zone_bytes)
    try:
        blocks, successors, ordered, predecessors = _graph(il, work)
        if not limits.frame_cfg and len(ordered) != 1:
            raise ValueError("caller-frame control-flow proofs disabled")
        lifter = Caller(func, limits, deadline)
        lifter.work = work
        lifter.frame = TrackedFrame(limits.frame_bytes, lifter.frame.little_endian, effects=effects)
        initial = lifter.state, lifter.frame
        remaining = {node: len(successors[node]) for node in ordered}
        states = {}
        for start in ordered:
            work.tick()
            incoming = [states[pred] for pred in sorted(predecessors[start])] if start else [initial]
            lifter.state, lifter.frame = join(incoming, start, stack_name, work.tick)
            frame = lifter.frame
            def minimum_stack():
                offset = frame_offset(lifter.state.values[stack_name], stack_name, size * 8)
                return None if offset is None else offset - reserve
            frame.stack_position = minimum_stack
            for pred in predecessors[start]:
                remaining[pred] -= 1
                if not remaining[pred]: del states[pred]
            for node in blocks[start]:
                work.tick()
                if node.operation == Op.LLIL_CALL:
                    summary = summaries.get(node.instr_index)
                    if summary is None:
                        raise ValueError("caller-frame use crosses an unproved call")
                    value = Const(returns[node.instr_index], size * 8)
                    if summary.stack_return:
                        before = lifter.state.values[stack_name]
                        adjusted = BinOp("sub", before, Const(size, size * 8), size * 8)
                        lifter.state.write(lifter.register(stack_name, size), adjusted)
                        offset = frame_offset(adjusted, stack_name, size * 8)
                        if offset is None: raise ValueError("unknown caller stack position")
                        frame.write(offset, value)
                    else:
                        lifter.state.write(lifter.register(func.arch.link_reg, size), value)
                    frame.owner = node.instr_index
                    for inner in summary.body:
                        lifter.execute(inner)
                        if minimum_stack() is None: raise ValueError("unknown callee stack position")
                    frame.owner = None
                    if summary.stack_return:
                        lifter.expr(list(summary.lifted.instructions)[-1].dest)
                elif node.operation == Op.LLIL_RET:
                    _return(lifter, node, limits, deadline)
                elif node.operation in (Op.LLIL_IF, Op.LLIL_GOTO):
                    if node.attributes: raise ValueError("protected caller branch")
                    if node.operation == Op.LLIL_IF: lifter.condition(node.condition)
                else:
                    lifter.statement(node)
                if minimum_stack() is None:
                    raise ValueError("unknown caller stack position")
            if successors[start]:
                states[start] = lifter.state, lifter.frame
        accepted = set(effects.stores) - effects.live - effects.outside
        return Proof(frozenset(accepted), "scratch bytes are read, outside the local frame, or otherwise unproved",
                     len(ordered), limits.frame_work - work.left)
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        return Proof(reason=str(exc), work=limits.frame_work - work.left)
