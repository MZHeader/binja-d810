"""Prove scratch writes dead in a bounded straight-line caller, byte by byte.

All calls must have body summaries. Unknown effects and control-flow splits fail closed;
the general CFG liveness pass can extend this boundary without weakening the proof here.
Writes are tracked through complete overwrites. A partial overwrite never kills other bytes.
"""

from binaryninja import LowLevelILOperation as Op

from ...ir.expr import BinOp, Const, Var, variables
from ...summaries.frame import Frame
from ...summaries.abi import red_zone
from ...semantic.verify import Verdict, check
from .lift import Lifter, frame_offset


class TrackedFrame(Frame):
    def __init__(self, *args):
        super().__init__(*args)
        self.owner = None
        self.origins = {}
        self.live = set()
        self.stores = {}
        self.outside = set()
        self.stack_position = lambda: None

    def read(self, offset, size):
        for at in range(offset, offset + size):
            writer = self.origins.get(at)
            if writer is not None and writer != self.owner:
                self.live.add(writer)
        return super().read(offset, size)

    def write(self, offset, value):
        super().write(offset, value)
        if self.owner is not None:
            stack = self.stack_position()
            if stack is None or offset < stack or offset + value.width // 8 > 0:
                self.outside.add(self.owner)
        for at in range(offset, offset + value.width // 8):
            self.origins[at] = self.owner
            if self.owner is not None:
                self.stores.setdefault(self.owner, set()).add(at)


def prove(il, func, summaries, returns, limits, deadline):
    """Returns accepted call indices and a reason. No guessed call parameters or alias flags."""
    if len(il) > limits.caller_instructions:
        return set(), "caller-frame instruction limit"
    lifter = Lifter(func, limits, deadline)
    lifter.frame = frame = TrackedFrame(limits.frame_bytes, lifter.frame.little_endian)
    stack_name, size = func.arch.stack_pointer, func.arch.address_size
    reserve = red_zone(func.platform.name, func.arch.name, limits.red_zone_bytes)
    stack_reg = lifter.register(stack_name, size)
    def minimum_stack():
        offset = frame_offset(lifter.state.values[stack_name], stack_name, size * 8)
        return None if offset is None else offset - reserve
    frame.stack_position = minimum_stack
    lowest = -reserve
    instructions = list(il.instructions)
    if not instructions or instructions[-1].operation != Op.LLIL_RET:
        return set(), "caller-frame control flow is not a single return"
    try:
        for node in instructions[:-1]:
            if node.operation == Op.LLIL_CALL:
                summary = summaries.get(node.instr_index)
                if summary is None:
                    return set(), "caller-frame use crosses an unproved call"
                value = Const(returns[node.instr_index], size * 8)
                if summary.stack_return:
                    before = lifter.state.values[stack_name]
                    adjusted = BinOp("sub", before, Const(size, size * 8), size * 8)
                    lifter.state.write(stack_reg, adjusted)
                    offset = frame_offset(adjusted, stack_name, size * 8)
                    if offset is None:
                        return set(), "unknown caller stack position"
                    frame.write(offset, value)
                else:
                    lifter.state.write(lifter.register(func.arch.link_reg, size), value)
                frame.owner = node.instr_index
                for inner in summary.body:
                    lifter.execute(inner)
                    offset = frame_offset(lifter.state.values[stack_name], stack_name, size * 8)
                    if offset is None:
                        return set(), "unknown callee stack position"
                    lowest = min(lowest, offset - reserve)
                frame.owner = None
                if summary.stack_return:
                    lifter.expr(list(summary.lifted.instructions)[-1].dest)
            else:
                lifter.statement(node)
            offset = frame_offset(lifter.state.values[stack_name], stack_name, size * 8)
            if offset is None:
                return set(), "unknown caller stack position"
            lowest = min(lowest, offset - reserve)
        if lifter.state.values[stack_name] != Var(stack_name, size * 8):
            return set(), "unbalanced caller frame"
        end = instructions[-1]
        if end.attributes:
            return set(), "caller return has unmodeled attributes"
        if func.arch.link_reg is not None:
            returned = lifter.expr(end.dest)
            if check(returned, Var(func.arch.link_reg, size * 8), deadline=deadline,
                     z3_timeout_ms=limits.proof_ms).verdict is not Verdict.PROVEN:
                return set(), "caller return target is not preserved"
        elif end.dest.operation != Op.LLIL_POP or end.dest.size != size:
            return set(), "caller has no ordinary stack return"
        else:
            frame.read(0, size)  # return-address reads matter for byte liveness too
        # Any frame-derived register result could escape through the private convention too.
        for name, value in lifter.state.outputs().items():
            if name != stack_name and any(v.name == stack_name for v in variables(value)):
                return set(), "caller-frame address may escape in a register"
        accepted = {index for index, addresses in frame.stores.items()
                    if index not in frame.live | frame.outside and all(lowest <= at < 0 for at in addresses)}
        return accepted, "scratch bytes are read, outside the local frame, or otherwise unproved"
    except (ValueError, KeyError, TypeError) as exc:
        return set(), str(exc)
