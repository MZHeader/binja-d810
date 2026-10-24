"""Compare straight-line scalar effects over shared, unconstrained live-in values."""
from dataclasses import dataclass
from ..ir.expr import Expr, Var, substitute, tree_size
from ..ir.normalize import normalize
from ..semantic.verify import Verdict, check


@dataclass(frozen=True)
class Assignment:
    target: Var
    value: Expr


def summarize(statements, *, max_nodes, tick):
    state = {}
    for statement in statements:
        tick()
        if statement.target.width != statement.value.width:
            raise ValueError("assignment width mismatch")
        value = substitute(statement.value, state)
        size = tree_size(value)
        if size > max_nodes:
            raise ValueError("branch expression limit")
        for _ in range(size):
            tick()
        state[statement.target.name] = normalize(value)
    return state


def equivalent(left, right, outputs, *, max_nodes, proof_ms, deadline, tick):
    """Every observable output must match for every input, without assuming the guard constant."""
    a = summarize(left, max_nodes=max_nodes, tick=tick)
    b = summarize(right, max_nodes=max_nodes, tick=tick)
    for output in outputs:
        tick()
        x, y = a.get(output.name, output), b.get(output.name, output)
        proof = check(x, y, z3_timeout_ms=proof_ms, deadline=deadline)
        if proof.verdict is not Verdict.PROVEN:
            return False, f"live-out {output.name} not equivalent: {proof.reason or proof.verdict.value}"
    return True, "all observable scalar outputs proved equal"
