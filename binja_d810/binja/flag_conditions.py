"""Boolean flag reconstruction from MLIL SSA and pure HLIL expressions."""

from dataclasses import dataclass, field
from types import SimpleNamespace
import time

from binaryninja import ILInstructionAttribute as Attr, MediumLevelILOperation as M, HighLevelILOperation as H

from ..flags.recombine import Limits, recombine
from ..flags.semantics import choose
from ..ir.bits import possible_ones
from ..ir.expr import BinOp, Const, UnOp, tree_size
from ..ir.normalize import normalize
from .mlil import lift as mlift
from .hlil import lift as hlift
from .mlil.adapter import MLILAdapter
from .hlil.adapter import HLILAdapter
from .ssa import reaching

_PROTECTED = {Attr.ILPreventDeadStoreElimination, Attr.ILPreventAliasAnalysis}


@dataclass
class Report:
    conditions: int = 0
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    exhausted: bool = False


class Work:
    def __init__(self, limits, deadline):
        self.left, self.deadline = limits.work, deadline

    def tick(self):
        self.left -= 1
        if self.left < 0 or self.deadline.expired():
            raise ValueError("flag proof budget exhausted")


class MLifter(mlift._Lifter):
    def __init__(self, il, root, limits, work):
        super().__init__(il.ssa_form, root, True, limits.nodes)
        self.limits, self.work = limits, work
        self.dataflow = False
        self.active, self.flag_values, self.guards = set(), set(), []
        self.rejections = set()

    def expr(self, e, *, inlined=False):
        self.work.tick()
        if e.attributes & _PROTECTED:
            raise ValueError("protected flag expression")
        op = e.operation
        if e.size == 0:
            self.budget -= 1
            self.visits += 1
            if self.budget < 0:
                return None
            if op == M.MLIL_CONST:
                return Const(e.constant, 1)
            if op == M.MLIL_VAR_SSA:
                return self.var(e.src, 1)
            if op == M.MLIL_NOT:
                value = self.expr(e.src, inlined=inlined)
                return None if value is None or value.width != 1 else UnOp("not", value, 1)
            if op in (M.MLIL_AND, M.MLIL_OR, M.MLIL_XOR):
                a, b = self.expr(e.left, inlined=inlined), self.expr(e.right, inlined=inlined)
                return None if a is None or b is None or a.width != 1 or b.width != 1 \
                    else BinOp(mlift._BINARY[op], a, b, 1)
        if op == M.MLIL_ADD_OVERFLOW:
            self.rejections.add("native overflow operation is not modeled")
            return None
        return super().expr(e, inlined=inlined)

    def boolean(self, e, *, inlined):
        value = self.expr(e, inlined=inlined)
        return value if value is not None and value.width == 1 else None

    def var(self, var, width):
        if var.var.type is not None and var.var.type.volatile:
            return None
        result = super().var(var, width)
        if result is not None and var.version and possible_ones(result) <= 1:
            self.flag_values.add(var)
        return result

    def definition(self, definition, var, width):
        if definition.operation == M.MLIL_SET_VAR_SSA and definition.src.operation == M.MLIL_ADD_OVERFLOW:
            self.rejections.add("native overflow operation is not modeled")
            return None
        if definition.operation == M.MLIL_SET_VAR_SSA and definition.src.size == 0:
            value = self.expr(definition.src, inlined=True)
            return value if value is None or width == 1 else UnOp("zx", value, width)
        if definition.operation == M.MLIL_VAR_PHI:
            return self.phi(definition, var, width)
        return super().definition(definition, var, width)

    def phi(self, definition, var, width):
        self.work.tick()
        if var in self.active or len(self.active) >= self.limits.merges:
            return None
        merge = definition.il_basic_block
        edges = merge.incoming_edges
        if len(edges) != 2:
            return None
        arms = [edge.source for edge in edges]
        heads = [a.incoming_edges[0].source if len(a.incoming_edges) == 1 else None for a in arms]
        head = heads[0] if heads[0] is not None and heads[0] == heads[1] else \
            arms[0] if heads[1] == arms[0] else arms[1] if heads[0] == arms[1] else None
        if head is None or head == merge:
            return None
        if any(a != head and (len(a.outgoing_edges) != 1 or a.outgoing_edges[0].target != merge)
               for a in arms):
            return None
        branch = self.ssa[head.end - 1]
        targets = {merge.start if a == head else a.start for a in arms}
        if branch.operation != M.MLIL_IF or {branch.true, branch.false} != targets:
            return None
        values = {merge.start if arm == head else arm.start: reaching(self.ssa, arm, var.var, depth=0) for arm in arms}
        # Phi source order is not an edge order. Require the actual reaching versions.
        if None in values.values() or set(values.values()) != set(definition.src):
            return None
        self.active.add(var)
        try:
            guard = self.expr(branch.condition, inlined=True)
            yes = self.var(values[branch.true], width)
            no = self.var(values[branch.false], width)
            if guard is None or guard.width != 1 or yes is None or no is None:
                return None
            if possible_ones(yes) > 1 or possible_ones(no) > 1:
                return None
            if guard not in self.guards:
                self.guards.append(guard)
            selected = choose(guard, yes if width == 1 else normalize(UnOp("trunc", yes, 1)),
                              no if width == 1 else normalize(UnOp("trunc", no, 1)))
            return selected if width == 1 else UnOp("zx", selected, width)
        finally:
            self.active.remove(var)


