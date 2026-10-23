"""Independent readback of final, straight-line HLIL local stores in the C fixtures."""
from dataclasses import dataclass
from binaryninja import HighLevelILOperation as H, TypeClass, VariableSourceType


@dataclass(frozen=True)
class Pointer:
    offset: int


class UnknownValue(Exception):
    pass


def recover(func):
    cells, memory, observed = {}, {}, []
    order = "little" if func.arch.endianness.name == "LittleEndian" else "big"

    def local(var):
        return var.source_type == VariableSourceType.StackVariableSourceType

    def address(node):
        if node.operation == H.HLIL_VAR and local(node.var): return node.var.storage
        if node.operation == H.HLIL_STRUCT_FIELD: return address(node.src) + node.offset
        if node.operation == H.HLIL_ARRAY_INDEX:
            base = Pointer(address(node.src)) if node.src.operation == H.HLIL_VAR and local(node.src.var) else value(node.src)
            assert isinstance(base, Pointer), str(node)
            return base.offset + value(node.index) * node.size
        if node.operation == H.HLIL_DEREF:
            found = value(node.src); assert isinstance(found, Pointer), str(node)
            return found.offset
        raise AssertionError(str(node))

    def value(node):
        op, bits = node.operation, node.size * 8 or 1
        mask = (1 << bits) - 1
        if op in (H.HLIL_CONST, H.HLIL_CONST_PTR): return node.constant & mask
        if op == H.HLIL_ADDRESS_OF: return Pointer(address(node.src))
        if op == H.HLIL_VAR:
            if isinstance(cells.get(node.var.identifier), Pointer): return cells[node.var.identifier]
            if local(node.var):
                if any(node.var.storage + i not in memory for i in range(node.size)): raise UnknownValue()
                return int.from_bytes(bytes(memory[node.var.storage + i] for i in range(node.size)), order)
            if node.var.identifier in cells and cells[node.var.identifier] is not None: return cells[node.var.identifier]
            raise UnknownValue()
        if op in (H.HLIL_ZX, H.HLIL_LOW_PART, H.HLIL_BOOL_TO_INT):
            found = value(node.src)
            return found if isinstance(found, Pointer) else found & mask
        if op in (H.HLIL_NEG, H.HLIL_NOT): return (-value(node.src) if op == H.HLIL_NEG else ~value(node.src)) & mask
        if op in (H.HLIL_ADD, H.HLIL_SUB):
            a, b = value(node.left), value(node.right)
            if isinstance(b, Pointer) and op == H.HLIL_ADD: a, b = b, a
            if isinstance(a, Pointer): return Pointer(a.offset + (b if op == H.HLIL_ADD else -b))
            return (a + b if op == H.HLIL_ADD else a - b) & mask
        if op in (H.HLIL_OR, H.HLIL_AND, H.HLIL_XOR, H.HLIL_MUL, H.HLIL_LSL, H.HLIL_LSR):
            a, b = value(node.left), value(node.right)
            return {H.HLIL_OR: lambda: a | b, H.HLIL_AND: lambda: a & b, H.HLIL_XOR: lambda: a ^ b,
                    H.HLIL_MUL: lambda: a * b, H.HLIL_LSL: lambda: a << b, H.HLIL_LSR: lambda: a >> b}[op]() & mask
        if op in (H.HLIL_STRUCT_FIELD, H.HLIL_ARRAY_INDEX, H.HLIL_DEREF):
            at = address(node)
            if any(at + i not in memory for i in range(node.size)): raise UnknownValue()
            return int.from_bytes(bytes(memory[at + i] for i in range(node.size)), order)
        raise AssertionError(str(node))

    def store(at, datum, size):
        if datum is None or isinstance(datum, Pointer):
            for i in range(size): memory.pop(at + i, None)
        else:
            for i, byte in enumerate((datum & ((1 << (8 * size)) - 1)).to_bytes(size, order)): memory[at + i] = byte

    def statements(root):
        if root.operation == H.HLIL_BLOCK:
            for child in root.body: yield from statements(child)
        else: yield root

    for node in statements(func.hlil.root):
        if node.operation in (H.HLIL_VAR_DECLARE, H.HLIL_NOP, H.HLIL_RET): continue
        if node.operation == H.HLIL_VAR_INIT:
            try: datum = value(node.src)
            except UnknownValue: datum = None
            cells[node.dest.identifier] = datum
            if local(node.dest): store(node.dest.storage, datum, node.size)
        elif node.operation == H.HLIL_ASSIGN:
            try: datum = value(node.src)
            except UnknownValue: datum = None
            if node.dest.operation == H.HLIL_VAR:
                cells[node.dest.var.identifier] = datum
                if local(node.dest.var): store(node.dest.var.storage, datum, node.dest.size)
            else: store(address(node.dest), datum, node.dest.size)
        elif node.operation in (H.HLIL_CALL, H.HLIL_TAILCALL):
            if len(node.params) >= 2 and node.params[1].operation == H.HLIL_CONST_DATA:
                raw = bytes(node.params[1].constant_data.data)
                size = value(node.params[2]) if len(node.params) == 3 else raw.index(0) + 1
                base = value(node.params[0]); assert isinstance(base, Pointer)
                for i, byte in enumerate(raw[:size]): memory[base.offset + i] = byte
            else:
                for parameter in node.params:
                    try: datum = value(parameter)
                    except UnknownValue: continue
                    if not isinstance(datum, Pointer): continue
                    raw, at = [], datum.offset
                    while at in memory and len(raw) < 4096:
                        byte = memory[at]; raw.append(byte); at += 1
                        if byte == 0: break
                    observed.append(bytes(raw))
        elif node.operation in (H.HLIL_CONST, H.HLIL_CONST_PTR, H.HLIL_ADDRESS_OF):
            continue
        else:
            raise AssertionError(str(node))
    return observed
