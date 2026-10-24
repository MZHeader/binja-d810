"""Compact a complete preimage predicate when it describes one exact input value."""
from ..ir.expr import BinOp,Const,UnOp,Var,CMP_OPS,mask,variables,tree_size
from ..ir.evaluate import shift_count
from .verify import Verdict,check,to_z3,_z3_context


def project(predicate,*,proof_ms,deadline):
    names=variables(predicate)
    if len(names)!=1 or deadline.expired():return predicate
    try:import z3
    except ImportError:return predicate
    source=names[0];ctx=_z3_context()
    value=z3.BitVec(source.name,source.width,ctx=ctx)
    solver=z3.Solver(ctx=ctx)
    remaining=proof_ms if deadline.at==float('inf') else min(proof_ms,int(deadline.remaining()*1000))
    if remaining<1:return predicate
    solver.set(timeout=remaining)
    solver.add(to_z3(predicate,{source.name:value},ctx)==1)
    result=solver.check()
    if result==z3.unsat:return Const(0,1)
    if result!=z3.sat:return predicate
    relevant=input_bits(predicate,source.name)
    selected=source if relevant==mask(source.width) else BinOp('and',source,Const(relevant,source.width),source.width)
    candidate=BinOp('eq',selected,Const(solver.model().eval(value,model_completion=True).as_long()&relevant,source.width),1)
    if tree_size(candidate)>=tree_size(predicate):return predicate
    proof=check(predicate,candidate,z3_timeout_ms=proof_ms,deadline=deadline)
    return candidate if proof.verdict is Verdict.PROVEN else predicate


def input_bits(expression,name):
    """Conservative bit dependencies, used only to propose a mask for the final exact proof."""
    found=0
    def visit(node,needed):
        nonlocal found
        needed&=mask(node.width)
        if not needed or isinstance(node,Const):return
        if isinstance(node,Var):
            if node.name==name:found|=needed
            return
        if isinstance(node,UnOp):
            if node.op in ('zx','trunc','not'):visit(node.operand,needed)
            elif node.op=='sx':
                sign=1<<(node.operand.width-1)
                visit(node.operand,(needed&mask(node.operand.width))|(sign if needed>>node.operand.width else 0))
            else:visit(node.operand,mask(needed.bit_length()))
            return
        a,b=node.left,node.right
        if node.op in CMP_OPS:
            visit(a,mask(a.width));visit(b,mask(b.width));return
        if node.op in ('and','or') and isinstance(b,Const):
            visit(a,needed&(b.value if node.op=='and' else ~b.value));return
        if node.op in ('and','or') and isinstance(a,Const):
            visit(b,needed&(a.value if node.op=='and' else ~a.value));return
        if node.op in ('and','or','xor'):
            visit(a,needed);visit(b,needed);return
        if node.op in ('shl','lshr','ashr') and isinstance(b,Const):
            count=shift_count(b.value,node.width)
            if node.op=='shl':visit(a,needed>>count)
            else:
                source=needed<<count
                if node.op=='ashr' and needed>>(max(0,node.width-count)):source|=1<<(node.width-1)
                visit(a,source)
            return
        if node.op=='mul':
            if isinstance(a,Const):a,b=b,a
            if isinstance(b,Const) and b.value and b.value&(b.value-1)==0:
                visit(a,needed>>(b.value.bit_length()-1));return
        if node.op in ('add','sub','mul'):
            visit(a,mask(needed.bit_length()));visit(b,mask(needed.bit_length()));return
        visit(a,mask(a.width));visit(b,mask(b.width))
    visit(expression,mask(expression.width))
    return found
