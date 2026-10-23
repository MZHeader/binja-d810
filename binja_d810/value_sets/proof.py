"""Exact fixed-width proofs under independent per-read value domains.

This is intentionally separate from the unconstrained proof cache: a proof under one domain
must never authorize a rewrite in another function or after the domain changes.
"""

from ..deadline import Deadline
from ..ir.expr import tree_size, variables
from ..semantic.verify import Check, Verdict, _z3_context, to_z3


def check(original, replacement, domains, *, timeout_ms=200, max_values=64, max_domains=16,
          max_nodes=2000, deadline=Deadline.never()):
    if original.width != replacement.width:
        return Check(Verdict.REFUTED, "width")
    if deadline.expired():
        return Check(Verdict.TESTED, "none", "value-set proof deadline exhausted")
    if len(domains) > max_domains or any(len(values) > max_values for values in domains.values()) \
            or max(tree_size(original), tree_size(replacement)) > max_nodes:
        return Check(Verdict.TESTED, "none", "value-set proof size limit")
    widths = {}
    for variable in variables(original) + variables(replacement):
        if variable.name in widths and widths[variable.name] != variable.width:
            return Check(Verdict.TESTED, "none", "ambiguous value-set variable width")
        widths[variable.name] = variable.width
    if any(name not in widths or not values or any(type(v) is not int or not 0 <= v < 1 << widths[name] for v in values)
           for name, values in domains.items()):
        return Check(Verdict.TESTED, "none", "invalid or empty value domain")
    try:
        import z3
    except ImportError:
        return Check(Verdict.TESTED, "none", "z3 unavailable")
    context = _z3_context()
    symbols = {name: z3.BitVec(name, width, context) for name, width in widths.items()}
    solver = z3.Solver(ctx=context)
    milliseconds = timeout_ms if deadline.at == float("inf") else min(timeout_ms, int(deadline.remaining() * 1000))
    if milliseconds < 1:
        return Check(Verdict.TESTED, "none", "value-set proof deadline exhausted")
    solver.set(timeout=milliseconds)
    for name, values in domains.items():
        if deadline.expired():
            return Check(Verdict.TESTED, "none", "value-set proof deadline exhausted")
        solver.add(z3.Or([symbols[name] == value for value in sorted(values)]))
    solver.add(to_z3(original, symbols, context) != to_z3(replacement, symbols, context))
    verdict = solver.check()
    if verdict == z3.unsat:
        return Check(Verdict.PROVEN, "z3 value sets")
    if verdict == z3.sat:
        return Check(Verdict.REFUTED, "z3 value sets")
    return Check(Verdict.TESTED, "z3 value sets", solver.reason_unknown())