class HLifter(hlift._Lifter):
    def __init__(self, root, aliased, limits, work):
        super().__init__(root, aliased, True, limits.nodes, False)
        self.work, self.flag_values, self.guards = work, set(), []
        self.rejections = set()

    def expr(self, e, *, inlined=False):
        self.work.tick()
        if e.attributes & _PROTECTED:
            raise ValueError("protected flag expression")
        if e.size == 0:
            self.budget -= 1
            self.visits += 1
            if self.budget < 0:
                return None
            if e.operation == H.HLIL_CONST:
                return Const(e.constant, 1)
            if e.operation == H.HLIL_VAR_SSA:
                return self.var(e.var, 1)
            if e.operation == H.HLIL_NOT:
                value = self.expr(e.src, inlined=inlined)
                return None if value is None or value.width != 1 else UnOp("not", value, 1)
            if e.operation in (H.HLIL_AND, H.HLIL_OR, H.HLIL_XOR):
                a, b = self.expr(e.left, inlined=inlined), self.expr(e.right, inlined=inlined)
                return None if a is None or b is None or a.width != 1 or b.width != 1 \
                    else BinOp(hlift._BINARY[e.operation], a, b, 1)
        if e.operation == H.HLIL_ADD_OVERFLOW:
            self.rejections.add("native overflow operation is not modeled")
            return None
        return super().expr(e, inlined=inlined)

    def var(self, var, width):
        if var.var.type is not None and var.var.type.volatile:
            return None
        definition = self.definition_of(var)
        if definition is not None and hasattr(definition, "src") and definition.src.operation == H.HLIL_ADD_OVERFLOW:
            self.rejections.add("native overflow operation is not modeled")
        result = super().var(var, width)
        if result is not None and var.version and possible_ones(result) <= 1:
            self.flag_values.add(var)
        return result


def roots(il, level, work):
    comparisons = mlift._COMPARE if level == "mlil" else hlift._COMPARE
    pending = list(il.instructions) if level == "mlil" else [il.root]
    seen = set()
    while pending:
        work.tick()
        node = pending.pop()
        if node.expr_index in seen:
            continue
        seen.add(node.expr_index)
        name = node.operation.name
        if any(part in name for part in ("CALL", "STORE", "INTRINSIC")):
            continue  # Conditions exposed as external data do not authorize flag elimination.
        if name == "HLIL_ASSIGN" and node.dest.operation not in (H.HLIL_VAR, H.HLIL_STRUCT_FIELD):
            continue
        boolean = node.operation in comparisons or node.operation.name.endswith("BOOL_TO_INT") \
            or (node.size == 0 and name.rsplit("_", 1)[-1] in ("AND", "OR", "XOR", "NOT", "VAR"))
        if boolean:
            yield node
            continue
        for operand in node.operands:
            if hasattr(operand, "operation"):
                pending.append(operand)
            elif isinstance(operand, list):
                pending.extend(item for item in operand if hasattr(item, "operation"))


