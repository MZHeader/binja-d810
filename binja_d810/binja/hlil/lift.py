"""HLIL (SSA) -> binja_d810 IR (spec section 5.2).

HLIL SSA numbers versions per variable (docs/design/spikes/2026-10-15-v2-hlil.md, gate 2), so
a parameter read after MLIL reused its register is still the parameter. Version safety is checked
per variable: every leaf must hold the version of its variable that is current at the statement
holding the root, because the rewrite is lowered there and reads variables by name. Definitions
(VAR_INIT and SSA assignments of supported expressions) are inlined, so their own reads need not
be live; only the final leaves are checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from binaryninja import HighLevelILInstruction, HighLevelILOperation as H

from ...ir.expr import COUNT_OPS, BinOp, Const, Expr, UnOp, Var, variables
from ..effects import hlil_effect
from ..storage import contained, field_read, fit

_BINARY = {H.HLIL_ADD: "add", H.HLIL_SUB: "sub", H.HLIL_MUL: "mul", H.HLIL_AND: "and", H.HLIL_OR: "or",
           H.HLIL_XOR: "xor", H.HLIL_LSL: "shl", H.HLIL_LSR: "lshr", H.HLIL_ASR: "ashr", H.HLIL_ROL: "rol",
           H.HLIL_ROR: "ror"}
_UNARY = {H.HLIL_NEG: "neg", H.HLIL_NOT: "not", H.HLIL_ZX: "zx", H.HLIL_SX: "sx", H.HLIL_LOW_PART: "trunc"}
_COMPARE = {H.HLIL_CMP_E: "eq", H.HLIL_CMP_NE: "ne", H.HLIL_CMP_ULT: "ult", H.HLIL_CMP_ULE: "ule",
            H.HLIL_CMP_UGT: "ugt", H.HLIL_CMP_UGE: "uge", H.HLIL_CMP_SLT: "slt", H.HLIL_CMP_SLE: "sle",
            H.HLIL_CMP_SGT: "sgt", H.HLIL_CMP_SGE: "sge"}
SUPPORTED_OPS = frozenset(_BINARY) | frozenset(_UNARY) | frozenset(_COMPARE) | {H.HLIL_BOOL_TO_INT}
STRUCTURE = frozenset({H.HLIL_BLOCK, H.HLIL_IF, H.HLIL_WHILE, H.HLIL_DO_WHILE, H.HLIL_FOR, H.HLIL_SWITCH,
                       H.HLIL_CASE})
_DEFINITIONS = frozenset({H.HLIL_VAR_INIT_SSA, H.HLIL_ASSIGN})


@dataclass(frozen=True)
class FieldRef:
    """A read of `size` bytes at byte `offset` of a variable (HLIL_STRUCT_FIELD of a variable)."""

    var: object
    offset: int
    size: int
    member: int  # the raw member-index operand, copied as is


def children(node: HighLevelILInstruction) -> Iterator[HighLevelILInstruction]:
    for operand in node.operands:
        if isinstance(operand, HighLevelILInstruction):
            yield operand
        elif isinstance(operand, list):
            yield from (item for item in operand if isinstance(item, HighLevelILInstruction))


def expression_roots(hlil) -> Iterator[HighLevelILInstruction]:
    """Each statement and each condition of a structured statement, once."""
    stack = [hlil.root]
    while stack:
        node = stack.pop()
        if node.operation in STRUCTURE:
            stack.extend(children(node))
        else:
            yield node


def candidate_roots(statement: HighLevelILInstruction, aliased: frozenset,
                    rejected: Optional[list] = None) -> Iterator[HighLevelILInstruction]:
    """Maximal supported expressions that read only variables, their fields and constants.

    Calls, dereferences, aliased reads and any other operation bound candidates: their pure
    supported sub-expressions are candidates instead (spec section 5.2).
    """
    def visit(e):
        if e.operation in SUPPORTED_OPS:
            reason = hlil_effect(e, aliased)
            if reason is None:
                yield e
                return
            if rejected is not None:
                rejected.append((e, reason))
        for child in children(e):
            if child.operation not in STRUCTURE:
                yield from visit(child)

    yield from visit(statement)


def lift(ssa_expr: HighLevelILInstruction, *, aliased: frozenset = frozenset(), check_versions: bool = True,
         max_nodes: int = 200, effect_leaves: bool = False, inlined: Optional[set] = None,
         stats: Optional[dict] = None, definitions: Optional[dict] = None,
         keep: frozenset = frozenset()) -> Optional[Expr]:
    """`aliased` holds the address-taken variables (HighLevelILFunction.aliased_vars): their reads
    are never moved. `effect_leaves` is for diagnostics only: other operations become leaves
    without a ref, so the result can be classified but never lowered. `stats` receives the number of
    IL nodes visited ("visits"), failed inlining attempts included. `definitions` memoizes SSA
    definitions across lifts of one SSA form. `keep` holds (identifier, version) keys of SSA values
    left as leaves when live at the root (ILAdapter.shallow)."""
    lifter = _Lifter(ssa_expr, aliased, check_versions, max_nodes, effect_leaves, definitions)
    lifter.keep = keep
    e = lifter.expr(ssa_expr, inlined=False)
    if stats is not None:
        stats["visits"] = lifter.visits
    if e is not None and inlined is not None:
        inlined.update(lifter.inlined)  # (variable identifier, version) -> SSA variable
    return None if e is None else _relate(e)


@dataclass(frozen=True)
class HLILBacking:
    """One SSA version of a variable read at several widths (x as 32 and 64 bits on x64): a value
    of the widest read's width whose bits beyond each narrower read are unknown."""

    var: object
    version: int


