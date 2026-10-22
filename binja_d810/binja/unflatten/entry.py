"""Entry values carried outside a function's declared parameters."""

from binaryninja import MediumLevelILOperation as Op, RegisterValueType, SSAVariable, VariableSourceType

_TRANSFERS = frozenset({Op.MLIL_CALL, Op.MLIL_CALL_UNTYPED, Op.MLIL_TAILCALL,
                         Op.MLIL_TAILCALL_UNTYPED, Op.MLIL_JUMP, Op.MLIL_JUMP_TO, Op.MLIL_RET})


def variables(mlil, params):
    """Live non-parameter entry variables, following phis but ignoring dead phi-only webs."""
    own = {p.identifier for p in params}
    ssa = mlil.ssa_form
    for var in mlil.vars:
        if var.identifier in own:
            continue
        if next(uses(ssa, var), None) is not None:
            yield var


def uses(ssa, var):
    """Concrete uses of an entry value, including uses reached through phis."""
    work, seen, emitted = [SSAVariable(var, 0)], set(), set()
    while work:
        version = work.pop()
        if version in seen:
            continue
        seen.add(version)
        for use in ssa.get_ssa_var_uses(version):
            if use.operation == Op.MLIL_VAR_PHI:
                work.append(use.dest)
            elif use.instr_index not in emitted:
                emitted.add(use.instr_index)
                yield use


class Preserved:
    """A caller's hidden registers stay unchanged across each copied handler transition.

    Require full callee-saved registers, native entry-value evidence at transfers, and no
    overlapping handler variables. Stack slots, register aliases and missing evidence reject.
    """

    def __init__(self, mlil, params):
        self.arch = mlil.arch
        self.registers = {}
        self.supported = True
        func = mlil.source_function
        saved = set(func.calling_convention.callee_saved_regs) if func.calling_convention else set()
        for var in variables(mlil, params):
            if var.source_type != VariableSourceType.RegisterVariableSourceType:
                self.supported = False
                break
            name = self.arch.get_reg_name(var.storage)
            info = self.arch.regs.get(name)
            if info is None or name != info.full_width_reg or name not in saved:
                self.supported = False
                break
            self.registers[name] = var.storage

    def at(self, func, address: int) -> bool:
        """Native values at this transfer are the same registers that entered the function."""
        if not self.supported or func.arch != self.arch:
            return False
        for name, storage in self.registers.items():
            value = func.get_reg_value_at(address, name)
            if value.type != RegisterValueType.EntryValue or value.value != storage or value.offset != 0:
                return False
        return True

    def by(self, mlil) -> bool:
        if not self.registers:
            return self.supported
        func = mlil.source_function
        cc = func.calling_convention
        if not self.supported or func.arch != self.arch or cc is None \
                or not self.registers.keys() <= set(cc.callee_saved_regs):
            return False
        # Variables keep their identifiers when copied. Even a saved/restored local use
        # could overwrite the caller's IL variable, so reject all overlapping register storage.
        for var in (*mlil.vars, *func.parameter_vars):
            if var.source_type == VariableSourceType.RegisterVariableSourceType:
                info = self.arch.regs.get(self.arch.get_reg_name(var.storage))
                if info is None or info.full_width_reg in self.registers:
                    return False
        return all(self.at(func, i.address) for i in mlil.instructions if i.operation in _TRANSFERS)
