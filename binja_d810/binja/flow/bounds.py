"""Bound addresses through SSA copies and guarded phi inputs, without unrolling loops.

Every incoming edge of a phi must be covered. A back-edge comparison bounds the value carried
into the next iteration; the initial value is checked separately. Arithmetic crossing a modular
boundary widens to the whole result width. Limits and missing evidence also widen.
"""

from binaryninja import MediumLevelILOperation as Op

from ...deadline import Deadline
from ..ssa import reaching

_CONSTANTS = (Op.MLIL_CONST, Op.MLIL_CONST_PTR)
_READS = (Op.MLIL_VAR_SSA, Op.MLIL_VAR_SSA_FIELD)
_COMPARE = {Op.MLIL_CMP_ULT: "lt", Op.MLIL_CMP_ULE: "le", Op.MLIL_CMP_UGT: "gt",
            Op.MLIL_CMP_UGE: "ge", Op.MLIL_CMP_E: "eq", Op.MLIL_CMP_NE: "ne"}
_INVERSE = {"lt": "ge", "le": "gt", "gt": "le", "ge": "lt", "eq": "ne", "ne": "eq"}
_SWAP = {"lt": "gt", "le": "ge", "gt": "lt", "ge": "le", "eq": "eq", "ne": "ne"}


def _full(size):
    return 0, (1 << (8 * size)) - 1