def _relate(e: Expr) -> Expr:
    groups: dict[tuple, list[Var]] = {}
    for leaf in variables(e):
        ref = leaf.ref
        if ref is not None and hasattr(ref, "version") and hasattr(ref, "var"):
            groups.setdefault((ref.var.identifier, ref.version), []).append(leaf)
    mapping = {}
    for leaves in groups.values():
        widths = {leaf.width for leaf in leaves}
        if len(widths) < 2:
            continue
        ref = leaves[0].ref
        backing = Var(f"@{ref.var.name}#{ref.version}", max(widths), ref=HLILBacking(ref.var, ref.version))
        for leaf in leaves:
            mapping[(leaf.name, leaf.width)] = field_read(backing, 0, leaf.width)
    return _substitute_leaves(e, mapping) if mapping else e


def _substitute_leaves(e: Expr, mapping: dict) -> Expr:
    if isinstance(e, Var):
        return mapping.get((e.name, e.width), e)
    if isinstance(e, Const):
        return e
    if isinstance(e, UnOp):
        return UnOp(e.op, _substitute_leaves(e.operand, mapping), e.width)
    return BinOp(e.op, _substitute_leaves(e.left, mapping), _substitute_leaves(e.right, mapping), e.width)


class _Lifter:
    def __init__(self, root: HighLevelILInstruction, aliased: frozenset, check: bool, max_nodes: int,
                 effect_leaves: bool, definitions: Optional[dict] = None):
        self.root = root
        self.aliased = aliased
        self.ssa = root.function
        self.check = check
        self.budget = max_nodes
        # Bounded work: as in binja_d810.binja.mlil.lift (failed attempts remembered, inlined values
        # reused, no new attempt after 3 * max_nodes visits).
        self.visits = 0
        self.max_visits = 3 * max_nodes
        self.failed: dict[tuple, int] = {}
        self.done: dict[tuple, tuple] = {}
        self.definitions = {} if definitions is None else definitions
        self.keep: frozenset = frozenset()
        self.effect_leaves = effect_leaves
        self.names: dict[tuple, str] = {}
        self.inlined: dict[tuple[int, int], object] = {}  # (identifier, version) -> SSA variable

    def expr(self, e: HighLevelILInstruction, *, inlined: bool) -> Optional[Expr]:
        self.visits += 1
        self.budget -= 1
        if self.budget < 0:
            return None
        op, width = e.operation, e.size * 8
        if op in _COMPARE:
            return self.compare(e, inlined=inlined)
        if op == H.HLIL_BOOL_TO_INT:
            found = self.expr(e.src, inlined=inlined)
            return None if found is None or found.width != 1 or width == 0 else UnOp("zx", found, width)
        if width == 0:
            return None
        if op in (H.HLIL_CONST, H.HLIL_CONST_PTR):
            return Const(e.constant, width)
        if op == H.HLIL_VAR_SSA:
            return self.var(e.var, width)
        if op == H.HLIL_STRUCT_FIELD and e.src.operation == H.HLIL_VAR_SSA:
            return self.field(e, width)
        if op in _BINARY:
            left, right = self.expr(e.left, inlined=inlined), self.expr(e.right, inlined=inlined)
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
        if op in _UNARY:
            operand = self.expr(e.src, inlined=inlined)
            if operand is None:
                return None
            try:
                return UnOp(_UNARY[op], operand, width)
            except ValueError:
                return None
        if self.effect_leaves and not inlined:
            return Var(f"effect@{e.expr_index}", width)
        return None

    def var(self, ssa_var, width: int) -> Optional[Expr]:
        key = (ssa_var.var.identifier, ssa_var.version, width)
        found = self.done.get(key)
        if found is not None and found[1] <= self.budget:
            expr, nodes, inlined = found
            self.budget -= nodes
            self.inlined.update(inlined)
            return expr
        definition = self.definition_of(ssa_var)
        if key[:2] in self.keep and self.live(ssa_var):
            definition = None  # a value that stays visible elsewhere (ILAdapter.shallow)
        src = definition.src if definition is not None and definition.operation in _DEFINITIONS else None
        boolean = src is not None and (src.operation in _COMPARE or src.operation == H.HLIL_BOOL_TO_INT)
        if (src is not None and (boolean or src.size * 8 == width)
                and self.budget > self.failed.get(key, 0) and self.visits < self.max_visits
                and ssa_var.var not in self.aliased):
            saved, saved_inlined = self.budget, dict(self.inlined)
            if hlil_effect(src, self.aliased) is None:
                inner = self.expr(src, inlined=True)
                if inner is not None and boolean and inner.width != width:
                    # r15_1 = rax_1 != 0: a 1-bit comparison read as an integer of the variable's width
                    inner = UnOp("zx", inner, width) if inner.width == 1 and width > 1 else None
                if inner is not None:
                    self.inlined[(ssa_var.var.identifier, ssa_var.version)] = ssa_var
                    added = {k: v for k, v in self.inlined.items() if k not in saved_inlined}
                    self.done[key] = (inner, saved - self.budget, added)
                    return inner
            self.failed[key] = max(self.failed.get(key, 0), saved)
            self.budget, self.inlined = saved, saved_inlined
        if not self.live(ssa_var) or ssa_var.var in self.aliased:
            return None  # redefined before the root and not rebuildable from live values
        label = f"{ssa_var.var.name}#{ssa_var.version}"
        return Var(self.name(("var", ssa_var.var.identifier, ssa_var.version, width), label), width,
                   ref=ssa_var)

    def compare(self, e: HighLevelILInstruction, *, inlined: bool) -> Optional[Expr]:
        """A comparison: 1 bit, over operands of the comparison's own size."""
        width = e.size * 8
        left, right = self.expr(e.left, inlined=inlined), self.expr(e.right, inlined=inlined)
        left = None if left is None else fit(left, width)
        right = None if right is None else fit(right, width)
        if left is None or right is None:
            return None
        return BinOp(_COMPARE[e.operation], left, right, 1)

    def definition_of(self, ssa_var):
        key = (ssa_var.var.identifier, ssa_var.version)
        if key not in self.definitions:
            self.definitions[key] = self.ssa.get_ssa_var_definition(ssa_var)
        return self.definitions[key]

    def field(self, e: HighLevelILInstruction, width: int) -> Optional[Expr]:
        ssa_var = e.src.var
        declared = e.src.size * 8
        if contained(e.offset, width // 8, declared // 8) and ssa_var.var not in self.aliased:
            # r11_1.d where r11_1 = zx.q(y) & zx.q(x): the definition gives every bit.
            whole = self.var(ssa_var, declared)
            if whole is not None and not (isinstance(whole, Var) and whole.ref is ssa_var):
                return field_read(whole, 8 * e.offset, width)
        if not self.live(ssa_var):
            return None
        label = f"{ssa_var.var.name}#{ssa_var.version}:{e.offset}.{width // 8}"
        ref = FieldRef(ssa_var.var, e.offset, width // 8, e.core_operands[2])
        return Var(self.name(("field", ssa_var.var.identifier, ssa_var.version, e.offset, width), label), width,
                   ref=ref)

    def live(self, ssa_var) -> bool:
        return not self.check or self.root.get_ssa_var_version(ssa_var.var) == ssa_var.version

    def name(self, identity: tuple, label: str) -> str:
        if identity not in self.names:
            taken = set(self.names.values())
            name = label
            while name in taken:
                name += "'"
            self.names[identity] = name
        return self.names[identity]
