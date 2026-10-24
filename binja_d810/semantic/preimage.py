"""Exact preimages of constant equalities under scalar bit-vector operations."""
from dataclasses import dataclass
from ..ir.expr import BinOp, Const, UnOp, mask, tree_size
from ..ir.normalize import normalize
from ..ir.evaluate import shift_count


@dataclass(frozen=True)
class Result:
    expression: object
    steps: int
    method: str = "exact bit-vector preimage"


def rotate(value, count, width, left):
    count %= width
    if not count:
        return value & mask(width)
    return (((value << count) | (value >> (width-count))) if left else
            ((value >> count) | (value << (width-count)))) & mask(width)


def comparison(expression, *, steps, max_nodes, tick):
    """Peel reversible encodings; retain exact masks for operations that discard bits.

    Equality and inequality only. Ordered comparisons do not survive modular multiplication,
    XOR or rotation in general and are deliberately excluded.
    """
    if not isinstance(expression, BinOp) or expression.op not in ("eq", "ne"):
        return None
    if tree_size(expression) > max_nodes:
        raise ValueError("preimage expression limit")
    source, target = expression.left, expression.right
    if isinstance(source, Const):
        source, target = target, source
    if not isinstance(target, Const):
        return None
    value, used = target.value, 0
    def impossible():
        return Result(Const(int(expression.op == 'ne'),1),used+1)
    for _ in range(steps):
        tick()
        width = source.width
        m = mask(width)
        if isinstance(source, UnOp):
            if source.op == 'not':value = ~value & m
            elif source.op == 'neg':value = -value & m
            elif source.op in ('zx','sx'):
                inner = source.operand.width
                low = value & mask(inner)
                extended = low if source.op == 'zx' or not low & (1 << (inner-1)) else low | (m ^ mask(inner))
                if extended != value:return impossible()
                value = low
            elif source.op == 'trunc':
                source = BinOp('and',source.operand,Const(m,source.operand.width),source.operand.width)
                used += 1
                break
            else:break
            source = source.operand
            used += 1
            continue
        if not isinstance(source, BinOp):break
        a,b,op = source.left,source.right,source.op
        if op in ('rol','ror','shl','lshr') and isinstance(b, Const):
            if op in ('rol','ror'):
                value = rotate(value,b.value,width,op == 'ror')
                source = a
            else:
                count = shift_count(b.value,width)
                if count >= width:
                    return Result(Const(int((value == 0) == (expression.op == 'eq')),1),used+1)
                if op == 'shl':
                    if value & mask(count):return impossible()
                    source = BinOp('and',a,Const(mask(width-count),width),width)
                    value >>= count
                else:
                    if value > mask(width-count):return impossible()
                    source = BinOp('and',a,Const(m ^ mask(count),width),width)
                    value <<= count
                used += 1
                break
            used += 1
            continue
        if isinstance(a,Const) and op in ('add','xor','mul'):
            a,b = b,a
        if op == 'sub' and isinstance(a,Const):
            value = (a.value-value) & m
            source = b
        elif isinstance(b,Const) and op in ('add','sub','xor','mul'):
            if op == 'add':value = (value-b.value)&m
            elif op == 'sub':value = (value+b.value)&m
            elif op == 'xor':value ^= b.value
            elif b.value == 0:
                return Result(Const(int((value == 0) == (expression.op == 'eq')),1),used+1)
            elif b.value & 1:
                inverse = pow(b.value,-1,1<<width)
                assert b.value*inverse & m == 1
                value = value*inverse & m
            else:
                zeros = (b.value & -b.value).bit_length()-1
                if value & mask(zeros):return impossible()
                reduced = width-zeros
                inverse = pow(b.value >> zeros,-1,1<<reduced)
                value = ((value >> zeros)*inverse)&mask(reduced)
                source = BinOp('and',a,Const(mask(reduced),width),width)
                used += 1
                break
            source = a
        else:break
        used += 1
    if not used:return None
    result = normalize(BinOp(expression.op,source,Const(value,source.width),1))
    return Result(result,used) if tree_size(result) < tree_size(expression) else None
