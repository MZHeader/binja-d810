"""Per-function fixpoint: lift candidates, simplify, lower, replace_expr; roll back on failure."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple, Sequence

from binaryninja import (
    ILSourceLocation, MediumLevelILFunction, MediumLevelILInstruction, MediumLevelILOperation as Op, SSAVariable,
)

from ..ir.expr import is_mba, to_str
from ..rules.base import Rule
from ..semantic.backend import Tier2Backend
from ..simplify import Limits, simplify_expression
from .lift import SUPPORTED_OPS, candidate_roots, lift
from .lower import lower

_ARITH = frozenset({Op.MLIL_ADD, Op.MLIL_SUB, Op.MLIL_MUL, Op.MLIL_NEG, Op.MLIL_LSL})
_BITWISE = frozenset({Op.MLIL_AND, Op.MLIL_OR, Op.MLIL_XOR, Op.MLIL_NOT})


@dataclass(frozen=True)
class EngineConfig:
    limits: Limits = Limits()
    max_iterations: int = 10
    tier2: bool = True
    disabled_rules: frozenset[str] = frozenset()


class RewriteRecord(NamedTuple):
    address: int
    source: str
    before: str
    after: str
    verdict: str  # "PROVEN" or "TESTED": the original -> replacement check (spec section 10)
    method: str  # "signature", "z3" or "sampling"
    reason: str  # why a TESTED rewrite has no proof
    level: str = "mlil"


class SkipRecord(NamedTuple):
    address: int
    reason: str
    level: str = "mlil"


class ErrorRecord(NamedTuple):
    source: str
    message: str
    level: str = "mlil"


@dataclass
class FunctionReport:
    rewrites: list[RewriteRecord] = field(default_factory=list)
    skipped: list[SkipRecord] = field(default_factory=list)
    errors: list[ErrorRecord] = field(default_factory=list)
    capped: bool = False
    still_firing: list[str] = field(default_factory=list)
    rolled_back: bool = False
    refuted: int = 0  # candidate results the checker refuted; never applied
    rolled_back_rewrites: int = 0  # attempted, then undone; never counted as applied

    def assurance(self) -> Counter:
        return Counter(r.verdict for r in self.rewrites)


def _operations(e: MediumLevelILInstruction):
    stack = [e]
    while stack:
        node = stack.pop()
        yield node.operation
        stack.extend(op for op in node.operands if isinstance(op, MediumLevelILInstruction))


def _has_mixed_ops(mlil: MediumLevelILFunction) -> bool:
    seen = set()
    for instr in mlil.instructions:
        rejected: list = []
        for root in candidate_roots(instr, rejected):
            seen.update(_operations(root))
        for expr, _ in rejected:
            seen.update(_operations(expr))
    return bool(seen & _ARITH) and bool(seen & _BITWISE)


_PURE_LEAVES = frozenset({Op.MLIL_VAR_SSA, Op.MLIL_VAR_SSA_FIELD, Op.MLIL_CONST, Op.MLIL_CONST_PTR})


def _pure(e: MediumLevelILInstruction) -> bool:
    if e.operation in _PURE_LEAVES:
        return True
    if e.operation not in SUPPORTED_OPS:
        return False
    return all(_pure(op) for op in e.operands if isinstance(op, MediumLevelILInstruction))


def _dead_assignments(mlil: MediumLevelILFunction) -> set[int]:
    """Indices of pure SET_VAR instructions whose value is never used, even indirectly.

    At -O0 a rewritten result leaves chains of copies through stack slots behind
    (x8_1 = x8 ^ x9; var_10 = x8_1; x8_2 = var_10; ...), which HLIL would still show.
    """
    ssa = mlil.ssa_form
    pure_defs = {}
    live: set = set()
    work = []
    for instr in ssa.instructions:
        if instr.operation == Op.MLIL_SET_VAR_SSA and _pure(instr.src):
            pure_defs[_key(instr.dest)] = instr
        else:
            work.extend(_key(v) for v in _ssa_vars(instr))
    while work:
        key = work.pop()
        if key in live:
            continue
        live.add(key)
        if key in pure_defs:
            work.extend(_key(v) for v in _ssa_vars(pure_defs[key].src))
    return {instr.non_ssa_form.instr_index for key, instr in pure_defs.items() if key not in live}


def _ssa_vars(e: MediumLevelILInstruction):
    """Every SSA variable in an expression tree. MLIL_RET's vars_read is empty, so walk operands.

    For instructions with side effects this also yields what they write (calls, phis), which only
    keeps more alive.
    """
    stack: list = [e]
    while stack:
        item = stack.pop()
        if isinstance(item, SSAVariable):
            yield item
        elif isinstance(item, MediumLevelILInstruction):
            stack.extend(item.operands)
        elif isinstance(item, list):
            stack.extend(item)


def _key(ssa_var: SSAVariable) -> tuple[int, int]:
    return ssa_var.var.identifier, ssa_var.version


def _remove(mlil: MediumLevelILFunction, indices: set[int], backups: list) -> None:
    for index in sorted(indices):
        instr = mlil[index]
        backups.append((instr.expr_index, instr.copy_to(mlil)))
        mlil.replace_expr(instr.expr_index, mlil.nop(ILSourceLocation.from_instruction(instr)))


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
                rejected: list = []
                roots = list(candidate_roots(instr, rejected))
                for expr, reason in rejected:
                    seen = lift(expr.ssa_form, root_instr=instr, max_nodes=config.limits.max_nodes, effect_leaves=True)
                    if seen is not None and is_mba(seen):
                        report.skipped.append(SkipRecord(expr.address, reason))
                for root in roots:
                    ir = lift(root.ssa_form, root_instr=instr, max_nodes=config.limits.max_nodes)
                    if ir is None or not is_mba(ir):
                        continue
                    outcome = simplify_expression(ir, rules, backends, config.limits, frozenset(dead))
                    report.refuted += outcome.refuted
                    for source, message in outcome.errors:
                        if source != "verify":
                            dead.add(source)
                        report.errors.append(ErrorRecord(source, message))
                    if outcome.skipped:
                        report.skipped.append(SkipRecord(root.address, outcome.skipped))
                    if outcome.rewrite is not None:
                        planned.append((root, outcome.rewrite))
            if not planned:
                break
            dead_before = _dead_assignments(mlil)
            for root, rewrite in planned:
                loc = ILSourceLocation.from_instruction(root)
                backups.append((root.expr_index, root.copy_to(mlil)))
                mlil.replace_expr(root.expr_index, lower(mlil, rewrite.replacement, loc))
                report.rewrites.append(RewriteRecord(
                    root.address, rewrite.source, to_str(rewrite.original), to_str(rewrite.replacement),
                    rewrite.verdict.name if rewrite.verdict else "UNCHECKED", rewrite.method, rewrite.reason))
            mlil.finalize()
            mlil.generate_ssa_form()
            # Only what these rewrites made dead: ordinary dead stores (z_1 = z at -O0) stay.
            newly_dead = _dead_assignments(mlil) - dead_before
            if newly_dead:
                _remove(mlil, newly_dead, backups)
                mlil.finalize()
                mlil.generate_ssa_form()
            last_sources = sorted({rewrite.source for _, rewrite in planned})
        else:
            report.capped = True
            report.still_firing = last_sources
        if _cfg_shape(mlil) != shape:
            raise RuntimeError("control flow changed by expression rewrites")
    except Exception as exc:
        report.errors.append(ErrorRecord("engine", f"{type(exc).__name__}: {exc}"))
        for expr_index, backup in reversed(backups):
            mlil.replace_expr(expr_index, backup)
        if backups:
            mlil.finalize()
            mlil.generate_ssa_form()
        report.rolled_back_rewrites = len(report.rewrites)
        report.rewrites.clear()
        report.rolled_back = True
    return report
