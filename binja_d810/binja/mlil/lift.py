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

from ...ir.bits import compare_with_zero
from ...ir.expr import COUNT_OPS, BinOp, Const, Expr, UnOp, Var, variables
from ..effects import mlil_effect
from ..storage import contained, field_read, fit, merge_partial_write, read_over_write

_BINARY = {Op.MLIL_ADD: "add", Op.MLIL_SUB: "sub", Op.MLIL_MUL: "mul", Op.MLIL_AND: "and",
           Op.MLIL_OR: "or", Op.MLIL_XOR: "xor", Op.MLIL_LSL: "shl", Op.MLIL_LSR: "lshr", Op.MLIL_ASR: "ashr",
           Op.MLIL_ROL: "rol", Op.MLIL_ROR: "ror"}
_UNARY = {Op.MLIL_NEG: "neg", Op.MLIL_NOT: "not", Op.MLIL_ZX: "zx", Op.MLIL_SX: "sx", Op.MLIL_LOW_PART: "trunc"}
SUPPORTED_OPS = frozenset(_BINARY) | frozenset(_UNARY)
_COMPARE_ZERO = {Op.MLIL_CMP_E: "eq", Op.MLIL_CMP_NE: "ne"}


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
         check_versions: bool = True, max_nodes: int = 200, effect_leaves: bool = False,
         inlined: Optional[set] = None, stats: Optional[dict] = None,
         definitions: Optional[dict] = None, keep: frozenset = frozenset()) -> Optional[Expr]:
    """`effect_leaves` is for diagnostics only: effectful sub-expressions of the root become leaves
    without a Binary Ninja ref, so the result can be classified but never lowered. `stats` receives
    the number of IL nodes visited ("visits"), failed inlining attempts included. `definitions`
    memoizes SSA definitions across lifts of one SSA form. `keep` holds (identifier, version) keys of
    SSA values left as leaves when live at the root (ILAdapter.shallow)."""
    lifter = _Lifter(ssa_expr.function, root_instr, check_versions, max_nodes, effect_leaves, definitions)
    lifter.keep = keep
    e = lifter.expr(ssa_expr, inlined=False)
    if stats is not None:
        stats["visits"] = lifter.visits
    if e is not None and inlined is not None:
        inlined.update(lifter.inlined)  # (variable identifier, version) -> SSA variable, each definition followed
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
    def __init__(self, ssa_func, root, check_versions: bool, max_nodes: int, effect_leaves: bool = False,
                 definitions: Optional[dict] = None):
        self.ssa = ssa_func
        self.root = root
        self.check = check_versions
        self.budget = max_nodes
        # Bounded work (v2.1): when values are read several times (VM handlers), the fully inlined
        # tree is exponential and a failed inlining attempt, undone, was retried at every read.
        # Failed attempts are remembered with their budget, inlined values are reused, and no new
        # attempt starts after 3 * max_nodes visits.
        self.visits = 0
        self.max_visits = 3 * max_nodes
        self.failed: dict[tuple, int] = {}  # (identifier, version, width) -> largest budget that failed
        self.done: dict[tuple, tuple] = {}  # (identifier, version, width) -> (expr, nodes, inlined)
        self.definitions = {} if definitions is None else definitions
        self.keep: frozenset = frozenset()
        self.effect_leaves = effect_leaves
        self.names: dict[tuple, str] = {}  # leaf identity -> IR name; names are labels, not identities
        self.backings: dict[tuple, Var] = {}
        self.inlined: dict[tuple[int, int], object] = {}  # (identifier, version) -> SSA variable

    def expr(self, e: MediumLevelILInstruction, *, inlined: bool) -> Optional[Expr]:
        self.visits += 1
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
            if _BINARY[op] not in COUNT_OPS:
                # HLIL folds lea into x * 5 with an 8-byte constant under a 4-byte multiply.
                left, right = fit(left, width), fit(right, width)
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
        elif op == Op.MLIL_BOOL_TO_INT:
            found = self.flag(e.src, width, inlined=inlined)
            if found is not None:
                return found
        if inlined:
            return None  # never move loads or other unsupported expressions across instructions
        if mlil_effect(e) is not None:
            # Only pure leaves may be evaluated again, or not at all.
            return Var(f"effect@{e.expr_index}", width) if self.effect_leaves else None
        return Var(f"opaque@{e.expr_index}", width, ref=Opaque(e.non_ssa_form))

    def var(self, ssa_var: SSAVariable, width: int) -> Optional[Expr]:
        key = (ssa_var.var.identifier, ssa_var.version, width)
        reused = self.reuse(key)
        if reused is not None:
            return reused
        definition = self.definition_of(ssa_var)
        kept = key[:2] in self.keep and self.live(ssa_var)
        if definition is not None and not kept and self.may_inline(key):
            saved, saved_inlined = self.budget, dict(self.inlined)
            inner = self.definition(definition, ssa_var, width)
            if inner is not None:
                self.inlined[(ssa_var.var.identifier, ssa_var.version)] = ssa_var
                self.remember(key, inner, saved, saved_inlined)
                return inner
            self.failed[key] = max(self.failed.get(key, 0), saved)
            self.budget, self.inlined = saved, saved_inlined
        if not self.live(ssa_var):
            return None  # overwritten before the root and not rebuildable from live values
        # One IR name per (variable, version, width): the verifier keys samples and Z3 symbols by name.
        return Var(self.name(("var", ssa_var.var.identifier, ssa_var.version, width), f"{ssa_var.var.name}#{ssa_var.version}"),
                   width, ref=ssa_var)

    def definition_of(self, ssa_var: SSAVariable):
        key = (ssa_var.var.identifier, ssa_var.version)
        if key not in self.definitions:
            self.definitions[key] = self.ssa.get_ssa_var_definition(ssa_var)
        return self.definitions[key]

    def may_inline(self, key: tuple) -> bool:
        return self.budget > self.failed.get(key, 0) and self.visits < self.max_visits

    def reuse(self, key: tuple) -> Optional[Expr]:
        found = self.done.get(key)
        if found is None or found[1] > self.budget:
            return None
        expr, nodes, inlined = found
        self.budget -= nodes
        self.inlined.update(inlined)
        return expr

    def remember(self, key: tuple, expr: Expr, saved_budget: int, saved_inlined: dict) -> None:
        added = {k: v for k, v in self.inlined.items() if k not in saved_inlined}
        self.done[key] = (expr, saved_budget - self.budget, added)

    def flag(self, e: MediumLevelILInstruction, width: int, *, inlined: bool) -> Optional[Expr]:
        """A boolean as a `width`-bit 0 or 1 (x86 setcc, ARM64 cset): a flag variable (cond:0 = t != 0)
        or a comparison with zero of a value that can only be 0 or 1 (ir.bits.compare_with_zero)."""
        if e.operation == Op.MLIL_VAR_SSA and e.size == 0:
            definition = self.definition_of(e.src)
            if definition is None or definition.operation != Op.MLIL_SET_VAR_SSA or not _is_flag(definition.src):
                return None
            found = self.flag(definition.src, width, inlined=True)
            if found is not None:
                self.inlined[(e.src.var.identifier, e.src.version)] = e.src
            return found
        kind = _COMPARE_ZERO.get(e.operation)
        if kind is None:
            return None
        if _is_zero(e.right):
            operand = e.left
        elif _is_zero(e.left):
            operand = e.right
        else:
            return None
        value = self.expr(operand, inlined=inlined)
        return None if value is None else compare_with_zero(value, kind, width)

    def definition(self, definition, ssa_var: SSAVariable, width: int) -> Optional[Expr]:
        if definition.operation == Op.MLIL_SET_VAR_SSA and _is_flag(definition.src):
            return self.flag(definition.src, width, inlined=True)  # r15 = cond:0
        if definition.operation == Op.MLIL_SET_VAR_SSA and definition.src.size * 8 == width:
            return self.expr(definition.src, inlined=True)
        if definition.operation == Op.MLIL_SET_VAR_SSA_FIELD and _declared_bits(ssa_var.var) == width:
            # A write of part of the variable that keeps the rest of it: rax#2 = rax#1 with .al = e.
            value = self.written(definition)
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

    def written(self, definition) -> Optional[Expr]:
        """The value a partial write (SET_VAR_SSA_FIELD) stores: rcx#2.cl = rax#2 != 0 @ rcx#1."""
        if _is_flag(definition.src):
            return self.flag(definition.src, 8 * _written_bytes(definition), inlined=True)
        return self.expr(definition.src, inlined=True)

    def field(self, ssa_var: SSAVariable, offset: int, width: int) -> Optional[Expr]:
        """A read of `width` bits at byte `offset` of a variable's storage (spec section 6.1)."""
        declared = _declared_bits(ssa_var.var)
        definition = self.definition_of(ssa_var)
        kept = (ssa_var.var.identifier, ssa_var.version) in self.keep
        if (definition is not None and definition.operation == Op.MLIL_SET_VAR_SSA_FIELD and self.budget > 0
                and self.visits < self.max_visits and not kept):
            # Read over a partial write: x86 -O0 spills bytes into one slot (var_8:7.b = y; ...).
            kind = read_over_write(offset, width // 8, definition.offset, _written_bytes(definition))
            saved = self.budget
            if kind == "inside":
                value = self.written(definition)
                if value is not None:
                    self.inlined[(ssa_var.var.identifier, ssa_var.version)] = ssa_var
                    return field_read(value, 8 * (offset - definition.offset), width)
            elif kind == "disjoint":
                found = self.field(definition.prev, offset, width)
                if found is not None:
                    return found
            self.budget = saved
        if declared and contained(offset, width // 8, declared // 8):
            # Within the variable itself: its definition, if any, gives every bit.
            definition = self.definition_of(ssa_var)
            if definition is not None and self.budget > 0 and self.visits < self.max_visits and not kept:
                saved = self.budget
                whole = self.definition(definition, ssa_var, declared)
                if whole is not None:
                    self.inlined[(ssa_var.var.identifier, ssa_var.version)] = ssa_var
                    return field_read(whole, 8 * offset, width)
                self.budget = saved
        backing = self.backing(self.forward(ssa_var, offset, width // 8))
        if backing is None or not contained(offset, width // 8, backing.width // 8):
            return None
        return field_read(backing, 8 * offset, width)

    def forward(self, ssa_var: SSAVariable, offset: int, size: int) -> SSAVariable:
        """The current version of the variable, if no partial write since `ssa_var` touched these
        bytes: Windows x64 -O0 spills x to var_8:6.b and then reuses rcx, so x#0 is dead at the
        root while var_8's current version still holds the byte."""
        if not self.check:
            return ssa_var
        current = self.root.get_ssa_var_version(ssa_var.var)
        version = current
        for _ in range(64):
            if version == ssa_var.version:
                return SSAVariable(ssa_var.var, current)
            definition = self.definition_of(SSAVariable(ssa_var.var, version))
            if (definition is None or definition.operation != Op.MLIL_SET_VAR_SSA_FIELD
                    or definition.prev.var != ssa_var.var
                    or read_over_write(offset, size, definition.offset, _written_bytes(definition)) != "disjoint"):
                return ssa_var
            version = definition.prev.version
        return ssa_var

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
        groups: dict[tuple, list[Var]] = {}
        for leaf in variables(e):
            if isinstance(leaf.ref, SSAVariable):
                key = (int(leaf.ref.var.source_type), leaf.ref.var.storage, leaf.ref.version)
                groups.setdefault(key, []).append(leaf)
        mapping = {}
        for key, leaves in groups.items():
            # A version also read through fields, or read at two widths (x as 32 and 64 bits):
            # the reads share their low bits, and an IR variable has a single width.
            if key not in self.backings and len({leaf.width for leaf in leaves}) < 2:
                continue
            backing = self.backing(leaves[0].ref)
            if backing is None:
                continue
            for leaf in leaves:
                if leaf.width <= backing.width:
                    mapping[(leaf.name, leaf.width)] = field_read(backing, 0, leaf.width)
        return _substitute_leaves(e, mapping) if mapping else e

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


def _substitute_leaves(e: Expr, mapping: dict[tuple[str, int], Expr]) -> Expr:
    """Like ir.expr.substitute, keyed by (name, width): one name may be read at two widths."""
    if isinstance(e, Var):
        return mapping.get((e.name, e.width), e)
    if isinstance(e, Const):
        return e
    if isinstance(e, UnOp):
        return UnOp(e.op, _substitute_leaves(e.operand, mapping), e.width)
    return BinOp(e.op, _substitute_leaves(e.left, mapping), _substitute_leaves(e.right, mapping), e.width)


def _is_flag(e: MediumLevelILInstruction) -> bool:
    return e.operation in _COMPARE_ZERO or (e.operation == Op.MLIL_VAR_SSA and e.size == 0)


def _written_bytes(definition) -> int:
    """Bytes a partial write stores: its own size (a comparison's size is its operands')."""
    return definition.size if _is_flag(definition.src) else definition.src.size


def _is_zero(e: MediumLevelILInstruction) -> bool:
    return e.operation in (Op.MLIL_CONST, Op.MLIL_CONST_PTR) and e.constant == 0


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
