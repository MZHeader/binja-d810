"""Identity wrappers (v4.0, spec 3.5): a short function that returns its argument unchanged, called with a code
pointer, is inlined into its callers (auto, recorded), so the call through its result resolves there."""

from __future__ import annotations

from binaryninja import MediumLevelILOperation as Op

from ...flow.record import Change

_ORIGIN_DEPTH = 16


def _origin(ssa, var, depth: int = 0):
    """The version-0 variable an SSA variable copies, through copies and phis; None if it computes anything."""
    if depth > _ORIGIN_DEPTH:
        return None
    definition = ssa.get_ssa_var_definition(var)
    if definition is None:
        return var if var.version == 0 else None
    if definition.operation == Op.MLIL_SET_VAR_SSA and definition.src.operation == Op.MLIL_VAR_SSA:
        return _origin(ssa, definition.src.src, depth + 1)
    if definition.operation == Op.MLIL_VAR_PHI:
        found = {_origin(ssa, source, depth + 1) for source in definition.src}
        return found.pop() if len(found) == 1 else None
    return None


def is_identity(func) -> bool:
    """Only copies of one value, and a return of a parameter's incoming value."""
    try:
        ssa = func.mlil.ssa_form
    except Exception:  # noqa: BLE001 - no MLIL
        return False
    params = set(func.parameter_vars)
    returned = []
    for instr in ssa.instructions:
        op = instr.operation
        if op in (Op.MLIL_NOP, Op.MLIL_VAR_PHI):
            continue
        if op == Op.MLIL_SET_VAR_SSA and instr.src.operation == Op.MLIL_VAR_SSA and instr.src.size == instr.size:
            continue
        if op == Op.MLIL_RET and len(instr.src) == 1 and instr.src[0].operation == Op.MLIL_VAR_SSA:
            returned.append(instr.src[0].src)
            continue
        return False
    origins = {_origin(ssa, var) for var in returned}
    if len(origins) != 1:
        return False
    origin = origins.pop()
    return origin is not None and origin.var in params


def set_inline_if_default(func) -> bool:
    """Set only an unclaimed flag; user and other analysis decisions win."""
    flag = func.inline_during_analysis
    if (int(flag.value), flag.confidence) != (0, 0):
        return False
    func.set_auto_inline_during_analysis(True)
    return True


def decide(work, site) -> None:
    """Inline an identity wrapper whose result a site's call uses as a code pointer, unless its flag is set."""
    view = work.view
    callee = view.get_function_at(site.callee)
    if callee is None:
        return
    if work.record.get("inline", callee.start) is not None:
        work.add(Change("inline", callee.start, claims={site.function}))
        return
    if not is_identity(callee):
        work.note(site.function, f"{site.address:#x}: {callee.name} is not an identity wrapper")
        return
    callers = sorted({caller.start for caller in callee.callers})
    if not set_inline_if_default(callee):
        work.note(site.function, f"{site.address:#x}: {callee.name}'s inline flag is set by someone else")
        return
    work.add(Change("inline", callee.start, {"callers": callers}, [], {site.function}))
    work.touched |= {callee.start, site.function, *callers}
    work.note(site.function, f"inlined {callee.name}, an identity wrapper")
