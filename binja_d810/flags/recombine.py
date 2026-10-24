"""Propose comparisons from the operands present in a flag expression; prove every result."""

from dataclasses import dataclass, field

from ..deadline import Deadline
from ..ir.expr import BinOp, CMP_OPS, Const, UnOp, children, distinct_nodes, tree_size
from ..ir.normalize import normalize
from ..semantic.verify import Verdict, check
from .semantics import choose


@dataclass(frozen=True)
class Limits:
    instructions: int = 20000
    nodes: int = 2000
    work: int = 80000
    candidates: int = 128
    conditions: int = 128
    merges: int = 8
    proof_ms: int = 200
    rounds: int = 3
    boolean: bool = True
    boolean_atoms: int = 4
    phi_uses: bool = True


@dataclass
class Result:
    expression: object
    changes: int = 0
    attempts: int = 0
    notes: list[str] = field(default_factory=list)


def recombine(expression, *, guards=(), limits=Limits(), deadline=Deadline.never(), tick=lambda: None):
    result = Result(expression)
    left = limits.work
    def circuit_tick():
        nonlocal left
        tick(); left -= 1
        if left < 0 or deadline.expired(): raise ValueError("flag reconstruction budget exhausted")
    if deadline.expired() or tree_size(expression) > limits.nodes:
        result.notes.append("flag expression budget exhausted")
        return result
    if limits.boolean:
        from .boolean import simplify
        try:
            circuit = simplify(expression, max_atoms=limits.boolean_atoms, max_nodes=limits.nodes,
                               proof_ms=limits.proof_ms, deadline=deadline, tick=circuit_tick)
            if circuit is not None:
                expression = circuit
                result.expression = circuit  # already proved by universal predicate substitution
                result.changes += 1
                result.notes.append("Boolean circuit simplified with an exact-width proof")
        except (ValueError, ArithmeticError, RecursionError) as exc:
            result.notes.append(str(exc) or "Boolean circuit is unsupported")
    # Operands are discovered semantically from subtraction and comparison expressions.
    # Their order and widths come from the IL; there is no catalogue of target constants.
    pairs, differences = [], []
    pending, seen = [expression], set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(children(node))
        if isinstance(node, BinOp) and (node.op == "sub" or node.op in CMP_OPS) \
                and node.left.width > 1 and node.left.width == node.right.width:
            pair = node.left, node.right
            if pair not in pairs:
                pairs.append(pair)
            if node.op == "sub" and pair not in differences:
                differences.append(pair)
        if len(pairs) * len(CMP_OPS) >= limits.candidates:
            break
    pairs = differences + [pair for pair in pairs if pair not in differences]
    candidates = [BinOp(op, a, b, 1) for a, b in pairs for op in sorted(CMP_OPS)]
    memo = {}

    def visit(node):
        nonlocal left
        tick()
        left -= 1
        if left < 0 or deadline.expired():
            raise ValueError("flag reconstruction budget exhausted")
        if node in memo:
            return memo[node]
        original = node
        if node.width == 1 and not isinstance(node, Const):
            old_cost = distinct_nodes(node)
            for candidate in candidates:
                tick()
                if distinct_nodes(candidate) >= old_cost:
                    continue
                if result.attempts >= limits.candidates or deadline.expired():
                    break
                result.attempts += 1
                proof = check(node, candidate, z3_timeout_ms=limits.proof_ms, deadline=deadline)
                if proof.verdict is Verdict.PROVEN:
                    node = candidate
                    result.changes += 1
                    break
        if node == original:
            if isinstance(node, BinOp):
                node = normalize(BinOp(node.op, visit(node.left), visit(node.right), node.width))
            elif isinstance(node, UnOp):
                node = normalize(UnOp(node.op, visit(node.operand), node.width))
        memo[original] = node
        return node

    try:
        # Split only on selectors proved by the adapter's CFG analysis. The final proof
        # checks the recombined expression too, so a selector is never an assumption.
        def selected(node, depth):
            nonlocal left
            tick()
            left -= 1
            if left < 0 or deadline.expired():
                raise ValueError("flag selector budget exhausted")
            if depth >= len(guards) or depth >= limits.merges:
                return visit(node)
            guard = guards[depth]
            from ..semantic.selects import replace
            yes = normalize(replace(node, guard, Const(1, 1)))
            no = normalize(replace(node, guard, Const(0, 1)))
            if yes == node and no == node:
                return selected(node, depth + 1)
            return choose(guard, selected(yes, depth + 1), selected(no, depth + 1))
        proposed = selected(expression, 0) if expression.width == 1 else visit(expression)
        proposed = normalize(proposed)
        if tree_size(proposed) > limits.nodes:
            raise ValueError("flag result expression limit")
        if proposed != expression and (result.changes or guards) and not deadline.expired():
            proof = check(expression, proposed, z3_timeout_ms=limits.proof_ms, deadline=deadline)
            if proof.verdict is Verdict.PROVEN:
                result.expression = proposed
            else:
                result.notes.append("flag recombination was not proven: " + (proof.reason or proof.verdict.value))
        if result.attempts >= limits.candidates:
            result.notes.append("flag candidate limit")
    except (ValueError, RecursionError) as exc:
        result.notes.append(str(exc) or "flag expression depth is unsupported")
    return result
