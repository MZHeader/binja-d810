"""v3 spike (docs/design/spikes/2026-10-17-v3-predicates.md): a throwaway activity after
core.function.generateMediumLevelIL that replaces a named function's first MLIL_IF condition with a
constant built in the selected way. Not product code."""

from __future__ import annotations

import json
import threading

from binaryninja import Activity, MediumLevelILOperation as Op, Workflow

NAME = "extension.binja_d810.spike_v3"
MODES: dict[str, str] = {}  # function name -> mode
LOG: dict[int, list] = {}
_LOCK = threading.Lock()
_REGISTERED: list = []


def _build(mlil, mode: str, loc):
    if mode == "const_size0_true":
        return mlil.const(0, 1, loc)
    if mode == "const_size1_true":
        return mlil.const(1, 1, loc)
    if mode == "const_size4_true":
        return mlil.const(4, 1, loc)
    if mode == "const_size1_false":
        return mlil.const(1, 0, loc)
    if mode == "cmp_true":
        return mlil.compare_equal(4, mlil.const(4, 0, loc), mlil.const(4, 0, loc), loc)
    if mode == "cmp_false":
        return mlil.compare_not_equal(4, mlil.const(4, 0, loc), mlil.const(4, 0, loc), loc)
    raise ValueError(mode)


def _action(ctx) -> None:
    func = ctx.function
    if func is None:
        return
    mode = MODES.get(func.name.lstrip("_"))
    if mode is None:
        return
    from binaryninja import ILSourceLocation

    mlil = ctx.mlil
    for instr in mlil.instructions:
        if instr.operation == Op.MLIL_IF:
            cond = instr.condition
            before = str(instr)
            mlil.replace_expr(cond.expr_index, _build(mlil, mode, ILSourceLocation.from_instruction(cond)))
            mlil.finalize()
            mlil.generate_ssa_form()
            ctx.mlil = mlil
            with _LOCK:
                LOG.setdefault(func.start, []).append({"mode": mode, "before": before,
                                                       "after": str(mlil[instr.instr_index])})
            return


def register() -> None:
    if _REGISTERED:
        return
    config = json.dumps({"name": NAME, "title": "D-810 v3 spike", "description": "spike"})
    activity = Activity(configuration=config, action=_action)
    _REGISTERED.append(activity)
    wf = Workflow("core.function.metaAnalysis").clone("core.function.metaAnalysis")
    wf.register_activity(activity)
    wf.insert_after("core.function.generateMediumLevelIL", [NAME])
    wf.register()
