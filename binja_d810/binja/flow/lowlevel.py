"""Address-use evidence for references that MLIL folded away. LLIL SSA retains dead loads and address
construction, so the same bounded use policy can still distinguish reads from writes and escapes."""

from binaryninja import LowLevelILOperation as Op
from binaryninja.lowlevelil import SSARegister

from ...flow.uses import READ, classify

_KINDS = {
    Op.LLIL_LOAD_SSA: "load", Op.LLIL_STORE_SSA: "store", Op.LLIL_SET_REG_SSA: "set_var",
    Op.LLIL_REG_PHI: "phi", Op.LLIL_REG_SSA: "var", Op.LLIL_CONST: "const", Op.LLIL_CONST_PTR: "const",
    Op.LLIL_ADD: "add", Op.LLIL_SUB: "sub", Op.LLIL_MUL: "mul", Op.LLIL_LSL: "shl",
    Op.LLIL_ZX: "zx", Op.LLIL_SX: "sx", Op.LLIL_LOW_PART: "trunc",
}
_ADDRESS = frozenset({"const", "var", "add", "sub", "mul", "shl", "zx", "sx", "trunc"})


def _key(reg):
    return reg.reg.name, reg.version


class Uses:
    def __init__(self, func):
        self.func = func
        self.ssa = func.llil.ssa_form
        self.nodes, self.parents, self.definitions, self.reads = {}, {}, {}, {}
        self.indexed = set()
        self.return_regs = set(func.return_regs)
        cc = func.calling_convention
        if cc is not None:
            self.return_regs.update(r for r in (cc.int_return_reg, cc.high_int_return_reg) if r is not None)
        self.has_return = any(i.operation == Op.LLIL_RET for i in self.ssa.instructions)

    def index(self, statement):
        if statement.expr_index in self.indexed:
            return
        self.indexed.add(statement.expr_index)
        stack = [(statement, None)]
        while stack:
            node, parent = stack.pop()
            at = node.expr_index
            self.nodes[at] = node
            if parent is not None:
                self.parents[at] = parent
            for name, value, _ in node.detailed_operands:
                for item in value if isinstance(value, list) else (value,):
                    if hasattr(item, "operation"):
                        stack.append((item, (at, name)))
                    elif isinstance(item, SSARegister):
                        if name in ("dest", "output") and self.kind(at) in ("set_var", "phi"):
                            self.definitions.setdefault(at, []).append(item)
                        else:
                            self.reads.setdefault(_key(item), []).append(at)

    def kind(self, node):
        if node == -1:
            return "unknown"
        return _KINDS.get(self.nodes[node].operation, "unknown")

    def parent(self, node):
        return self.parents.get(node)

    def readers(self, node):
        found = []
        for reg in self.definitions.get(node, []):
            # LLIL RET does not list the function's returned registers as explicit operands. Do not
            # mistake a returned address for a dead value, even if it also fed a load along the way.
            if self.has_return and (not self.return_regs or reg.reg.name in self.return_regs):
                return [-1]
            for use in self.ssa.get_ssa_reg_uses(reg):
                self.index(use)
            found.extend(self.reads.get(_key(reg), []))
        return found or [-1]  # missing consumers are not evidence of a read

    def read_only(self, address: int, start: int, end: int) -> bool:
        starts = set()
        for instr in self.func.get_llils_at(address):
            instr = instr.ssa_form
            self.index(instr)
            stack = [instr]
            while stack:
                node = stack.pop()
                if self.kind(node.expr_index) in _ADDRESS:
                    value = node.value
                    if value.type.name in ("ConstantValue", "ConstantPointerValue") and start <= value.value < end:
                        starts.add(node.expr_index)
                for _, value, _ in node.detailed_operands:
                    stack.extend(v for v in (value if isinstance(value, list) else (value,))
                                 if hasattr(v, "operation"))
        return bool(starts) and classify(starts, self)[0] == READ
