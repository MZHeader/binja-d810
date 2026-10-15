"""HLIL spike (spec section 5.5): a throwaway activity after core.function.generateHighLevelIL.

It lifts HLIL expressions to binja_d810 IR (following SSA definitions), simplifies them with the
v1 pipeline and replaces them with HLIL built from the result. Everything it does is recorded in
LOG so the gate tests can inspect it. Not product code.
"""

from __future__ import annotations

import ctypes
import json
import threading
import traceback
from typing import Optional

from binaryninja import (
    Activity, HighLevelILInstruction, HighLevelILOperation as H, ILSourceLocation, Workflow,
)

from binja_d810.ir.expr import BinOp, Const, Expr, UnOp, Var, is_mba, to_str
from binja_d810.rules.registry import load_rules
from binja_d810.semantic.backend import default_backends
from binja_d810.simplify import simplify_expression

NAME = "extension.binja_d810.spike_hlil"
LOG: dict[int, list] = {}
FAIL_AT: dict[str, Optional[str]] = {"stage": None}  # injected failure: replace/finalize/ssa/after
CHECK_VERSIONS = {"on": False}
_LOCK = threading.Lock()
_REGISTERED: list = []

_BINARY = {H.HLIL_ADD: "add", H.HLIL_SUB: "sub", H.HLIL_MUL: "mul", H.HLIL_AND: "and", H.HLIL_OR: "or",
           H.HLIL_XOR: "xor", H.HLIL_LSL: "shl", H.HLIL_LSR: "lshr", H.HLIL_ASR: "ashr"}
_UNARY = {H.HLIL_NEG: "neg", H.HLIL_NOT: "not", H.HLIL_ZX: "zx", H.HLIL_SX: "sx", H.HLIL_LOW_PART: "trunc"}


MLIL_TOGGLE = "extension.binja_d810.mba"
RECORDER = "extension.binja_d810.spike_mlil_recorder"
GENERATIONS: dict[int, list] = {}  # func start -> [("mlil"|"hlil", mlil handle, hlil handle)]
MODE = {"gate_on_toggle": True, "remove_dead": True, "enabled": True}


def _handle(il) -> Optional[int]:
    return None if il is None else ctypes.addressof(il.handle.contents)


def register() -> None:
    if _REGISTERED:
        return
    config = json.dumps({"name": NAME, "title": "D-810 HLIL spike", "description": "spike"})
    recorder = json.dumps({"name": RECORDER, "title": "D-810 spike recorder", "description": "spike"})
    activity = Activity(configuration=config, action=_action)
    mlil_recorder = Activity(configuration=recorder, action=_record_mlil)
    _REGISTERED.extend([activity, mlil_recorder])
    wf = Workflow("core.function.metaAnalysis").clone("core.function.metaAnalysis")
    wf.register_activity(activity)
    wf.register_activity(mlil_recorder)
    wf.insert_after("core.function.generateMediumLevelIL", [RECORDER])
    wf.insert_after("core.function.generateHighLevelIL", [NAME])
    wf.register()


def _record_mlil(ctx) -> None:
    func = ctx.function
    if func is not None:
        with _LOCK:
            GENERATIONS.setdefault(func.start, []).append(("mlil", _handle(ctx.mlil), None))


def _record(func, entry) -> None:
    with _LOCK:
        LOG.setdefault(func.start, []).append(entry)


class _Lift:
    def __init__(self, ssa, root_ssa, check: bool):
        self.ssa, self.root, self.check = ssa, root_ssa, check
        self.leaves: list[tuple] = []

    def expr(self, e) -> Optional[Expr]:
        op, width = e.operation, e.size * 8
        if width == 0:
            return None
        if op in (H.HLIL_CONST, H.HLIL_CONST_PTR):
            return Const(e.constant, width)
        if op == H.HLIL_VAR_SSA:
            return self.var(e.var, width)
        if op in _BINARY:
            a, b = self.expr(e.left), self.expr(e.right)
            if a is None or b is None:
                return None
            try:
                return BinOp(_BINARY[op], a, b, width)
            except ValueError:
                return None
        if op in _UNARY:
            a = self.expr(e.src)
            if a is None:
                return None
            try:
                return UnOp(_UNARY[op], a, width)
            except ValueError:
                return None
        return None

    def var(self, ssa_var, width) -> Optional[Expr]:
        definition = self.ssa.get_ssa_var_definition(ssa_var)
        if (definition is not None and definition.operation in (H.HLIL_VAR_INIT_SSA, H.HLIL_ASSIGN)
                and definition.src.size * 8 == width):
            inner = self.expr(definition.src)
            if inner is not None:
                return inner
        current = self.root.get_ssa_var_version(ssa_var.var)
        self.leaves.append((ssa_var.var.name, ssa_var.version, current, definition is None))
        if self.check and current != ssa_var.version:
            return None
        return Var(f"{ssa_var.var.name}#{ssa_var.version}@{ssa_var.var.identifier:x}", width, ref=ssa_var.var)


def _lower(hlil, e: Expr, loc) -> int:
    size = e.width // 8
    if isinstance(e, Const):
        return hlil.const(size, e.value, loc)
    if isinstance(e, Var):
        return hlil.var(size, e.ref, loc)
    if isinstance(e, UnOp):
        inner = _lower(hlil, e.operand, loc)
        return {"neg": hlil.neg_expr, "not": hlil.not_expr, "zx": hlil.zero_extend, "sx": hlil.sign_extend,
                "trunc": hlil.low_part}[e.op](size, inner, loc)
    a, b = _lower(hlil, e.left, loc), _lower(hlil, e.right, loc)
    return {"add": hlil.add, "sub": hlil.sub, "mul": hlil.mult, "and": hlil.and_expr, "or": hlil.or_expr,
            "xor": hlil.xor_expr, "shl": hlil.shift_left, "lshr": hlil.logical_shift_right,
            "ashr": hlil.arith_shift_right}[e.op](size, a, b, loc)


