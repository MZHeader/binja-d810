"""Bounded scalar loop summaries; control must be known, data may remain symbolic."""
from dataclasses import dataclass
from ..ir.expr import Const,substitute,tree_size
from ..ir.normalize import normalize
from .equivalence import Assignment
from .finite import Break,Continue,Decision,Havoc


@dataclass(frozen=True)
class Summary:
    values:dict
    written:frozenset
    iterations:int


def summarize(loop,initial,*,iterations,max_nodes,tick):
    state=dict(initial);written=set()
    def value(expression):
        tick()
        found=substitute(expression,state)
        size=tree_size(found)
        if size>max_nodes:raise ValueError('symbolic loop expression limit')
        for _ in range(size):tick()
        return normalize(found)
    def guard(expression):
        found=value(expression)
        if not isinstance(found,Const):raise ValueError('symbolic loop control is unknown')
        return found.value!=0
    def run(statements):
        for statement in statements:
            tick()
            if isinstance(statement,Assignment):
                if statement.target.width!=statement.value.width:raise ValueError('symbolic assignment width mismatch')
                state[statement.target.name]=value(statement.value)
                written.add(statement.target.name)
            elif isinstance(statement,Havoc):
                # A declaration with no value cannot establish a fresh symbolic input: retaining
                # an old value under the same name could conflate distinct unknown definitions.
                raise ValueError('uninitialized scalar in symbolic loop')
            elif isinstance(statement,Decision):
                signal=run(statement.yes if guard(statement.condition) else statement.no)
                if signal is not None:return signal
            elif isinstance(statement,(Break,Continue)):return statement
            else:raise ValueError('unsupported symbolic loop statement')
        return None
    if run(loop.init) is not None:raise ValueError('control transfer in loop initializer')
    count=0
    while True:
        tick()
        if not loop.post_test and not guard(loop.condition):break
        if count>=iterations:raise ValueError('symbolic loop iteration limit')
        signal=run(loop.body);count+=1
        if isinstance(signal,Break):break
        if run(loop.update) is not None:raise ValueError('control transfer in loop update')
        if loop.post_test and not guard(loop.condition):break
    return Summary({name:state[name] for name in written},frozenset(written),count)
