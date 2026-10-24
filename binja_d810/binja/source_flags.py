"""Expand subtraction overflow from original flag-setting IL, before flag lowering.

This never assigns guessed semantics to ADD_OVERFLOW. Original instructions and their flags
remain; an exact redundant overflow write expresses the primitive subtraction's semantics.
"""
from dataclasses import dataclass, field
import time
from binaryninja import (FlagRole, ILInstructionAttribute as Attr, ILSourceLocation, LLIL_TEMP,
                         LowLevelILFunction, LowLevelILOperation as L)
from ..deadline import Deadline
from ..flags.semantics import subtract_overflow_bits, subtract_overflow_wide
from ..flags.liveness import live_after
from ..ir.expr import BinOp, Const, Var
from ..semantic.verify import Verdict, check
from .callees.rewrite import _copy

# Native semantic-class metadata for integer subtraction, verified on the supported targets.
INTEGER_CLASSES = {'aarch64': {'int'}, 'x86_64': {None}, 'x86': {None}}
PURE = {L.LLIL_REG,L.LLIL_CONST,L.LLIL_CONST_PTR,L.LLIL_NEG,L.LLIL_NOT,L.LLIL_ZX,L.LLIL_SX,
        L.LLIL_LOW_PART,L.LLIL_ADD,L.LLIL_SUB,L.LLIL_MUL,L.LLIL_AND,L.LLIL_OR,L.LLIL_XOR,
        L.LLIL_LSL,L.LLIL_LSR,L.LLIL_ASR,L.LLIL_ROL,L.LLIL_ROR}
PROTECTED = {Attr.ILPreventDeadStoreElimination,Attr.ILPreventAliasAnalysis,Attr.SrcInstructionUsesPointerAuth}


@dataclass
class Report:
    models: int = 0
    notes: list = field(default_factory=list)
    seconds: float = 0.0


