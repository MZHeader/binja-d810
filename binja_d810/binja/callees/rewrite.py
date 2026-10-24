"""Call-site substitution using exact IL copies, retaining flag and call-frame effects.

The scalar summary certifies the body; copying its operations preserves implicit flag writes
as well as every declared and undeclared register result. No calling convention is consulted.
The architecture's call/return mechanics are a separate, deliberately small boundary.
"""

from dataclasses import dataclass

from binaryninja import (
    ILInstructionAttribute as Attr, ILSourceLocation, LowLevelILFunction, LowLevelILInstruction,
    LowLevelILOperation as Op,
)

from ...deadline import Deadline
from ...summaries.verify import identical
from .lift import Factory, Limits, Nested
from . import frame
from .calls import CALL_MODELS, return_address, target


# These are call-link semantics, not instruction patterns. Other architectures remain unchanged
# until their link-address rules have an independent fixture (e.g. mode bits and delay slots).
@dataclass(frozen=True)
class Site:
    index: int
    summary: object
    return_address: int


def plan(il, func, limits: Limits, deadline):
    sites, notes = {}, []
    if len(il) > limits.caller_instructions:
        return sites, ["caller instruction limit"]
    factory = Factory(limits, deadline)
    considered = 0
    mode = CALL_MODELS.get(func.arch.name)
    if mode is None:
        return sites, ["unsupported call-link semantics"]
    for instruction in il.instructions:
        if deadline.expired():
            notes.append("time budget exhausted")
            break
        if instruction.operation != Op.LLIL_CALL:
            continue
        considered += 1
        if considered > limits.sites:
            notes.append("callee call-site limit")
            break
        try:
            callee = target(func, instruction)
            ret = return_address(func, instruction)
        except ValueError as exc:
            notes.append(f"{instruction.address:#x}: {exc}")
            continue
        result = factory.get(callee)
        if result.summary is None:
            notes.append(f"{instruction.address:#x}: {result.reason}")
            continue
        summary = result.summary
        if summary.stack_return != (mode == "stack"):
            notes.append(f"{instruction.address:#x}: return does not match call-link semantics")
            continue
        sites[instruction.instr_index] = Site(instruction.instr_index, summary, ret)
    needing = {i for i, site in sites.items() if site.summary.frame_outputs}
    if needing:
        proof = frame.prove(il, func, {i: s.summary for i, s in sites.items()},
                            {i: s.return_address for i, s in sites.items()}, limits, deadline)
        if proof.accepted & needing:
            notes.append(f"caller-frame proof: {len(proof.accepted & needing)} scratch call sites; "
                         f"{proof.blocks} blocks checked once; {proof.work} work visits")
        for i in needing - proof.accepted:
            notes.append(f"{il[i].address:#x}: {proof.reason}")
            del sites[i]
    return sites, notes


def _copy(node, new):
    # The SDK's copy_to does not copy optional attributes, including volatile/atomic metadata.
    out = node.copy_to(new, lambda child: _copy(child, new))
    new.set_expr_attributes(out, node.attributes)
    return out


def rewrite(il, sites, limits=Limits(), deadline=Deadline.never()):
    """Only ordinary returns are removed; their pop/read effects remain, as in native inlining."""
    if len(il) > limits.caller_instructions:
        raise ValueError("caller instruction limit")
    new = LowLevelILFunction(il.arch, source_func=il.source_function)
    new.prepare_to_copy_function(il)
    for block in il.basic_blocks:
        new.prepare_to_copy_block(block)
        for instr in block:
            if deadline.expired():
                raise ValueError("time budget exhausted before substitution completed")
            new.set_current_address(instr.address, block.arch)
            site = sites.get(instr.instr_index)
            if site is None:
                new.append(_copy(instr, new))
                continue
            _emit_call(new, instr, site.summary, site.return_address, limits, deadline)
    new.finalize()
    return new


def _emit_call(new, instr, summary, ret, limits, deadline):
    loc = ILSourceLocation.from_instruction(instr)
    address = new.const_pointer(new.arch.address_size, ret, loc)
    if summary.stack_return:
        link = new.push(new.arch.address_size, address, loc=loc)
    else:
        link = new.set_reg(new.arch.address_size, new.arch.link_reg, address, loc=loc)
    # Matches native inlining: a used return-address value stays; only a dead link may go.
    new.set_expr_attributes(link, {Attr.ILAllowDeadStoreElimination})
    new.append(link)
    for inner in summary.body:
        if isinstance(inner, Nested):
            _emit_call(new, inner.node, inner.summary, inner.return_address, limits, deadline)
            continue
        copied = _copy(inner, new)
        if not identical(inner, LowLevelILInstruction.create(new, copied), limits.nodes, deadline):
            raise ValueError("callee copy failed exact structural proof")
        new.append(copied)
    operand = list(summary.lifted.instructions)[-1].dest
    copied = _copy(operand, new)
    if not identical(operand, LowLevelILInstruction.create(new, copied), limits.nodes, deadline):
        raise ValueError("return copy failed exact structural proof")
    new.append(copied)
