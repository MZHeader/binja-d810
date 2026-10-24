"""Sound known-zero/known-one bits, including carries and constant shifts."""
from .expr import BinOp,Const,UnOp,Var,mask
from .evaluate import _binary,_unary,_rotate,shift_count


def known_bits(expression,*,tick=lambda:None,memo=None):
    """(known-one value, known mask). Unknown bits are never assumed zero."""
    if memo is None:memo={}
    def visit(e):
        tick()
        if id(e) in memo:return memo[id(e)]
        w,m=e.width,mask(e.width)
        value,known=0,0
        if isinstance(e,Const):value,known=e.value,m
        elif isinstance(e,Var):pass
        elif isinstance(e,UnOp):
            a,k=visit(e.operand)
            if k==mask(e.operand.width):value,known=_unary(e,a),m
            elif e.op=='not':value,known=(~a)&k,k
            elif e.op in ('zx','trunc'):
                value=a&m;known=(k|(m^mask(e.operand.width)) if e.op=='zx' else k)&m
            elif e.op=='sx':
                extra=m^mask(e.operand.width);sign=1<<(e.operand.width-1)
                value=a|(extra if a&sign else 0)
                known=k|(extra if k&sign else 0)
            elif e.op=='neg':value,known=addition(0,m,(~a)&k,k,1,w,tick)
        else:
            a,ak=visit(e.left);b,bk=visit(e.right)
            if ak==mask(e.left.width) and bk==mask(e.right.width):value,known=_binary(e,a,b),m
            elif e.op=='and':
                value=a&b;known=value|((~a)&ak)|((~b)&bk)
            elif e.op=='or':
                value=a|b;known=value|(((~a)&ak)&((~b)&bk))
            elif e.op=='xor':known=ak&bk;value=(a^b)&known
            elif e.op in ('add','sub'):
                value,known=addition(a,ak,b if e.op=='add' else (~b)&bk,bk,int(e.op=='sub'),w,tick)
            elif e.op in ('shl','lshr','ashr','rol','ror') and bk==mask(e.right.width):
                if e.op in ('rol','ror'):
                    value,known=_rotate(e.op,a,b,w),_rotate(e.op,ak,b,w)
                else:value,known=shift(a,ak,shift_count(b,w),w,e.op)
            elif e.op=='mul':
                if isinstance(e.left,Const):a,ak,b=b,bk,e.left.value
                elif not isinstance(e.right,Const):b=None
                if b==0:value,known=0,m
                elif b is not None and b&(b-1)==0:
                    value,known=shift(a,ak,b.bit_length()-1,w,'shl')
        result=(value&m,known&m)
        memo[id(e)]=result
        return result
    return visit(expression)


def shift(value,known,count,width,operation):
    m=mask(width)
    if operation=='shl':
        return (0,m) if count>=width else ((value<<count)&m,((known<<count)|mask(count))&m)
    if operation=='lshr':
        return (0,m) if count>=width else (value>>count,(known>>count)|(m^mask(width-count)))
    count=min(count,width)
    extra=m^mask(width-count);sign=1<<(width-1)
    return ((value>>count)|(extra if value&sign else 0),
            (known>>count)|(extra if known&sign else 0))


def addition(a,ak,b,bk,carry,width,tick):
    value=known=0;carries={carry}
    for bit in range(width):
        tick();flag=1<<bit
        left=((a>>bit)&1,) if ak&flag else (0,1)
        right=((b>>bit)&1,) if bk&flag else (0,1)
        sums={x+y+c for x in left for y in right for c in carries}
        results={s&1 for s in sums}
        if len(results)==1:
            known|=flag
            if 1 in results:value|=flag
        carries={s>>1 for s in sums}
    return value,known
