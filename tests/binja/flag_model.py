"""Independent symbolic execution of the small C flag fixtures' final structured HLIL.

This test oracle does not use D810's lifters, flag models or comparison reconstruction.
Branches are explored rather than treated as unrelated phi inputs.
"""

import z3
from binaryninja import HighLevelILOperation as H, TypeClass,VariableSourceType,BuiltinType


def sized(value, width):
    return z3.Extract(width - 1, 0, value) if value.size() > width else \
        z3.ZeroExt(width - value.size(), value) if value.size() < width else value


def stack_read(state,offset,bits):
    count=(bits+7)//8
    values=[state.setdefault(('stack',offset+i),z3.BitVec(f'stack_uninitialized_{offset+i}',8)) for i in range(count)]
    return sized(z3.Concat(*(values if state.get('_big_endian') else list(reversed(values)))) if count>1 else values[0],bits)


def stack_write(state,offset,bits,datum):
    count=(bits+7)//8;datum=sized(datum,count*8)
    for i in range(count):
        shift=(count-1-i if state.get('_big_endian') else i)*8
        state[('stack',offset+i)]=z3.Extract(shift+7,shift,datum)


def local_address(node,state):
    def pointer(item):
        if item.operation==H.HLIL_ADDRESS_OF:return address(item.src)
        if item.operation==H.HLIL_VAR and item.expr_type is not None and item.expr_type.type_class==TypeClass.ArrayTypeClass:return address(item)
        if item.operation in (H.HLIL_ADD,H.HLIL_SUB):
            base=pointer(item.left);offset=z3.simplify(value(item.right,state))
            if base is not None and z3.is_bv_value(offset):return base+(offset.as_long() if item.operation==H.HLIL_ADD else -offset.as_long())
        return None
    def address(item):
        if item.operation==H.HLIL_VAR and item.var.source_type==VariableSourceType.StackVariableSourceType:return item.var.storage
        if item.operation==H.HLIL_STRUCT_FIELD:
            base=address(item.src)
            return None if base is None else base+item.offset
        if item.operation==H.HLIL_DEREF:return pointer(item.src)
        if item.operation==H.HLIL_ARRAY_INDEX:
            base=pointer(item.src);index=z3.simplify(value(item.index,state))
            if base is not None and z3.is_bv_value(index):return base+index.as_long()*item.size
        return None
    return address(node)


def store_variable(var,bits,datum,state):
    state[var.identifier]=sized(datum,bits)
    if state.get('_local_memory') and var.source_type==VariableSourceType.StackVariableSourceType:
        stack_write(state,var.storage,bits,datum)


def value(node, state):
    op, width = node.operation, node.size * 8 or 1
    if state.get('_local_memory') and op in (H.HLIL_VAR,H.HLIL_STRUCT_FIELD,H.HLIL_DEREF,H.HLIL_ARRAY_INDEX):
        at=local_address(node,state)
        if at is not None:return stack_read(state,at,width)
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
    if op == H.HLIL_ASSIGN and node.dest.operation == H.HLIL_VAR:
        assigned = sized(value(node.src,state),node.dest.size*8)
        assert node.dest.var.type is None or node.dest.var.type.width*8 == assigned.size()
        state[node.dest.var.identifier] = assigned
        return sized(assigned,width)
    if op == H.HLIL_VAR:
        return sized(state.setdefault(node.var.identifier, z3.BitVec("uninitialized_" + str(node.var.identifier),
                                                                   max(width, 8 * node.var.type.width if node.var.type else width))), width)
    if op == H.HLIL_STRUCT_FIELD:
        backing,offset=node,0
        while backing.operation==H.HLIL_STRUCT_FIELD and backing.member_index is None:
            offset+=backing.offset;backing=backing.src
        if backing.operation==H.HLIL_VAR:
            whole=value(backing,state)
            assert 8*offset+width<=whole.size(),'field outside backing variable'
            return sized(z3.LShR(whole,offset*8),width)
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


def execute(func,loop_limit=0,local_memory=False):
    il = func.hlil
    assert il is not None, (func.name, func.analysis_skipped, func.analysis_skip_reason, func.analysis_performance_info)
    parameters = {v.identifier: z3.BitVec(n, 32) for v, n in zip(func.parameter_vars, ("x", "y", "z"))}
    if local_memory:
        parameters.update(_local_memory=True,_big_endian=func.arch.endianness.name=='BigEndian')
        for var in func.parameter_vars:
            if var.source_type==VariableSourceType.StackVariableSourceType:
                stack_write(parameters,var.storage,32,parameters[var.identifier])
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
                store_variable(node.dest,node.size*8 or 1,value(node.src,state),state)
                output.append((path, state, writes))
            elif op == H.HLIL_ADDRESS_OF:
                value(node, state)  # pure address expressions may survive as unused statements
                output.append((path, state, writes))
            elif op == H.HLIL_CALL and state.get('_local_memory'):
                # Native constant-data builtins represent compiler stores, not target calls.
                assert len(node.params)==3 and node.params[1].operation==H.HLIL_CONST_DATA
                raw,kind=node.params[1].constant_data.data_and_builtin
                assert kind in (BuiltinType.BuiltinMemcpy,BuiltinType.BuiltinStrncpy)
                assert node.params[0].operation==H.HLIL_ADDRESS_OF
                at=local_address(node.params[0].src,state)
                count=z3.simplify(value(node.params[2],state))
                assert at is not None and z3.is_bv_value(count)
                raw=bytes(raw);count=count.as_long();assert 0<count<=len(raw)
                for i,byte in enumerate(raw[:count]):stack_write(state,at+i,8,z3.BitVecVal(byte,8))
                output.append((path,state,writes))
            elif op == H.HLIL_ASSIGN:
                rhs = value(node.src, state)
                if node.dest.operation == H.HLIL_VAR:
                    store_variable(node.dest.var,node.dest.size*8 or 1,rhs,state)
                elif node.dest.operation == H.HLIL_SPLIT:
                    high,low=node.dest.high,node.dest.low
                    assert high.operation==low.operation==H.HLIL_VAR
                    assert rhs.size()==(high.size+low.size)*8
                    store_variable(low.var,low.size*8,z3.Extract(low.size*8-1,0,rhs),state)
                    store_variable(high.var,high.size*8,z3.Extract(rhs.size()-1,low.size*8,rhs),state)
                elif state.get('_local_memory') and local_address(node.dest,state) is not None:
                    stack_write(state,local_address(node.dest,state),node.dest.size*8,rhs)
                elif node.dest.operation == H.HLIL_STRUCT_FIELD:
                    field = node.dest
                    assert field.src.operation == H.HLIL_VAR
                    old = value(field.src, state)
                    bits, shift = field.size * 8, field.offset * 8
                    mask = ((1 << bits) - 1) << shift
                    store_variable(field.src.var,old.size(),(old & ~mask) | (sized(rhs, old.size()) << shift),state)
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