def shallow_copy(hlil, e) -> int:
    """A new expression with the same operation and operands (children are shared by index).

    HighLevelILFunction.copy_expr uses raw_operands, which in 6.0.10601 returns the enclosing
    statement's decoded operands, not this expression's raw ones; core_operands has those."""
    raw = list(e.core_operands) + [0] * 5
    return hlil.expr(e.operation, raw[0], raw[1], raw[2], raw[3], raw[4], size=e.size,
                     source_location=e.source_location)


def _roots(statement: HighLevelILInstruction):
    if statement.operation == H.HLIL_RET:
        yield from statement.src
    elif statement.operation in (H.HLIL_VAR_INIT, H.HLIL_ASSIGN):
        yield statement.src


def _pure(e) -> bool:
    if e.operation in (H.HLIL_VAR, H.HLIL_CONST, H.HLIL_CONST_PTR):
        return True
    if e.operation not in _BINARY and e.operation not in _UNARY:
        return False
    return all(_pure(op) for op in e.operands if hasattr(op, "operation"))


def _dead_inits(hlil) -> set:
    """Statement indices of pure VAR_INITs whose variable is never read (non-SSA uses)."""
    dead = set()
    for statement in hlil.instructions:
        if statement.operation == H.HLIL_VAR_INIT and _pure(statement.src):
            if not hlil.get_var_uses(statement.dest):
                dead.add(statement.instr_index)
    return dead


def _newly_dead(hlil, dead_before: set):
    for statement in list(hlil.instructions):
        if statement.operation == H.HLIL_VAR_INIT and statement.instr_index not in dead_before and _pure(statement.src):
            if not hlil.get_var_uses(statement.dest):
                yield statement


def _action(ctx) -> None:
    func, hlil = ctx.function, ctx.hlil
    if func is None or hlil is None:
        return
    with _LOCK:
        GENERATIONS.setdefault(func.start, []).append(("hlil", _handle(ctx.mlil), _handle(hlil)))
    if not MODE["enabled"] or (MODE["gate_on_toggle"] and not ctx.get_setting_bool(MLIL_TOGGLE)):
        return
    entry: dict = {"rewrites": [], "error": None, "stage": "plan", "removed": []}
    backups: list = []
    try:
        ssa = hlil.ssa_form
        planned = []
        for statement in list(hlil.instructions):
            for root in _roots(statement):
                root_ssa = root.ssa_form
                lifter = _Lift(ssa, root_ssa, CHECK_VERSIONS["on"])
                ir = lifter.expr(root_ssa)
                if ir is None or not is_mba(ir):
                    continue
                outcome = simplify_expression(ir, load_rules(), default_backends())
                if outcome.rewrite is not None:
                    planned.append((root, outcome.rewrite, lifter.leaves))
        live_before = _dead_inits(hlil)
        entry["stage"] = "replace"
        for root, rewrite, leaves in planned:
            loc = ILSourceLocation.from_instruction(root)
            backups.append((root.expr_index, shallow_copy(hlil, root)))
            hlil.replace_expr(root.expr_index, _lower(hlil, rewrite.replacement, loc))
            entry["rewrites"].append({"address": root.address, "before": to_str(rewrite.original),
                                      "after": to_str(rewrite.replacement), "leaves": leaves})
            if FAIL_AT["stage"] == "replace":
                raise RuntimeError("injected failure after first replacement")
        if not planned:
            return
        if MODE["remove_dead"]:
            entry["stage"] = "cleanup"
            hlil.finalize()
            hlil.generate_ssa_form()
            by_block: dict = {}
            for statement in _newly_dead(hlil, live_before):
                parent = statement.parent
                if parent is None or parent.operation != H.HLIL_BLOCK:
                    continue
                by_block.setdefault(parent.expr_index, (parent, set()))[1].add(statement.expr_index)
                entry["removed"].append(str(statement))
            for parent, removed in by_block.values():
                # Rebuild the enclosing block without the dead statements (a nop would still print).
                body = [stmt.expr_index for stmt in parent.body if stmt.expr_index not in removed]
                backups.append((parent.expr_index, shallow_copy(hlil, parent)))
                hlil.replace_expr(parent.expr_index, hlil.block(body, ILSourceLocation.from_instruction(parent)))
            if FAIL_AT["stage"] == "cleanup":
                raise RuntimeError("injected failure during cleanup")
        entry["stage"] = "finalize"
        if FAIL_AT["stage"] == "finalize":
            raise RuntimeError("injected failure before finalize")
        hlil.finalize()
        entry["stage"] = "ssa"
        if FAIL_AT["stage"] == "ssa":
            raise RuntimeError("injected failure before SSA generation")
        hlil.generate_ssa_form()
        entry["stage"] = "done"
        ctx.hlil = hlil
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["trace"] = traceback.format_exc()
        for index, backup in reversed(backups):
            hlil.replace_expr(index, backup)
        if backups:
            hlil.finalize()
            hlil.generate_ssa_form()
        entry["rolled_back"] = True
    finally:
        _record(func, entry)