def run(il, limits, deadline=Deadline.never(), max_bits=64):
    started = time.monotonic(); report = Report(); left = limits.work
    def tick():
        nonlocal left
        left -= 1
        if left < 0 or deadline.expired(): raise ValueError('source flag proof budget exhausted')
    def walk(root):
        pending = [root]
        while pending:
            tick(); node = pending.pop(); yield node
            for value in node.operands:
                if isinstance(value, list):
                    pending.extend(n for n in value if hasattr(n, 'operation'))
                elif hasattr(value, 'operation'):
                    pending.append(value)
    try:
        if il.arch.name not in INTEGER_CLASSES: return il, report
        if len(il) > limits.instructions: raise ValueError('source flag instruction limit')
        needed, raw, reads, writes = set(), set(), {}, {}
        for instruction in il.instructions:
            reads[instruction.instr_index], writes[instruction.instr_index] = set(), set()
            for node in walk(instruction):
                writes[instruction.instr_index].update(il.arch.flags_written_by_flag_write_type.get(node.flags,()))
                if node.operation == L.LLIL_SET_FLAG: writes[instruction.instr_index].add(str(node.dest))
                if node.operation in (L.LLIL_FLAG,L.LLIL_FLAG_BIT): raw.add(str(node.src))
                elif node.operation == L.LLIL_FLAG_GROUP:
                    reads[instruction.instr_index].update(il.arch.flags_required_for_semantic_flag_group.get(node.semantic_group.name,()))
                elif node.operation == L.LLIL_FLAG_COND:
                    cls = node.semantic_class.name if node.semantic_class else None
                    reads[instruction.instr_index].update(il.arch.get_flags_required_for_flag_condition(node.condition,cls))
            needed.update(reads[instruction.instr_index])
        if not needed: return il, report
        edges = {}
        for block in il.basic_blocks:
            for instruction in block:
                index = instruction.instr_index
                edges[index] = (index+1,) if index+1 < block.end else tuple(e.target.start for e in block.outgoing_edges if e.target is not None)
        live = live_after(edges,reads,writes,tick)
        plans, proofs = {}, {}
        for instruction in il.instructions:
            tick()
            source = instruction.src if instruction.operation == L.LLIL_SET_REG else instruction
            if source.operation != L.LLIL_SUB or not source.flags: continue
            classes = il.arch.semantic_class_for_flag_write_type
            if source.flags not in classes or classes[source.flags] not in INTEGER_CLASSES[il.arch.name]: continue
            written = il.arch.flags_written_by_flag_write_type.get(source.flags, ())
            flags = [f for f in written if f in live.get(instruction.instr_index,()) and il.arch.get_flag_role(f,classes[source.flags]) == FlagRole.OverflowFlagRole]
            if not flags: continue
            if raw & set(written):
                report.notes.append(f'{instruction.address:#x}: raw flag data use keeps the original flag source'); continue
            if instruction.attributes & PROTECTED or source.attributes & PROTECTED:
                report.notes.append(f'{instruction.address:#x}: protected flag source stays unchanged'); continue
            width = source.size * 8
            if not 0 < width <= min(max_bits,64):
                report.notes.append(f'{instruction.address:#x}: source flag scalar width limit'); continue
            if source.left.size != source.size or source.right.size != source.size:
                report.notes.append(f'{instruction.address:#x}: subtraction operand widths differ'); continue
            if instruction is not source and instruction.flags: continue
            operands = list(walk(source.left)) + list(walk(source.right))
            if len(operands) > limits.nodes: raise ValueError('source flag expression limit')
            if any(n.operation not in PURE or n.flags or n.attributes & PROTECTED for n in operands):
                report.notes.append(f'{instruction.address:#x}: subtraction operands have unmodeled effects'); continue
            if width not in proofs:
                a,b = Var('left',width),Var('right',width)
                expression = subtract_overflow_bits(a,b)
                proof = check(expression,subtract_overflow_wide(a,b),z3_timeout_ms=limits.proof_ms,deadline=deadline)
                proofs[width] = expression if proof.verdict is Verdict.PROVEN else None
                if proofs[width] is None:
                    report.notes.append(f'{instruction.address:#x}: {width}-bit subtraction overflow was not proven: {proof.reason or proof.verdict.value}')
            if proofs[width] is None: continue
            if len(plans) >= limits.conditions: raise ValueError('source flag model count limit')
            plans[instruction.instr_index] = source,flags,proofs[width]
        if not plans: return il, report
        new = LowLevelILFunction(il.arch,source_func=il.source_function)
        new.prepare_to_copy_function(il)
        temporary = il.temp_reg_count
        def lower(expr, bindings, loc):
            tick()
            if isinstance(expr,Var): return new.reg(expr.width//8,bindings[expr.name],loc)
            if isinstance(expr,Const): return new.const(expr.width//8,expr.value,loc)
            assert isinstance(expr,BinOp)
            a,b = lower(expr.left,bindings,loc),lower(expr.right,bindings,loc)
            make = {'sub':new.sub,'and':new.and_expr,'xor':new.xor_expr,'ne':new.compare_not_equal}[expr.op]
            return make(expr.left.width//8 if expr.op=='ne' else expr.width//8,a,b,loc=loc)
        for block in il.basic_blocks:
            new.prepare_to_copy_block(block)
            for instruction in block:
                tick(); new.set_current_address(instruction.address,block.arch)
                loc = ILSourceLocation.from_instruction(instruction)
                plan = plans.get(instruction.instr_index)
                if plan:
                    source,flags,expression = plan
                    bindings = {'left':LLIL_TEMP(temporary),'right':LLIL_TEMP(temporary+1)}; temporary += 2
                    new.append(new.set_reg(source.size,bindings['left'],_copy(source.left,new),loc=loc))
                    new.append(new.set_reg(source.size,bindings['right'],_copy(source.right,new),loc=loc))
                new.append(_copy(instruction,new))
                if plan:
                    for flag in flags: new.append(new.set_flag(flag,lower(expression,bindings,loc),loc=loc))
        new.finalize()
        report.models = len(plans)
        report.notes.insert(0,f'{len(plans)} subtraction overflow sources expanded with exact-width proofs')
        return new,report
    except (ValueError,NotImplementedError,RecursionError) as exc:
        report.notes.append(f'left unchanged: {exc}'); return il,report
    finally: report.seconds = time.monotonic()-started
