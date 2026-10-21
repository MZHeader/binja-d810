"""Address-use evidence for references that MLIL folded away. LLIL SSA retains dead loads and address
construction, so the same bounded use policy can still distinguish reads from writes and escapes. An arm64 adrp
names a page: a neighbour's access through it, at a constant address outside the object, does not use it, nor does
the page plus a neighbour's offset: an address starting another data variable past the object."""

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
_ARITHMETIC = frozenset({"add"})
_PAGE = 0x1000  # an arm64 adrp's granule
_CONSTANT = ("ConstantValue", "ConstantPointerValue")


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
        self._has_return = None
        self.object = None  # (start, end) while read_only asks about it
        self.only_outside = False

    @property
    def has_return(self):
        if self._has_return is None:
            self._has_return = any(i.operation == Op.LLIL_RET for i in self.ssa.instructions)
        return self._has_return

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
        instr = self.nodes[node]
        kind = _KINDS.get(instr.operation, "unknown")
        if self.object is not None:
            if kind in ("load", "store") and self._misses(instr, kind):
                return kind + "_outside"
            if kind in _ARITHMETIC and self._past(instr):
                return "address_outside"
        if self.only_outside and kind == "load":
            return "unknown"  # a possible read of the changed bytes must still be reanalyzed
        return kind

    def _misses(self, instr, kind) -> bool:
        """The access is at a constant address and none of its bytes is the object's."""
        address = instr.src if kind == "load" else instr.dest
        try:
            value = address.value
        except Exception:  # noqa: BLE001 - no dataflow there
            return False
        if value.type.name not in _CONSTANT:
            return False
        start, end = self.object
        at = value.value & ((1 << (8 * address.size)) - 1)
        return at + instr.size <= start or at >= end

    def _past(self, instr) -> bool:
        """An add of an offset to a page base (an arm64 adrp) that is the object's own start, reaching a constant
        that starts another data variable past the object and the data variable holding it: the page names the
        object only by coincidence; the sum is a neighbour's address. An offset from the object's own address
        (not a page) may still index back into it."""
        if instr.operation != Op.LLIL_ADD:
            return False
        try:
            value, bases = instr.value, [o.value for o in (instr.left, instr.right)]
        except Exception:  # noqa: BLE001 - no dataflow there
            return False
        view, (start, end) = self.func.view, self.object
        if value.type.name not in _CONSTANT or not any(
                b.type.name in _CONSTANT and b.value % _PAGE == 0 and start <= b.value < end for b in bases):
            return False
        at = value.value & ((1 << (8 * instr.size)) - 1)
        holder = view.get_data_var_at(start)
        if holder is not None and holder.type is not None:
            end = max(end, holder.address + holder.type.width)
        neighbour = view.get_data_var_at(at)
        return at >= end and neighbour is not None and neighbour.address == at

    def parent(self, node):
        return self.parents.get(node)

    def readers(self, node):
        found = []
        for reg in self.definitions.get(node, []):
            # LLIL RET does not list the function's returned registers as explicit operands. Do not
            # mistake a returned address for a dead value, even if it also fed a load along the way.
            if (not self.return_regs or reg.reg.name in self.return_regs) and self.has_return:
                return [-1]
            for use in self.ssa.get_ssa_reg_uses(reg):
                self.index(use)
            found.extend(self.reads.get(_key(reg), []))
        return found or [-1]  # missing consumers are not evidence of a read

    def read_only(self, address: int, start: int, end: int) -> bool:
        self.object = (start, end)
        try:
            return self._read_only(address, start, end)
        finally:
            self.object = None

    def outside(self, address: int, first: int, start: int, end: int) -> bool:
        """Every use of this reference stays outside the changed bytes. Unknown addresses, escapes,
        missing uses and overlapping reads all fail this proof, so their functions remain affected.
        `first` includes references to a containing object's base, before the changed span.
        """
        self.object, self.only_outside = (start, end), True
        try:
            return self._read_only(address, first, end)
        finally:
            self.object, self.only_outside = None, False

    def _read_only(self, address: int, start: int, end: int) -> bool:
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
