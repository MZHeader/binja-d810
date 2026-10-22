"""Pointer values in whole local stack slots. Every read and address use is checked.

All versions of a slot are treated as possibly holding the pointer, including reads before
the store. This avoids relying on a guessed memory ordering or overwriting away an escape.
Partial accesses, overlapping variables, escaped addresses and unsupported effects reject.
"""

from binaryninja import MediumLevelILOperation as Op, SSAVariable, Variable, VariableSourceType

from ...flow.uses import ADDRESS_OPS, READ, classify

_ADDRESS = (Op.MLIL_ADDRESS_OF, Op.MLIL_ADDRESS_OF_FIELD)
_READS = (Op.MLIL_VAR_SSA, Op.MLIL_VAR_SSA_FIELD, Op.MLIL_VAR_ALIASED, Op.MLIL_VAR_ALIASED_FIELD)
_WRITES = (Op.MLIL_SET_VAR_SSA, Op.MLIL_SET_VAR_SSA_FIELD, Op.MLIL_SET_VAR_ALIASED,
           Op.MLIL_SET_VAR_ALIASED_FIELD)


class Slots:
    def __init__(self):
        self.checked = {}

    def store(self, graph, node):
        if node.operation in (Op.MLIL_SET_VAR_ALIASED, Op.MLIL_SET_VAR_ALIASED_FIELD):
            var = node.dest.var
        elif node.operation in (Op.MLIL_STORE_SSA, Op.MLIL_STORE_STRUCT_SSA):
            var = self._address(graph, node.dest)
        else:
            return False
        size = graph.uses.ssa.source_function.arch.address_size
        if var is None or node.size != size or node.src.size != size or getattr(node, "offset", 0):
            return False
        return self.check(graph, var, size)

    def _address(self, graph, node, depth=0):
        if depth >= 8:
            return None
        if node.operation in _ADDRESS and not getattr(node, "offset", 0):
            return node.src
        if node.operation == Op.MLIL_VAR_SSA:
            d = graph.uses.ssa.get_ssa_var_definition(node.src)
            if d is not None and d.operation == Op.MLIL_SET_VAR_SSA and d.size == node.size:
                return self._address(graph, d.src, depth + 1)
        return None

    def check(self, graph, var, size):
        if var.identifier in self.checked:
            return self.checked[var.identifier]
        self.checked[var.identifier] = False  # recursive storage chains need a separate proof
        ssa = graph.uses.ssa
        func = ssa.source_function
        if (var.source_type != VariableSourceType.StackVariableSourceType or var.storage >= 0
                or var.type is None or var.type.width != size or var in func.parameter_vars):
            return False
        for other in ssa.non_ssa_form.vars:
            if other.identifier == var.identifier or other.source_type != var.source_type:
                continue
            width = other.type.width if other.type is not None else 0
            if (not width and other.storage < var.storage + size) or \
                    (width and other.storage < var.storage + size and var.storage < other.storage + width):
                return False
        if graph.proof._body(graph.uses) is None:
            return False
        addresses, reads = [], []
        for node in graph.uses.nodes.values():
            if node.operation in _ADDRESS and node.src.identifier == var.identifier:
                if getattr(node, "offset", 0):
                    return False
                addresses.append(node.expr_index)
            elif node.operation in _READS and node.src.var.identifier == var.identifier:
                if node.size != size or getattr(node, "offset", 0):
                    return False
                reads.append(node.expr_index)
            elif node.operation in _WRITES and node.dest.var.identifier == var.identifier:
                if node.size != size or getattr(node, "offset", 0):
                    return False
            else:
                for _, value, _ in node.detailed_operands:
                    for item in value if isinstance(value, list) else (value,):
                        held = item.var if isinstance(item, SSAVariable) else item
                        if isinstance(held, Variable) and held.identifier == var.identifier:
                            return False  # split values and unsupported references must not disappear
        address_graph = AddressGraph(graph)
        if addresses and classify(addresses, address_graph)[0] != READ:
            return False
        graph.reloads.update(reads)
        if classify(reads, graph)[0] != READ:
            return False
        self.checked[var.identifier] = True
        return True


class AddressGraph:
    """A pointer to the slot: its whole load yields the pointer whose consumers matter."""
    def __init__(self, owner):
        self.owner, self.uses = owner, owner.uses
        self.size = self.uses.ssa.source_function.arch.address_size

    def __getattr__(self, name):
        return getattr(self.uses, name)

    def kind(self, node):
        self.owner.proof.left -= 1
        if self.owner.proof.left < 0 or self.owner.proof.deadline.expired():
            return "unknown"
        kind = self.uses.kind(node)
        instr = self.uses.nodes[node]
        if instr.operation in _ADDRESS:
            # ADDRESS_OF has implicit architecture pointer width (native size is zero).
            return "var" if instr.size in (0, self.size) and not getattr(instr, "offset", 0) else "unknown"
        if kind in ADDRESS_OPS:
            return "unknown"  # only a whole slot at its exact address is supported
        if kind in ("var", "const", "set_var", "load", "store", "store_outside"):
            if instr.size != self.size or getattr(instr, "offset", 0):
                return "unknown"
            if kind in ("store", "store_outside"):
                return "store_outside"  # overwriting a local pointer does not write its pointee
        return kind

    def loaded_read_only(self, parent, node):
        self.owner.reloads.add(parent)
        return classify([parent], self.owner)[0] == READ

    def call_read_only(self, parent, node):
        call = self.uses.nodes[parent]
        summary = self.owner.proof.argument(call, node, self.owner.depth, indirect=True)
        if summary is None or summary.returned and not self.owner._result(call, node):
            return False
        self.owner.accepted_calls.add(parent)
        return True
