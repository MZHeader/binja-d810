"""The unflattening graph model (binja_d810.unflatten.graph) over one MLIL function and its SSA form (v4.2).

Blocks are the non-SSA basic blocks, by index. Values are SSA versions: lifted expressions have their
SSAVariable leaves relinked to Key(identifier, version), and every other leaf (a backing storage read, an
opaque or loaded value) stays unknown. Everything is built lazily and cached: planning reads only the blocks
and values its paths reach.
"""

from __future__ import annotations

from typing import Optional, Sequence

from binaryninja import MediumLevelILOperation as Op, SSAVariable, VariableSourceType

from ...ir.expr import BinOp, Const, Expr, Var, substitute, variables
from ...unflatten.graph import (Assign, Branch, Definition, Edge, Goto, Jump, Key, Load, Phi, Step, Stop, Undefined,
                                Unknown)
from ..effects import mlil_effect
from ..memory import fixed_load_is_safe
from ..mlil.lift import Backing, lift, lift_live, lift_value
from ..ssa import definition_of, reaching

_PHIS = (Op.MLIL_VAR_PHI, Op.MLIL_MEM_PHI)
_ASSIGNS = (Op.MLIL_SET_VAR_SSA, Op.MLIL_SET_VAR_SSA_FIELD)
_LOADS = (Op.MLIL_LOAD_SSA, Op.MLIL_LOAD_STRUCT_SSA)
MAX_COPIES = 8  # copies of a value searched for one live at a split


