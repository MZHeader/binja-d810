"""Recover Boolean circuits hidden behind integer casts, masks and arithmetic.

Comparisons are independent Boolean inputs, so no relation between their operands is assumed.
The bounded truth table proposes a circuit; exact bit-vector verification checks the original.
"""
from ..ir.expr import BinOp, CMP_OPS, Const, UnOp, Var, children, substitute, tree_size
from ..ir.evaluate import evaluate
from ..ir.normalize import normalize
from ..semantic.linear_mba import _formula_for, _to_expr
from ..semantic.verify import Verdict, check


def simplify(expression, *, max_atoms, max_nodes, proof_ms, deadline, tick):
    if not 1 <= max_atoms <= 8: raise ValueError('Boolean circuit atom limit')
    if expression.width != 1 or tree_size(expression) > max_nodes:
        return None
    atoms, derived, shaped = {}, {}, {}

    def step():
        tick()
        if deadline.expired(): raise ValueError('Boolean circuit deadline exhausted')

    def boolean_derived(node):
        step()
        if node in derived: return derived[node]
        if isinstance(node, Const): found = True
        elif isinstance(node, Var): found = node.width == 1
        elif isinstance(node, BinOp) and node.op in CMP_OPS: found = True
        else: found = all(boolean_derived(child) for child in children(node))
        derived[node] = found
        return found

    def atom(node):
        if node not in atoms:
            if len(atoms) >= max_atoms: raise ValueError('Boolean circuit atom limit')
            atoms[node] = Var(f'boolean@{len(atoms)}',1)
        return atoms[node]

    def shape(node):
        step()
        if node in shaped: return shaped[node]
        if isinstance(node,Const): out = node
        elif isinstance(node,Var):
            if node.width != 1: raise ValueError('non-Boolean circuit input')
            out = atom(node)
        elif isinstance(node,BinOp):
            if node.op in CMP_OPS and not all(boolean_derived(c) for c in children(node)):
                out = atom(node)
            else: out = BinOp(node.op,shape(node.left),shape(node.right),node.width)
        else:
            assert isinstance(node,UnOp)
            out = UnOp(node.op,shape(node.operand),node.width)
        shaped[node] = out
        return out

    if not boolean_derived(expression): return None
    circuit = normalize(shape(expression))
    names = list(atoms.values())
    table = 0
    size = tree_size(circuit)
    for row in range(1 << len(names)):
        # Charge evaluation visits, including repeated occurrences in the expression tree.
        for _ in range(size): step()
        value = evaluate(circuit,{name.name:(row >> i)&1 for i,name in enumerate(names)})
        table |= (value & 1) << row
    full = (1 << (1 << len(names))) - 1
    if table in (0,full): abstract = Const(int(table == full),1)
    else: abstract = _to_expr(_formula_for(table,len(names)),names,1)
    step()
    proposed = normalize(substitute(abstract,{name.name:node for node,name in atoms.items()}))
    if tree_size(proposed) >= tree_size(expression) or tree_size(proposed) > max_nodes: return None
    # Prove universally over independent one-bit predicates, retaining every intermediate
    # integer width. Structural substitution then proves the original too, without repeatedly
    # evaluating or bit-blasting the predicates' possibly large arithmetic operands.
    proof = check(circuit,abstract,z3_timeout_ms=proof_ms,deadline=deadline)
    return proposed if proof.verdict is Verdict.PROVEN else None
