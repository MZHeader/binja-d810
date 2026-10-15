"""Per-function fixpoint: lift candidates, simplify, lower, replace_expr; roll back on failure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from binaryninja import ILSourceLocation, MediumLevelILFunction, MediumLevelILInstruction, MediumLevelILOperation as Op

from ..ir.expr import is_mba, to_str
from ..rules.base import Rule
from ..semantic.backend import Tier2Backend
from ..simplify import Limits, simplify_expression
from .lift import candidate_roots, lift
from .lower import lower

_ARITH = frozenset({Op.MLIL_ADD, Op.MLIL_SUB, Op.MLIL_MUL, Op.MLIL_NEG, Op.MLIL_LSL})
_BITWISE = frozenset({Op.MLIL_AND, Op.MLIL_OR, Op.MLIL_XOR, Op.MLIL_NOT})


@dataclass(frozen=True)
class EngineConfig:
    limits: Limits = Limits()
    max_iterations: int = 10
    tier2: bool = True
    disabled_rules: frozenset[str] = frozenset()


@dataclass
class FunctionReport:
    rewrites: list[tuple[int, str, str, str]] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    capped: bool = False
    still_firing: list[str] = field(default_factory=list)
    rolled_back: bool = False


def _operations(e: MediumLevelILInstruction):
    stack = [e]
    while stack:
        node = stack.pop()
        yield node.operation
        stack.extend(op for op in node.operands if isinstance(op, MediumLevelILInstruction))


def _has_mixed_ops(mlil: MediumLevelILFunction) -> bool:
    seen = set()
    for instr in mlil.instructions:
        for root in candidate_roots(instr):
            seen.update(_operations(root))
    return bool(seen & _ARITH) and bool(seen & _BITWISE)


def _cfg_shape(mlil: MediumLevelILFunction) -> tuple:
    return tuple((bb.start, bb.end, mlil[bb.end - 1].operation) for bb in mlil.basic_blocks)


def simplify_function(mlil: MediumLevelILFunction, rules: Sequence[Rule],
                      backends: Sequence[Tier2Backend], config: EngineConfig) -> FunctionReport:
    report = FunctionReport()
    if not _has_mixed_ops(mlil):
        return report
    shape = _cfg_shape(mlil)
    backups: list[tuple[int, int]] = []
    dead = set(config.disabled_rules)
    try:
        for _ in range(config.max_iterations):
            if mlil.ssa_form is None:
                mlil.generate_ssa_form()
            planned = []
            for instr in list(mlil.instructions):
                for root in candidate_roots(instr):
                    ir = lift(root.ssa_form, root_instr=instr, max_nodes=config.limits.max_nodes)
                    if ir is None or not is_mba(ir):
                        continue
                    outcome = simplify_expression(ir, rules, backends, config.limits, frozenset(dead))
                    for source, message in outcome.errors:
                        dead.add(source)
                        report.errors.append((source, message))
                    if outcome.skipped:
                        report.skipped.append((root.address, outcome.skipped))
                    if outcome.rewrite is not None:
                        planned.append((root, outcome.rewrite))
            if not planned:
                break
            for root, rewrite in planned:
                loc = ILSourceLocation.from_instruction(root)
                backups.append((root.expr_index, root.copy_to(mlil)))
                mlil.replace_expr(root.expr_index, lower(mlil, rewrite.replacement, loc))
                report.rewrites.append((root.address, rewrite.source, to_str(rewrite.original), to_str(rewrite.replacement)))
            mlil.finalize()
            mlil.generate_ssa_form()
            last_sources = sorted({rewrite.source for _, rewrite in planned})
        else:
            report.capped = True
            report.still_firing = last_sources
        if _cfg_shape(mlil) != shape:
            raise RuntimeError("control flow changed by expression rewrites")
    except Exception as exc:
        report.errors.append(("engine", f"{type(exc).__name__}: {exc}"))
        for expr_index, backup in reversed(backups):
            mlil.replace_expr(expr_index, backup)
        if backups:
            mlil.finalize()
            mlil.generate_ssa_form()
        report.rewrites.clear()
        report.rolled_back = True
    return report