class Bounds:
    def __init__(self, ssa, deadline: Deadline, limit: int = 256):
        self.ssa, self.deadline, self.left = ssa, deadline, limit
        self.cache, self.active = {}, set()

    def value(self, node, edge=None):
        full = _full(node.size)
        if node.size <= 0 or self.left <= 0 or self.deadline.expired():
            return full
        self.left -= 1
        op = node.operation
        if op in _CONSTANTS:
            v = node.constant & full[1]
            return v, v
        if op in _READS:
            if op == Op.MLIL_VAR_SSA_FIELD and node.offset:
                return full
            found = self.variable(node.src, node.size, edge)
            block = node.il_basic_block
            if block is not None and len(block.incoming_edges) == 1:
                found = self._guarded(node.src, found, block.incoming_edges[0].source, block)
            if edge is not None:
                found = self._guarded(node.src, found, *edge)
            return found if found[1] <= full[1] else full
        if op in (Op.MLIL_ZX, Op.MLIL_LOW_PART, Op.MLIL_SX):
            found = self.value(node.src, edge)
            if op == Op.MLIL_SX and found[1] >= 1 << (8 * node.src.size - 1):
                return full
            return found if found[1] <= full[1] else full
        if op in (Op.MLIL_ADD, Op.MLIL_SUB, Op.MLIL_MUL, Op.MLIL_LSL, Op.MLIL_AND):
            a, b = self.value(node.left, edge), self.value(node.right, edge)
            if op == Op.MLIL_ADD:
                found = a[0] + b[0], a[1] + b[1]
            elif op == Op.MLIL_SUB:
                found = a[0] - b[1], a[1] - b[0]
            elif op == Op.MLIL_MUL:
                found = a[0] * b[0], a[1] * b[1]
            elif op == Op.MLIL_AND:
                found = 0, min(a[1], b[1])
            elif b[0] == b[1] and b[1] < node.size * 8:
                found = a[0] << b[0], a[1] << b[1]
            else:
                return full
            modulus = full[1] + 1
            if found[0] <= found[1] and found[0] // modulus == found[1] // modulus:
                return found[0] % modulus, found[1] % modulus
            return full
        return full

    def variable(self, var, size, edge=None):
        key = (var, size, None if edge is None else tuple(b.index for b in edge))
        if key in self.cache:
            return self.cache[key]
        if key in self.active or self.left <= 0 or self.deadline.expired():
            return _full(size)
        self.left -= 1
        self.active.add(key)
        try:
            definition = self.ssa.get_ssa_var_definition(var)
            found = _full(size)
            if definition is not None and definition.operation == Op.MLIL_SET_VAR_SSA:
                if definition.size >= size:
                    found = self.value(definition.src, edge)
            elif definition is not None and definition.operation == Op.MLIL_VAR_PHI:
                incoming, covered = [], set()
                block = definition.il_basic_block
                for incoming_edge in block.incoming_edges:
                    source = reaching(self.ssa, incoming_edge.source, var.var)
                    if source not in definition.src:
                        return _full(size)
                    covered.add(source)
                    bounds = self.variable(source, size, (incoming_edge.source, block))
                    bounds = self._guarded(source, bounds, incoming_edge.source, block)
                    incoming.append(bounds)
                if incoming and covered == set(definition.src):
                    found = min(a for a, _ in incoming), max(b for _, b in incoming)
            self.cache[key] = found
            return found
        finally:
            self.active.remove(key)

    def _guarded(self, var, bounds, block, target):
        seen = set()
        for _ in range(8):
            if block.index in seen or self.deadline.expired():
                break
            seen.add(block.index)
            last = self.ssa[block.end - 1]
            if last.operation == Op.MLIL_IF and last.true != last.false:
                if target.start not in (last.true, last.false):
                    break
                atom = self._condition(last.condition, target.start == last.true)
                if atom is not None:
                    expr, relation, constant = atom
                    size = self._same(expr, var)
                    if size and bounds[1] <= _full(size)[1]:
                        lo, hi = bounds
                        if relation == "lt": hi = min(hi, constant - 1)
                        elif relation == "le": hi = min(hi, constant)
                        elif relation == "gt": lo = max(lo, constant + 1)
                        elif relation == "ge": lo = max(lo, constant)
                        elif relation == "eq": lo, hi = max(lo, constant), min(hi, constant)
                        if lo <= hi:
                            bounds = lo, hi
            elif last.operation != Op.MLIL_GOTO:
                break
            incoming = block.incoming_edges
            if len(incoming) != 1:
                break
            target, block = block, incoming[0].source
        return bounds

    def _same(self, node, var, depth=0):
        """A comparison may read a whole copy of the SSA value being bounded."""
        if depth >= 16 or self.deadline.expired():
            return 0
        if node.operation in _READS and not getattr(node, "offset", 0):
            if node.src == var:
                return node.size
            d = self.ssa.get_ssa_var_definition(node.src)
            if d is not None and d.operation == Op.MLIL_SET_VAR_SSA:
                size = self._same(d.src, var, depth + 1)
                return min(node.size, size)
        if node.operation in (Op.MLIL_ZX, Op.MLIL_LOW_PART):
            return min(node.size, self._same(node.src, var, depth + 1))
        return 0

    def _condition(self, node, truth, depth=0):
        if depth >= 16 or self.deadline.expired():
            return None
        op = node.operation
        if op == Op.MLIL_VAR_SSA:
            d = self.ssa.get_ssa_var_definition(node.src)
            if d is not None and d.operation == Op.MLIL_SET_VAR_SSA:
                return self._condition(d.src, truth, depth + 1)
        if op in (Op.MLIL_ZX, Op.MLIL_LOW_PART, Op.MLIL_BOOL_TO_INT):
            return self._condition(node.src, truth, depth + 1)
        if op == Op.MLIL_AND and node.right.operation in _CONSTANTS and node.right.constant == 1:
            return self._condition(node.left, truth, depth + 1)
        if op in _COMPARE:
            left, right, relation = node.left, node.right, _COMPARE[op]
            if left.operation in _CONSTANTS:
                left, right, relation = right, left, _SWAP[relation]
            if right.operation not in _CONSTANTS:
                return None
            constant = right.constant & _full(right.size)[1]
            if relation in ("eq", "ne") and constant == 0:
                nested = self._condition(left, truth == (relation == "ne"), depth + 1)
                if nested is not None:
                    return nested
            return left, relation if truth else _INVERSE[relation], constant
        return None

    def misses(self, store, start, end):
        lo, hi = self.value(store.dest)
        offset = store.offset if store.operation == Op.MLIL_STORE_STRUCT_SSA else 0
        lo, hi = lo + offset, hi + offset
        # Include every byte, and reject address-space wraparound.
        return (store.size > 0 and 0 <= lo <= hi and hi + store.size <= 1 << (8 * store.dest.size)
                and (hi + store.size <= start or lo >= end))
