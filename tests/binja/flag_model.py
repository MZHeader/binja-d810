"""Independent symbolic execution of the small C flag fixtures' final structured HLIL.

This test oracle does not use D810's lifters, flag models or comparison reconstruction.
Branches are explored rather than treated as unrelated phi inputs.
"""

import z3
from binaryninja import HighLevelILOperation as H, TypeClass


def sized(value, width):
    return z3.Extract(width - 1, 0, value) if value.size() > width else \
        z3.ZeroExt(width - value.size(), value) if value.size() < width else value


def value(node, state):
    op, width = node.operation, node.size * 8 or 1
    if op == H.HLIL_DEREF_FIELD and node.expr_type.type_class == TypeClass.ArrayTypeClass:
        return value(node.src, state) + node.offset  # array-to-pointer decay, not a scalar memory read
    if op == H.HLIL_ADDRESS_OF:
        def address(item):
            if item.operation == H.HLIL_DEREF:
                return value(item.src, state)
            if item.operation == H.HLIL_DEREF_FIELD:
                return value(item.src, state) + item.offset
            if item.operation == H.HLIL_STRUCT_FIELD:
                return address(item.src) + item.offset
            if item.operation == H.HLIL_ARRAY_INDEX:
                base = address(item.src)
                return base + sized(value(item.index, state), base.size()) * item.size
            raise AssertionError(f'unsupported lvalue address: {item}')
        return address(node.src)
    if op in (H.HLIL_CONST, H.HLIL_CONST_PTR):
        return z3.BitVecVal(node.constant, width)
    if op == H.HLIL_IMPORT:
        return z3.BitVec("import_" + str(node.constant), width)
    if op == H.HLIL_VAR:
        return sized(state.setdefault(node.var.identifier, z3.BitVec("uninitialized_" + str(node.var.identifier),
                                                                   max(width, 8 * node.var.type.width if node.var.type else width))), width)
    if op == H.HLIL_STRUCT_FIELD:
        return sized(z3.LShR(value(node.src, state), node.offset * 8), width)
    if op == H.HLIL_SPLIT:
        return z3.Concat(value(node.high,state),value(node.low,state))
    if op in (H.HLIL_ZX, H.HLIL_LOW_PART, H.HLIL_BOOL_TO_INT):
        return sized(value(node.src, state), width)
    if op == H.HLIL_SX:
        source = value(node.src, state)
        return z3.SignExt(width - source.size(), source)
    if op in (H.HLIL_NOT, H.HLIL_NEG):
        source = sized(value(node.src, state), width)
        return ~source if op == H.HLIL_NOT else -source
    left, right = value(node.left, state), value(node.right, state)
    if op in (H.HLIL_MULU_DP,H.HLIL_MULS_DP):
        a,b=sized(left,width),sized(right,width)
        extend=z3.SignExt if op==H.HLIL_MULS_DP else z3.ZeroExt
        return extend(width,a)*extend(width,b)
    if op.name.startswith("HLIL_CMP_"):
        width = node.size * 8 or max(left.size(), right.size())
        a, b = sized(left, width), sized(right, width)
        condition = {H.HLIL_CMP_E: lambda: a == b, H.HLIL_CMP_NE: lambda: a != b,
                     H.HLIL_CMP_SLT: lambda: a < b, H.HLIL_CMP_SLE: lambda: a <= b,
                     H.HLIL_CMP_SGT: lambda: a > b, H.HLIL_CMP_SGE: lambda: a >= b,
                     H.HLIL_CMP_ULT: lambda: z3.ULT(a, b), H.HLIL_CMP_ULE: lambda: z3.ULE(a, b),
                     H.HLIL_CMP_UGT: lambda: z3.UGT(a, b), H.HLIL_CMP_UGE: lambda: z3.UGE(a, b)}[op]()
        return z3.If(condition, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))
    a, b = sized(left, width), sized(right, width)
    if op in (H.HLIL_ADC,H.HLIL_SBB):
        carry=value(node.carry,state)
        assert carry.size()==1
        carry=sized(carry,width)
        return a+b+carry if op==H.HLIL_ADC else a-b-carry
    if op == H.HLIL_ADD_OVERFLOW:
        wide = z3.SignExt(1, a) + z3.SignExt(1, b)
        overflow = wide != z3.SignExt(1, a + b)
        return z3.If(overflow, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))
    return {H.HLIL_ADD: lambda: a + b, H.HLIL_SUB: lambda: a - b, H.HLIL_MUL: lambda: a * b,
            H.HLIL_AND: lambda: a & b, H.HLIL_OR: lambda: a | b, H.HLIL_XOR: lambda: a ^ b,
            H.HLIL_ROR: lambda: z3.RotateRight(a, b), H.HLIL_ROL: lambda: z3.RotateLeft(a, b),
            H.HLIL_LSL: lambda: a << b, H.HLIL_LSR: lambda: z3.LShR(a, b), H.HLIL_ASR: lambda: a >> b}[op]()