def run(il, level, limits, deadline):
    started, cpu = time.monotonic(), time.thread_time()
    report, work, plans = Report(), Work(limits, deadline), []
    adapter = MLILAdapter(il, limits.nodes, deadline) if level == "mlil" else HLILAdapter(il, limits.nodes, deadline)
    try:
        if len(il) > limits.instructions:
            raise ValueError("flag instruction limit")
        for index, root in enumerate(roots(il, level, work)):
            if index >= limits.conditions:
                report.notes.append("flag condition limit")
                break
            lifter = MLifter(il, il[root.instr_index], limits, work) if level == "mlil" \
                else HLifter(root.ssa_form, adapter.aliased, limits, work)
            source = lifter.expr(root.ssa_form)
            report.notes.extend(f"{root.address:#x}: {reason}" for reason in sorted(lifter.rejections))
            if level == "mlil" and source is not None:
                source = lifter.relate(source)
            if source is None or possible_ones(source) > 1:
                report.notes.append(f"{root.address:#x}: flags have unmodeled or stale inputs")
                continue
            simple = source.operand if isinstance(source, UnOp) and source.op == "zx" else source
            zero_test = (isinstance(simple, BinOp) and simple.op in ("eq", "ne")
                         and simple.right == Const(0, simple.left.width)
                         and isinstance(simple.left, BinOp) and simple.left.op == "sub")
            combined = isinstance(simple, BinOp) and (simple.op in ("and", "or", "xor") or simple.left.width == 1)
            if not lifter.flag_values and not lifter.guards and not zero_test and not combined:
                continue  # Ordinary comparisons need no speculative flag proofs.
            boolean = source if source.width == 1 else normalize(UnOp("trunc", source, 1))
            found = recombine(boolean, guards=tuple(lifter.guards), limits=limits, deadline=deadline, tick=work.tick)
            if source.width != 1:
                found.expression = UnOp("zx", found.expression, source.width)
                from ..semantic.verify import Verdict, check
                if check(source, found.expression, z3_timeout_ms=limits.proof_ms, deadline=deadline).verdict is not Verdict.PROVEN:
                    report.notes.append(f"{root.address:#x}: boolean-width reconstruction was not proven")
                    continue
            report.notes.extend(f"{root.address:#x}: {n}" for n in found.notes)
            if found.expression == source or tree_size(found.expression) >= tree_size(source):
                if lifter.flag_values or lifter.guards:
                    report.notes.append(f"{root.address:#x}: no smaller comparison proven for these flag values")
                continue
            plans.append((root, found.expression, lifter))
        # Every flag value in a plan must feed only the jointly rewritten conditions and
        # definitions in their expression slices. Other data/side-effect uses veto that plan.
        while plans:
            work.tick()
            allowed = {root.ssa_form.instr_index for root, _, _ in plans}
            for _, _, lifter in plans:
                for value in lifter.inlined.values():
                    work.tick()
                    definition = il.ssa_form.get_ssa_var_definition(value)
                    if definition is not None:
                        allowed.add(definition.instr_index)
            accepted = []
            for root, replacement, lifter in plans:
                external = False
                for v in lifter.flag_values:
                    for use in il.ssa_form.get_ssa_var_uses(v):
                        work.tick()
                        external |= use.instr_index not in allowed
                if external:
                    report.notes.append(f"{root.address:#x}: flag value has other uses")
                else:
                    accepted.append((root, replacement, lifter))
            if len(accepted) == len(plans):
                break
            plans = accepted
        accepted = [(root, replacement) for root, replacement, _ in plans]
        if accepted and not deadline.expired():
            dead_before = adapter.dead_assignments()
            for root, replacement in accepted:
                adapter.replace(SimpleNamespace(root=root), replacement)
            adapter.finish()
            newly_dead = adapter.dead_assignments() - dead_before
            if newly_dead:
                adapter.remove(newly_dead)
                adapter.finish()
            report.conditions = len(accepted)
            report.notes.insert(0, f"{len(accepted)} conditions recombined with exact bit-vector proofs")
    except (ValueError, NotImplementedError, RecursionError) as exc:
        adapter.restore()
        report.notes.append(f"left unchanged: {exc}")
    except Exception:
        adapter.restore()
        raise
    finally:
        report.seconds, report.cpu_seconds = time.monotonic() - started, time.thread_time() - cpu
        report.exhausted = deadline.expired()
    return report
