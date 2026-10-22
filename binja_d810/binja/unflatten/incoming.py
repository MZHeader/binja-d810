"""Bindings for handler inputs omitted from its declared argument list."""

from binaryninja import MediumLevelILOperation as Op, RegisterValueType as Value, VariableSourceType as Source

from ..effects import mlil_effect
from ..flow.collect import branch_instruction
from . import entry, selfcall


def _size(var):
    type_ = var.type
    return type_.width if type_ is not None else 0


def _register(arch, var):
    if var.source_type != Source.RegisterVariableSourceType:
        return None
    name = arch.get_reg_name(var.storage)
    info = arch.regs.get(name)
    return None if info is None else info.full_width_reg


def _entry_at(func, address, storage):
    value = func.get_reg_value_at(address, storage)
    return value.type == Value.EntryValue and value.value == storage and value.offset == 0


def _tail_frame(func, instr):
    """An actual tail branch with the same stack base as this function's entry."""
    if (instr.operation != Op.MLIL_TAILCALL or func.arch.stack_pointer is None
            or func.get_call_type_adjustment(instr.address) is not None):
        return False
    value = func.get_reg_value_at(instr.address, func.arch.stack_pointer)
    return (value.type == Value.StackFrameOffset and value.value == value.offset == 0
            and branch_instruction(func, instr.address) is not None)


class Inputs:
    def __init__(self, caller, mlil):
        self.caller, self.mlil = caller, mlil
        self.params = tuple(caller.parameter_vars)
        self.registers = tuple(entry.variables(mlil, mlil.source_function.parameter_vars))
        self.returns = {}
        self.supported = caller.arch == mlil.arch and self._registers() and self._returns()

    def _registers(self):
        if not self.registers:
            return True
        callee, arch = self.mlil.source_function, self.caller.arch
        if callee.arch != arch or self.caller.calling_convention is None or callee.calling_convention is None:
            return False
        saved = set(self.caller.calling_convention.callee_saved_regs) & set(callee.calling_convention.callee_saved_regs)
        own = {_register(arch, p) for p in callee.parameter_vars}
        for var in self.registers:
            name = _register(arch, var)
            if name not in saved or name in own or name in (arch.stack_pointer, arch.link_reg) or var.index != 0:
                return False
            info = arch.regs[name]
            matching = [p for p in self.params if _register(arch, p) == name]
            if (len(matching) != 1 or matching[0].identifier != var.identifier
                    or arch.get_reg_name(var.storage) != name
                    or _size(var) != info.size or _size(matching[0]) != info.size):
                return False
            if not all(_entry_at(callee, use.address, var.storage) for use in entry.uses(self.mlil.ssa_form, var)):
                return False
        return True

    def _returns(self):
        for instr in self.mlil.instructions:
            if instr.operation not in selfcall._CALLS | selfcall._TAILS or instr.dest.operation not in selfcall._CONSTANTS \
                    or instr.dest.constant != self.caller.start:
                continue
            args = list(getattr(instr, "params", []))
            if not args or len(args) == len(self.params):
                continue  # the existing explicit or untyped path already covers these
            n = len(args)
            if (n >= len(self.params) or not _tail_frame(self.mlil.source_function, instr)
                    or any(p.source_type != Source.RegisterVariableSourceType for p in self.params[:n])
                    or any(p.source_type != Source.StackVariableSourceType for p in self.params[n:])):
                return False
            self.returns[instr.instr_index] = self.params[n:]
        return not self.returns or (self._pure_body() and self._stack_slots())

    def _pure_body(self):
        """Only pure register/stack-variable code: no memory accesses, frame addresses or helper calls."""
        for instr in self.mlil.instructions:
            if instr.operation in (Op.MLIL_NOP, Op.MLIL_GOTO):
                continue
            if instr.operation in (Op.MLIL_SET_VAR, Op.MLIL_SET_VAR_FIELD):
                exprs = (instr.src,)
            elif instr.operation == Op.MLIL_IF:
                exprs = (instr.condition,)
            elif instr.operation == Op.MLIL_TAILCALL and instr.dest.operation in selfcall._CONSTANTS \
                    and instr.dest.constant == self.caller.start:
                exprs = instr.params
            else:
                return False
            for expr in exprs:
                if mlil_effect(expr) is not None or any(expr.traverse(
                        lambda node: True if node.operation in selfcall._ADDRESSES else None)):
                    return False
        return True

    def _stack_slots(self):
        params = [p for p in self.params if p.source_type == Source.StackVariableSourceType]
        slots = []
        for p in params:
            size = _size(p)
            if p.storage < 0 or not size:
                return False
            slots.append((p.storage, p.storage + size, p.identifier))
        slots.sort()
        if any(left[1] > right[0] for left, right in zip(slots, slots[1:])):
            return False
        for var in self.mlil.vars:
            if var.source_type != Source.StackVariableSourceType:
                continue
            size = _size(var)
            if not size:
                return False
            for start, end, identifier in slots:
                if var.storage < end and start < var.storage + size:
                    if var.identifier != identifier or size != end - start:
                        return False
        return True

    def at(self, instr):
        """The caller supplies the same whole incoming registers; shared-stack returns also need a tail entry."""
        if not self.supported or (self.returns and not _tail_frame(self.caller, instr)):
            return False
        ssa = instr.ssa_form if self.registers else None
        for var in self.registers:
            held = instr.get_var_for_reg(var.storage)
            if held is None or held.identifier != var.identifier or ssa.get_ssa_var_version(held) != 0:
                return False
            if not _entry_at(self.caller, instr.address, var.storage):
                return False
        return True