def execute(func,loop_limit=0):
    il = func.hlil
    assert il is not None, (func.name, func.analysis_skipped, func.analysis_skip_reason, func.analysis_performance_info)
    parameters = {v.identifier: z3.BitVec(n, 32) for v, n in zip(func.parameter_vars, ("x", "y", "z"))}
    returns = []

    def run(node, rows):
        op = node.operation
        if op == H.HLIL_BLOCK:
            for child in node.body:
                rows = run(child, rows)
            return rows
        if op in (H.HLIL_VAR_DECLARE, H.HLIL_NOP):
            return rows
        if op in (H.HLIL_WHILE,H.HLIL_DO_WHILE,H.HLIL_FOR):
            assert loop_limit>0,'loop evaluation requires an explicit test bound'
            active=run(node.init,rows) if op==H.HLIL_FOR else rows
            count=0
            if op==H.HLIL_DO_WHILE:
                active=run(node.body,active);count=1
            exits=[]
            while active:
                body=[]
                for path,state,writes in active:
                    guard=value(node.condition,state)!=0
                    yes,no=z3.simplify(z3.And(path,guard)),z3.simplify(z3.And(path,z3.Not(guard)))
                    if not z3.is_false(no):exits.append((no,dict(state),list(writes)))
                    if not z3.is_false(yes):body.append((yes,dict(state),list(writes)))
                if not body:break
                assert count<loop_limit,'loop did not terminate within the independent bound'
                active=run(node.body,body);count+=1
                if op==H.HLIL_FOR:active=run(node.update,active)
            return exits
        output = []
        for path, state, writes in rows:
            if op == H.HLIL_IF:
                guard = value(node.condition, state) != 0
                output += run(node.true, [(z3.And(path, guard), dict(state), list(writes))])
                output += run(node.false, [(z3.And(path, z3.Not(guard)), dict(state), list(writes))])
            elif op == H.HLIL_RET:
                returns.append((path, value(node.src[0], state), writes))
            elif op == H.HLIL_VAR_INIT:
                state[node.dest.identifier] = value(node.src, state)
                output.append((path, state, writes))
            elif op == H.HLIL_ADDRESS_OF:
                value(node, state)  # pure address expressions may survive as unused statements
                output.append((path, state, writes))
            elif op == H.HLIL_ASSIGN:
                rhs = value(node.src, state)
                if node.dest.operation == H.HLIL_VAR:
                    state[node.dest.var.identifier] = rhs
                elif node.dest.operation == H.HLIL_SPLIT:
                    high,low=node.dest.high,node.dest.low
                    assert high.operation==low.operation==H.HLIL_VAR
                    assert rhs.size()==(high.size+low.size)*8
                    state[low.var.identifier]=z3.Extract(low.size*8-1,0,rhs)
                    state[high.var.identifier]=z3.Extract(rhs.size()-1,low.size*8,rhs)
                elif node.dest.operation == H.HLIL_STRUCT_FIELD:
                    field = node.dest
                    assert field.src.operation == H.HLIL_VAR
                    old = value(field.src, state)
                    bits, shift = field.size * 8, field.offset * 8
                    mask = ((1 << bits) - 1) << shift
                    state[field.src.var.identifier] = (old & ~mask) | (sized(rhs, old.size()) << shift)
                elif node.dest.operation == H.HLIL_DEREF:
                    assert node.dest.size > 0
                    writes.append((value(node.dest.src, state), sized(rhs,node.dest.size*8)))
                else:
                    raise AssertionError(str(node))
                output.append((path, state, writes))
            else:
                raise AssertionError(str(node))
        return output

    leftover = run(il.root, [(z3.BoolVal(True), parameters, [])])
    assert not leftover
    assert returns
    return returns


def expected(name):
    x, y, z = (z3.BitVec(n, 32) for n in ("x", "y", "z"))
    if name == "flag_fixed_chain":
        return z3.And(x <= y, x >= 17)
    if name == "flag_signed_chain":
        return z3.And(x <= y, x >= z)
    if name in ("flag_unsigned_chain", "flag_nested_chain"):
        condition = z3.Or(z3.ULT(x, y), z3.UGT(x, z))
        return z3.And(condition, z3.Or(y != z, x == 7)) if name == "flag_nested_chain" else condition
    return {"eq": x == y, "ne": x != y, "slt": x < y, "sle": x <= y, "sgt": x > y, "sge": x >= y,
            "ult": z3.ULT(x, y), "ule": z3.ULE(x, y), "ugt": z3.UGT(x, y), "uge": z3.UGE(x, y)}[name.removeprefix("flag_")]


def proves(func, name):
    returns = execute(func)
    truth = expected(name)
    solver = z3.Solver()
    solver.set(timeout=2000)
    solver.add(z3.Or([z3.And(path, result != z3.If(truth, z3.BitVecVal(1, result.size()),
                                                 z3.BitVecVal(0, result.size()))) for path, result, _ in returns]))
    return solver.check() == z3.unsat
