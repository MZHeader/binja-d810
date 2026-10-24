"""Exact bounded control evaluation for a loop whose scalar results are unobserved.

Unknown data values remain unknown. Every control decision must be determined by concrete
values at the original bit widths. The caller proves purity and deadness separately.
"""
from dataclasses import dataclass
from ..ir.evaluate import evaluate
from ..ir.expr import Const, Var, variables, tree_size
from .equivalence import Assignment


@dataclass(frozen=True)
class Decision:
    condition: object
    yes: tuple
    no: tuple = ()


@dataclass(frozen=True)
class Break:
    pass


@dataclass(frozen=True)
class Continue:
    pass


@dataclass(frozen=True)
class Havoc:
    target: Var


@dataclass(frozen=True)
class Loop:
    condition: object
    body: tuple
    init: tuple = ()
    update: tuple = ()
    post_test: bool = False


def abstract_value(expression, state, *, max_nodes, tick):
    size = tree_size(expression)
    if size > max_nodes:
        raise ValueError("loop expression limit")
    for _ in range(size):
        tick()
    names = [v.name for v in variables(expression)]
    if any(state.get(name) is None for name in names):
        return None
    return evaluate(expression, {name:state[name] for name in names})


def terminates(loop, initial, *, iterations, max_nodes, tick):
    state = dict(initial)
    def guard(expression):
        result = abstract_value(expression, state, max_nodes=max_nodes, tick=tick)
        if result is None:
            raise ValueError("loop control depends on an unknown value")
        return result != 0
    def run(statements):
        for statement in statements:
            tick()
            if isinstance(statement, Assignment):
                if statement.target.width != statement.value.width:
                    raise ValueError("loop assignment width mismatch")
                state[statement.target.name] = abstract_value(statement.value, state, max_nodes=max_nodes, tick=tick)
            elif isinstance(statement, Havoc):
                state[statement.target.name] = None
            elif isinstance(statement, Decision):
                signal = run(statement.yes if guard(statement.condition) else statement.no)
                if signal is not None:
                    return signal
            elif isinstance(statement, (Break, Continue)):
                return statement
            else:
                raise ValueError("unsupported loop statement")
        return None
    if run(loop.init) is not None:
        raise ValueError("control transfer in loop initializer")
    count = 0
    while True:
        tick()
        if not loop.post_test and not guard(loop.condition):
            return count
        if count >= iterations:
            raise ValueError("loop iteration limit")
        signal = run(loop.body)
        count += 1
        if isinstance(signal, Break):
            return count
        if run(loop.update) is not None:
            raise ValueError("control transfer in loop update")
        if loop.post_test and not guard(loop.condition):
            return count
