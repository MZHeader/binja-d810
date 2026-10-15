"""MLIL (SSA) -> binja_d810 IR, following SSA definitions across instructions.

Version safety: every SSA variable left as a leaf must hold the version that is current at the
root instruction, because the rewrite is lowered there and reads variables by name. Binary Ninja
numbers SSA versions per storage location (x and x0 share register x0), so a variable counts as
overwritten when another variable in the same register is written. Intermediate definitions are
inlined, so their own reads need not be live; only the final leaves are checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from binaryninja import MediumLevelILInstruction, MediumLevelILOperation as Op, SSAVariable, VariableSourceType

from ..ir.expr import BinOp, Const, Expr, UnOp, Var, substitute, variables
from .effects import mlil_effect
from .storage import contained, field_read, merge_partial_write

_BINARY = {Op.MLIL_ADD: "add", Op.MLIL_SUB: "sub", Op.MLIL_MUL: "mul", Op.MLIL_AND: "and",
           Op.MLIL_OR: "or", Op.MLIL_XOR: "xor", Op.MLIL_LSL: "shl", Op.MLIL_LSR: "lshr", Op.MLIL_ASR: "ashr"}
_UNARY = {Op.MLIL_NEG: "neg", Op.MLIL_NOT: "not", Op.MLIL_ZX: "zx", Op.MLIL_SX: "sx", Op.MLIL_LOW_PART: "trunc"}
SUPPORTED_OPS = frozenset(_BINARY) | frozenset(_UNARY)


@dataclass(frozen=True)
class Opaque:
    """Leaf for a pure unsupported sub-expression of the root instruction (non-SSA form)."""

    expr: MediumLevelILInstruction


def candidate_roots(instr: MediumLevelILInstruction, rejected: Optional[list] = None) -> Iterator[MediumLevelILInstruction]:
    """Maximal supported expressions that contain no effect (spec section 5.2).

    A supported expression containing a load, call or other effect is not a candidate: rewriting
    it could delete, duplicate or reorder the effect (0 & [p] -> 0). Its effect-free supported
    sub-expressions are candidates instead. Each such split is appended to `rejected` as
    (expression, reason).
    """

    def visit(e):
        if e.operation in SUPPORTED_OPS:
            reason = mlil_effect(e.ssa_form)
            if reason is None:
                yield e
                return
            if rejected is not None:
                rejected.append((e, reason))
        for operand in e.operands:
            if isinstance(operand, MediumLevelILInstruction):
                yield from visit(operand)
            elif isinstance(operand, list):
                for item in operand:
                    if isinstance(item, MediumLevelILInstruction):
                        yield from visit(item)

    yield from visit(instr)


def lift(ssa_expr: MediumLevelILInstruction, *, root_instr: MediumLevelILInstruction,
         check_versions: bool = True, max_nodes: int = 200, effect_leaves: bool = False) -> Optional[Expr]:
    """`effect_leaves` is for diagnostics only: effectful sub-expressions of the root become leaves
    without a Binary Ninja ref, so the result can be classified but never lowered."""
    lifter = _Lifter(ssa_expr.function, root_instr, check_versions, max_nodes, effect_leaves)
    e = lifter.expr(ssa_expr, inlined=False)
    return None if e is None else lifter.relate(e)


@dataclass(frozen=True)
class Backing:
    """One SSA version of a whole storage location (spec section 6.1): every bit unknown.

    `var` is a variable living in that storage, used to lower reads of it.
    """

    var: object
    version: int
    size: int  # bytes


class _Lifter:
    def __init__(self, ssa_func, root, check_versions: bool, max_nodes: int, effect_leaves: bool = False):
        self.ssa = ssa_func
        self.root = root
        self.check = check_versions
        self.budget = max_nodes
        self.effect_leaves = effect_leaves
        self.names: dict[tuple, str] = {}  # leaf identity -> IR name; names are labels, not identities
        self.backings: dict[tuple, Var] = {}

    def expr(self, e: MediumLevelILInstruction, *, inlined: bool) -> Optional[Expr]:
        self.budget -= 1
        if self.budget < 0:
            return None
        op, width = e.operation, e.size * 8
        if width == 0:
            return None
        if op in (Op.MLIL_CONST, Op.MLIL_CONST_PTR):
            return Const(e.constant, width)
        if op == Op.MLIL_VAR_SSA:
            return self.var(e.src, width)
        if op == Op.MLIL_VAR_SSA_FIELD:
            found = self.field(e.src, e.offset, width)
            if found is not None or inlined:
                return found
        elif op in _BINARY:
            left = self.expr(e.left, inlined=inlined)
            right = self.expr(e.right, inlined=inlined)
            if left is None or right is None:
                return None
            try:
                return BinOp(_BINARY[op], left, right, width)
            except ValueError:
                return None
        elif op in _UNARY:
            operand = self.expr(e.src, inlined=inlined)
            if operand is None:
                return None
            try:
                return UnOp(_UNARY[op], operand, width)
            except ValueError:
                return None
        if inlined:
            return None  # never move loads or other unsupported expressions across instructions
        if mlil_effect(e) is not None:
            # Only pure leaves may be evaluated again, or not at all.
            return Var(f"effect@{e.expr_index}", width) if self.effect_leaves else None
        return Var(f"opaque@{e.expr_index}", width, ref=Opaque(e.non_ssa_form))

    def var(self, ssa_var: SSAVariable, width: int) -> Optional[Expr]:
        definition = self.ssa.get_ssa_var_definition(ssa_var)
        if definition is not None and self.budget > 0:
            saved = self.budget
            inner = self.definition(definition, ssa_var, width)
            if inner is not None:
                return inner
            self.budget = saved
        if not self.live(ssa_var):
            return None  # overwritten before the root and not rebuildable from live values
        return Var(self.name(("var", ssa_var.var.identifier, ssa_var.version), f"{ssa_var.var.name}#{ssa_var.version}"),
                   width, ref=ssa_var)

    def definition(self, definition, ssa_var: SSAVariable, width: int) -> Optional[Expr]:
        if definition.operation == Op.MLIL_SET_VAR_SSA and definition.src.size * 8 == width:
            return self.expr(definition.src, inlined=True)
        if definition.operation == Op.MLIL_SET_VAR_SSA_FIELD and _declared_bits(ssa_var.var) == width:
            # A write of part of the variable that keeps the rest of it: rax#2 = rax#1 with .al = e.
            value = self.expr(definition.src, inlined=True)
            if value is None or 8 * definition.offset + value.width > width:
                return None
            old = self.var(definition.prev, width)
            if old is None:
                return None
            try:
                return merge_partial_write(old, value, 8 * definition.offset)
            except ValueError:
                return None
        return None

    def field(self, ssa_var: SSAVariable, offset: int, width: int) -> Optional[Expr]:
        """A read of `width` bits at byte `offset` of a variable's storage (spec section 6.1)."""
        declared = _declared_bits(ssa_var.var)
        if declared and contained(offset, width // 8, declared // 8):
            # Within the variable itself: its definition, if any, gives every bit.
            definition = self.ssa.get_ssa_var_definition(ssa_var)
            if definition is not None and self.budget > 0:
                saved = self.budget
                whole = self.definition(definition, ssa_var, declared)
                if whole is not None:
                    return field_read(whole, 8 * offset, width)
                self.budget = saved
        backing = self.backing(ssa_var)
        if backing is None or not contained(offset, width // 8, backing.width // 8):
            return None
        return field_read(backing, 8 * offset, width)

    def backing(self, ssa_var: SSAVariable) -> Optional[Var]:
        var = ssa_var.var
        size = _storage_bytes(self.ssa, var)
        if size is None or not self.live(ssa_var):
            return None
        key = (int(var.source_type), var.storage, ssa_var.version)
        if key not in self.backings:
            label = f"@{_storage_name(self.ssa, var)}#{ssa_var.version}"
            self.backings[key] = Var(self.name(("backing",) + key, label), 8 * size,
                                     ref=Backing(var, ssa_var.version, size))
        return self.backings[key]

    def relate(self, e: Expr) -> Expr:
        """Express plain reads of a storage version that also has field reads through its backing
        value, so x and x.w0 are known to share their low byte."""
        if not self.backings:
            return e
        mapping = {}
        for leaf in variables(e):
            if not isinstance(leaf.ref, SSAVariable):
                continue
            key = (int(leaf.ref.var.source_type), leaf.ref.var.storage, leaf.ref.version)
            backing = self.backings.get(key)
            if backing is not None and leaf.width <= backing.width:
                mapping[leaf.name] = field_read(backing, 0, leaf.width)
        return substitute(e, mapping) if mapping else e

    def live(self, ssa_var: SSAVariable) -> bool:
        return not self.check or self.root.get_ssa_var_version(ssa_var.var) == ssa_var.version

    def name(self, identity: tuple, label: str) -> str:
        if identity not in self.names:
            taken = set(self.names.values())
            name = label
            while name in taken:
                name += "'"
            self.names[identity] = name
        return self.names[identity]


def _declared_bits(var) -> int:
    try:
        return 8 * var.type.width if var.type is not None else 0
    except Exception:
        return 0


def _storage_name(ssa_func, var) -> str:
    if var.source_type == VariableSourceType.RegisterVariableSourceType:
        return ssa_func.arch.get_reg_name(var.storage)
    return f"stack{var.storage:+#x}"


def _storage_bytes(ssa_func, var) -> Optional[int]:
    """Width of the storage a variable lives in: its register, or its own type on the stack."""
    if var.source_type == VariableSourceType.RegisterVariableSourceType:
        info = ssa_func.arch.regs.get(ssa_func.arch.get_reg_name(var.storage))
        return info.size if info is not None else None
    if var.source_type == VariableSourceType.StackVariableSourceType:
        return _declared_bits(var) // 8 or None
    return None