class MLILGraph:
    def __init__(self, mlil, *, view, ranges: Sequence[tuple[int, int]], max_nodes: int = 200,
                 passed: frozenset = frozenset()):
        """`passed`: parameters a loop's back edges assign (selfcall), whose entry value is the caller's."""
        self.mlil = mlil
        self.passed = passed
        self.ssa = mlil.ssa_form
        self.view = view
        self.ranges = list(ranges)
        self.max_nodes = max_nodes
        self._blocks = {b.index: b for b in mlil.basic_blocks}
        self._at = {b.start: b.index for b in mlil.basic_blocks}
        self.entry = mlil.get_basic_block_at(0).index
        self._defs: dict = {}  # the lifter's definition cache, shared by every lift of this SSA form
        self._vars: dict[Key, SSAVariable] = {}
        self._names: dict[int, str] = {}
        self._ssa_blocks: dict[int, object] = {}
        self._from_ssa: Optional[dict[int, int]] = None
        self._phis: dict[int, dict[Key, object]] = {}
        self._sources: dict[tuple[int, int, Key], Optional[Key]] = {}
        self._steps: dict[int, tuple[Step, ...]] = {}
        self._terms: dict[int, object] = {}
        self._definitions: dict[Key, Definition] = {}
        self._widths: dict[int, int] = {}
        self._preds: dict[int, list[Edge]] = {b: [] for b in self._blocks}
        for b, block in self._blocks.items():
            for target, slot in self._edges(block):
                self._preds[target].append(Edge(b, slot))

    # --- Graph protocol ---

    def blocks(self) -> list[int]:
        return sorted(self._blocks)

    def preds(self, block: int) -> list[Edge]:
        return self._preds[block]

    def phis(self, block: int) -> list[Key]:
        return list(self._phi_instrs(block))

    def phi_source(self, block: int, pred: int, dest: Key) -> Optional[Key]:
        cache = (block, pred, dest)
        if cache not in self._sources:
            phi = self._phi_instrs(block).get(dest)
            found = None if phi is None else reaching(self.ssa, self._ssa_block(pred), phi.dest.var)
            key = None if found is None else self._key(found)
            self._sources[cache] = key if phi is not None and key in {self._key(s) for s in phi.src} else None
        return self._sources[cache]

    def steps(self, block: int) -> tuple[Step, ...]:
        if block not in self._steps:
            b = self._blocks[block]
            self._steps[block] = tuple(self._step(self.mlil[i].ssa_form) for i in range(b.start, b.end - 1))
        return self._steps[block]

    def terminator(self, block: int):
        if block not in self._terms:
            self._terms[block] = self._terminator(self._blocks[block])
        return self._terms[block]

    def definition(self, key: Key) -> Definition:
        if key not in self._definitions:
            self._definitions[key] = self._definition(key)
        return self._definitions[key]

    def def_block(self, key: Key) -> Optional[int]:
        d = definition_of(self.ssa, self._vars[key], self._defs)
        return None if d is None else self.block_of(d)

    def block_of(self, ssa_instr) -> Optional[int]:
        """The block holding an instruction of the SSA form."""
        if self._from_ssa is None:
            self._from_ssa = {self._ssa_block(b).start: b for b in self._blocks}
        return self._from_ssa.get(ssa_instr.il_basic_block.start)

    def width(self, key: Key) -> int:
        return self._bits(self._vars[key].var)

    def read(self, address: int, width: int) -> Optional[int]:
        """Constant memory: a load fixed_load_is_safe allows, in a segment that is not writable or inside one
        of the configured read-only ranges."""
        size = width // 8
        if width % 8 or not fixed_load_is_safe(self.view, address, size):
            return None
        segment = self.view.get_segment_at(address)
        constant = segment is not None and not segment.writable and address + size <= segment.end
        if not constant and not any(start <= address and address + size <= end for start, end in self.ranges):
            return None
        try:
            return self.view.read_int(address, size, False)
        except ValueError:
            return None

    def live_expression(self, pred: int, key: Key) -> Optional[Expr]:
        """The value of `key` in values live at the end of `pred`: rebuilt from them, or (v4.3) a full-width copy
        of it that is still live there (a value saved across a call: the register it was made in is clobbered)."""
        b = self._blocks[pred]
        root = self.ssa[self.mlil.get_ssa_instruction_index(b.end - 1)]
        found = lift_live(self.ssa, self._vars[key], self.width(key), root_instr=root, max_nodes=self.max_nodes,
                          definitions=self._defs)
        if found is not None:
            return self._relink(found)
        copy = self._live_copy(self._vars[key], root)
        return None if copy is None else Var(f"{copy.var.name}#{copy.version}", self.width(key), ref=self._key(copy))

    def _live_copy(self, ssa_var: SSAVariable, root) -> Optional[SSAVariable]:
        width, work, seen = self._bits(ssa_var.var), [ssa_var], {ssa_var}
        while work and len(seen) <= MAX_COPIES:
            for use in self.ssa.get_ssa_var_uses(work.pop(0)):
                if use.operation != Op.MLIL_SET_VAR_SSA or use.src.operation != Op.MLIL_VAR_SSA:
                    continue
                dest = use.dest
                if dest in seen or self._bits(dest.var) != width:
                    continue
                if root.get_ssa_var_version(dest.var) == dest.version:
                    return dest
                seen.add(dest)
                work.append(dest)
        return None

    # --- for the rewrite ---

    def il_block(self, block: int):
        return self._blocks[block]

    def ssa_var(self, key: Key) -> SSAVariable:
        return self._vars[key]

    def name(self, var_id: int) -> str:
        return self._names[var_id]

    def with_refs(self, e: Expr) -> Expr:
        """`e` with Key refs back to SSA variables, for mlil.lower."""
        mapping = {leaf.name: Var(leaf.name, leaf.width, ref=self._vars[leaf.ref])
                   for leaf in variables(e) if isinstance(leaf.ref, Key)}
        return substitute(e, mapping) if mapping else e

    # --- building ---

    def _key(self, ssa_var: SSAVariable) -> Key:
        key = Key(ssa_var.var.identifier, ssa_var.version)
        self._vars.setdefault(key, ssa_var)
        self._names.setdefault(key.var, ssa_var.var.name)
        return key

    def _relink(self, e: Expr) -> Expr:
        mapping = {leaf.name: Var(leaf.name, leaf.width, ref=self._key(ref))
                   for leaf in variables(e) if (ref := self._whole(leaf)) is not None}
        return substitute(e, mapping) if mapping else e

    def _whole(self, leaf: Var) -> Optional[SSAVariable]:
        """The SSA variable a leaf reads in full: a variable, or the storage backing exactly one (a 64-bit state
        tested through its low half reads its register's backing value). Versions count per storage, so the
        backing's version is the variable's."""
        if isinstance(leaf.ref, SSAVariable):
            return leaf.ref
        if isinstance(leaf.ref, Backing) and 8 * leaf.ref.size == self._bits(leaf.ref.var):
            return SSAVariable(leaf.ref.var, leaf.ref.version)
        return None

    def _bits(self, var) -> int:
        """A variable's width: its type's, or for one the function's analysis has not typed (copied in from a
        spliced handler) the width its assignments write."""
        found = _bits(var)
        if found:
            return found
        if var.identifier not in self._widths:
            sizes = {d.size for d in self.mlil.get_var_definitions(var) if d.operation == Op.MLIL_SET_VAR}
            self._widths[var.identifier] = 8 * sizes.pop() if len(sizes) == 1 else 0
        return self._widths[var.identifier]

    def _lift(self, e, root) -> Optional[Expr]:
        found = lift(e, root_instr=root, check_versions=False, max_nodes=self.max_nodes, definitions=self._defs,
                     dataflow=False)
        return None if found is None else self._relink(found)

    def _ssa_block(self, block: int):
        if block not in self._ssa_blocks:
            start = self.mlil.get_ssa_instruction_index(self._blocks[block].start)
            self._ssa_blocks[block] = self.ssa[start].il_basic_block
        return self._ssa_blocks[block]

    def _phi_instrs(self, block: int) -> dict[Key, object]:
        if block not in self._phis:
            sblock, found = self._ssa_block(block), {}
            for i in range(sblock.start, sblock.end):
                instr = self.ssa[i]
                if instr.operation not in _PHIS:
                    break
                if instr.operation == Op.MLIL_VAR_PHI:
                    found[self._key(instr.dest)] = instr
            self._phis[block] = found
        return self._phis[block]

    def _edges(self, block) -> list[tuple[int, str]]:
        last = self.mlil[block.end - 1]
        if last.operation == Op.MLIL_GOTO:
            return [(self._at[last.dest], "goto")]
        if last.operation == Op.MLIL_IF:
            return [(self._at[last.true], "true"), (self._at[last.false], "false")]
        if last.operation == Op.MLIL_JUMP_TO:
            target = self._constant_jump(last)
            if target is not None:
                return [(target, "goto")]
            return [(b, "jump") for b in dict.fromkeys(self._at[i] for i in last.targets.values())]
        # Anything else that still has successors (a fall-through): an undecided jump, never rewritten.
        return [(b, "jump") for b in dict.fromkeys(e.target.index for e in block.outgoing_edges)]

    def _constant_jump(self, instr) -> Optional[int]:
        """A resolved jump with a literal destination matching its sole target is an ordinary goto.
        A target annotation alone does not establish the destination or make evaluating it effect-free."""
        if instr.dest.operation not in (Op.MLIL_CONST, Op.MLIL_CONST_PTR) or len(instr.targets) != 1:
            return None
        target = instr.targets.get(instr.dest.constant)
        return None if target is None else self._at.get(target)

    def _step(self, si) -> Step:
        if si.operation == Op.MLIL_NOP:
            return Step(None, None, (), False)
        if si.operation in _ASSIGNS and mlil_effect(si.src) is None:
            key = self._key(si.dest)
            return Step(key, self._value(si.dest), (key,), False)
        writes = tuple(self._key(v) for v in si.vars_written if isinstance(v, SSAVariable))
        return Step(None, None, writes, True)

    def _value(self, ssa_var: SSAVariable) -> Optional[Expr]:
        """What a pure definition computes, its own definitions inlined; None when nothing but the value
        itself comes out."""
        width = self._bits(ssa_var.var)
        d = definition_of(self.ssa, ssa_var, self._defs)
        if not width or d is None:
            return None
        found = lift_value(self.ssa, ssa_var, width, root_instr=d, max_nodes=self.max_nodes, definitions=self._defs)
        if isinstance(found, Var) and isinstance(found.ref, SSAVariable) and found.ref == ssa_var:
            return None
        return self._relink(found)

    def _local(self, ssa_var: SSAVariable) -> bool:
        """A stack slot below the frame that is not a parameter: its entry version is read before any store. Or a
        parameter in `passed`: its entry version is the first state, which the caller picks, as a flattened
        function's first store would; it does not count against a state the loop only assigns constants."""
        var = ssa_var.var
        if ssa_var.version == 0 and var in self.passed:
            return True
        if ssa_var.version != 0 or var.source_type != VariableSourceType.StackVariableSourceType or var.storage >= 0:
            return False
        return var not in self.mlil.source_function.parameter_vars

    def _definition(self, key: Key) -> Definition:
        ssa_var = self._vars[key]
        d = definition_of(self.ssa, ssa_var, self._defs)
        if d is None:
            return Undefined() if self._local(ssa_var) else Unknown()
        if d.operation == Op.MLIL_VAR_PHI:
            return Phi(tuple(self._key(s) for s in d.src))
        if d.operation in _ASSIGNS and mlil_effect(d.src) is None:
            e = self._value(ssa_var)
            if e is not None:
                return Assign(e)
        return Unknown()

    def _terminator(self, block):
        last = self.mlil[block.end - 1]
        si = last.ssa_form
        if last.operation == Op.MLIL_GOTO:
            return Goto(self._at[last.dest])
        if last.operation == Op.MLIL_IF:
            return Branch(self._condition(si), self._at[last.true], self._at[last.false])
        if last.operation == Op.MLIL_JUMP_TO:
            target = self._constant_jump(last)
            if target is not None:
                return Goto(target)
            return self._jump(si, {address: self._at[i] for address, i in last.targets.items()})
        edges = self._edges(block)
        return Jump(None, {}, {i: b for i, (b, _) in enumerate(edges)}) if edges else Stop()

    def _condition(self, si) -> Optional[Expr]:
        if si.condition.operation in (Op.MLIL_CONST, Op.MLIL_CONST_PTR):
            return Const(1 if si.condition.constant else 0, 1)  # if (false): folded already, 0 bits wide
        return self._lift(si.condition, si)

    def _jump(self, si, targets: dict[int, int]) -> Jump:
        """The destination with each load a leaf (lift's load_leaves), and each load's address lifted on its own."""
        dest = lift(si.dest, root_instr=si, check_versions=False, max_nodes=self.max_nodes, definitions=self._defs,
                    load_leaves=True, dataflow=False)
        if dest is None:
            return Jump(None, {}, targets)
        loads = {}
        for leaf in variables(dest):
            if leaf.ref is None or getattr(leaf.ref, "operation", None) not in _LOADS:
                continue
            address = self._lift(leaf.ref.src, si)
            if address is None:
                return Jump(None, {}, targets)
            if leaf.ref.operation == Op.MLIL_LOAD_STRUCT_SSA and leaf.ref.offset:
                address = BinOp("add", address, Const(leaf.ref.offset, address.width), address.width)
            loads[leaf.name] = Load(address, leaf.width)
        return Jump(self._relink(dest), loads, targets)


def _bits(var) -> int:
    try:
        return 8 * var.type.width if var.type is not None else 0
    except Exception:  # noqa: BLE001 - a variable without a usable type
        return 0
