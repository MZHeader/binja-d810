"""Complete bounded preimages for reversible scalar chains with small side-input domains.

Domains overapproximate inputs; the emitted conjunctions retain their actual expressions and
therefore all correlations. Both halves are enumerated completely or no result is returned.
"""
from dataclasses import dataclass
from ..ir.expr import BinOp,Const,UnOp,mask,tree_size
from ..ir.bits import possible_ones
from ..ir.known import known_bits
from ..ir.normalize import normalize
from .preimage import rotate


@dataclass(frozen=True)
class Limits:
    nodes:int=2000
    stages:int=64
    domain_bits:int=8
    states:int=65536
    solutions:int=16


@dataclass(frozen=True)
class Stage:
    operation:str
    side:object
    values:tuple
    width:int

    def function(self,inverse=False):
        """Prepare fixed widths and inverses once, outside the state enumeration."""
        w,m=self.width,mask(self.width)
        if self.operation=='add':return (lambda s,v:(s-v)&m) if inverse else (lambda s,v:(s+v)&m)
        if self.operation=='sub':return (lambda s,v:(s+v)&m) if inverse else (lambda s,v:(s-v)&m)
        if self.operation=='subr':return lambda s,v:(v-s)&m
        if self.operation=='xor':return lambda s,v:s^v
        if self.operation=='mul':
            if len(self.values)!=1:raise ValueError('multiplier must be constant')
            factor=pow(self.values[0],-1,1<<w) if inverse else self.values[0]
            return lambda s,v:s*factor&m
        if self.operation=='not':return lambda s,v:~s&m
        if self.operation=='neg':return lambda s,v:-s&m
        if self.operation in ('rol','ror'):
            left=(self.operation=='rol') != inverse
            return lambda s,v:rotate(s,v,w,left)
        raise ValueError('unsupported reversible stage')


@dataclass(frozen=True)
class Result:
    expression:object
    states:int
    solutions:int
    stages:int


def domain(expression,limit,tick=lambda:None,memo=None):
    if isinstance(expression,Const):return (expression.value,)
    value,known=known_bits(expression,tick=tick,memo=memo)
    bits=(mask(expression.width)^known)&possible_ones(expression)
    if bits.bit_count()>limit:return None
    positions=[1<<i for i in range(expression.width) if bits & (1<<i)]
    values=[value]
    for bit in positions:values += [v|bit for v in values]
    return tuple(values)


def decompose(expression,limits,tick):
    stages=[];current=expression;facts={}
    bounded=lambda e,limit=limits.domain_bits:domain(e,limit,tick,facts)
    for _ in range(limits.stages):
        tick()
        fixed=bounded(current,0)
        if fixed is not None:
            current=Const(fixed[0],current.width);break
        if isinstance(current,UnOp) and current.op in ('neg','not'):
            stages.append(Stage(current.op,Const(0,current.width),(0,),current.width))
            current=current.operand;continue
        if not isinstance(current,BinOp):break
        op,a,b=current.op,current.left,current.right
        if op=='mul':
            if isinstance(a,Const):a,b=b,a
            if not isinstance(b,Const) or not b.value&1:break
            inverse=pow(b.value,-1,1<<current.width)
            if b.value*inverse & mask(current.width)!=1:raise ValueError('noninvertible multiplier')
            values=(b.value,)
        elif op in ('rol','ror'):
            values=bounded(b)
            if values is None:break
        elif op in ('add','sub','xor'):
            values=bounded(b)
            other=bounded(a)
            if other is not None and (values is None or (len(other),tree_size(a)) < (len(values),tree_size(b))):
                a,b,values=b,a,other
                if op=='sub':op='subr'
            if values is None:break
        else:break
        if len(values)==1:b=Const(values[0],b.width)
        stages.append(Stage(op,b,values,current.width));current=a
    values=bounded(current)
    if values is None:raise ValueError('preimage seed domain is unbounded')
    return current,values,list(reversed(stages))


def recover(expression,target,*,limits=Limits(),tick=lambda:None):
    if tree_size(expression)>limits.nodes:raise ValueError('finite preimage expression limit')
    seed,initial,stages=decompose(normalize(expression),limits,tick)
    if not stages:
        if isinstance(seed,Const):
            equal=int(seed.value==(target&mask(expression.width)))
            return Result(Const(equal,1),0,equal,0)
        return None
    if any(stage.width!=expression.width for stage in stages) or seed.width!=expression.width:
        raise ValueError('preimage state width changes')
    prefix=[len(initial)]
    for stage in stages:
        prefix.append(prefix[-1]*len(stage.values))
        if prefix[-1]>limits.states*limits.states:raise ValueError('finite preimage state limit')
    total=prefix[-1]
    split=min(range(len(prefix)),key=lambda n:max(prefix[n],total//prefix[n]))
    if max(prefix[split],total//prefix[split])>limits.states:raise ValueError('finite preimage state limit')
    visits=0
    def expand(rows,sequence,inverse):
        nonlocal visits
        for stage in sequence:
            next_rows=[];apply=stage.function(inverse)
            for state in rows:
                for value in stage.values:
                    tick();visits+=1
                    next_rows.append(apply(state,value))
            if len(next_rows)>limits.states:raise ValueError('finite preimage state limit')
            rows=next_rows
        return rows
    def bindings(index,sequence,with_seed):
        # Enumeration order is a mixed-radix index into the original domains. Keep only
        # integer states during the search; reconstruct expressions only for matched rows.
        result=[]
        for stage in reversed(sequence):
            index,choice=divmod(index,len(stage.values))
            if not isinstance(stage.side,Const):result.append((stage.side,stage.values[choice]))
        if with_seed and not isinstance(seed,Const):result.append((seed,initial[index]))
        return result
    forward_steps=stages[:split];backward_steps=list(reversed(stages[split:]))
    forward=expand(list(initial),forward_steps,False)
    table={}
    for index,value in enumerate(forward):
        tick()
        if value not in table:table[value]=index
        else:
            previous=table[value]
            if isinstance(previous,int):table[value]=[previous,index]
            else:previous.append(index)
    backward=expand([target&mask(expression.width)],backward_steps,True)
    solutions=[]
    for index,value in enumerate(backward):
        tick()
        matches=table.get(value,())
        if isinstance(matches,int):matches=(matches,)
        right=bindings(index,backward_steps,False) if matches else ()
        for matched in matches:
            tick()
            # Contradictory occurrences of the same side expression cannot describe an input.
            joined={}
            for e,v in bindings(matched,forward_steps,True)+list(right):
                if e in joined and joined[e]!=v:break
                joined[e]=v
            else:
                if joined not in solutions:solutions.append(joined)
            if len(solutions)>limits.solutions:raise ValueError('finite preimage solution limit')
    result=Const(0,1)
    for solution in solutions:
        term=Const(1,1)
        for e,value in solution.items():
            term=BinOp('and',term,BinOp('eq',e,Const(value,e.width),1),1)
        result=BinOp('or',result,term,1)
    result=normalize(result)
    if tree_size(result)>limits.nodes:raise ValueError('finite preimage result limit')
    return Result(result,visits,len(solutions),len(stages))
