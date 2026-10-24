"""Remove unobserved, effect-free scalar loops only after proving bounded termination."""
from dataclasses import dataclass, field
import time
from binaryninja import HighLevelILOperation as H
from ..control.equivalence import Assignment
from ..control.finite import Break, Continue, Decision, Havoc, Loop, abstract_value, terminates
from .control_cleanup import BudgetExceeded, Scalar, Work, walk, reads, structure
from .hlil.adapter import HLILAdapter, shallow_copy

_LOOPS = {H.HLIL_WHILE, H.HLIL_DO_WHILE, H.HLIL_FOR}


@dataclass(frozen=True)
class Limits:
    instructions: int = 20000
    nodes: int = 2000
    work: int = 80000
    loops: int = 64
    iterations: int = 64
    rounds: int = 3
    bits: int = 64


@dataclass
class Report:
    loops: int = 0
    iterations: int = 0
    computations: int = 0
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    exhausted: bool = False


class Model(Scalar):
    def statements(self, node):
        self.check(node)
        op = node.operation
        if op == H.HLIL_BLOCK:
            return tuple(statement for child in node.body for statement in self.statements(child))
        if op == H.HLIL_IF:
            return (Decision(self.expression(node.condition), self.statements(node.true), self.statements(node.false)),)
        if op == H.HLIL_BREAK:
            return (Break(),)
        if op == H.HLIL_CONTINUE:
            return (Continue(),)
        if op == H.HLIL_VAR_DECLARE:
            return (Havoc(self.variable(node.var, node.size * 8 or 1)),)
        return tuple(self.block(node))

    def loop(self, node):
        self.check(node)
        condition = self.expression(node.condition)
        if condition.width != 1:
            raise ValueError("non-Boolean loop guard")
        return Loop(condition, self.statements(node.body),
                    self.statements(node.init) if node.operation == H.HLIL_FOR else (),
                    self.statements(node.update) if node.operation == H.HLIL_FOR else (),
                    node.operation == H.HLIL_DO_WHILE)


def writes(statements, tick):
    out = set()
    pending = list(statements)
    while pending:
        tick()
        statement = pending.pop()
        if isinstance(statement, (Assignment, Havoc)):
            out.add(statement.target)
        elif isinstance(statement, Decision):
            pending.extend(statement.yes + statement.no)
    return out


def entries(nodes, model):
    """Constants established by the straight-line prefix in the same enclosing block.

    Unknown control flow/effects discard the prefix facts. No value is inferred from a load,
    an alias, a predecessor outside the block or a guard's assumed outcome.
    """
    result = {}
    for block in nodes:
        model.work.tick()
        if block.operation != H.HLIL_BLOCK:
            continue
        state = {}
        for child in block.body:
            model.work.tick()
            if child.operation in _LOOPS:
                result[child.expr_index] = dict(state)
            try:
                if child.operation == H.HLIL_VAR_DECLARE:
                    state[model.variable(child.var, child.size * 8 or 1).name] = None
                else:
                    for assignment in model.block(child):
                        state[assignment.target.name] = abstract_value(assignment.value, state,
                                                                       max_nodes=model.limits.nodes, tick=model.work.tick)
            except (ValueError, RecursionError):
                state.clear()
                if model.work.left < 0 or model.work.deadline.expired():
                    raise
    return result


def remove_orphans(affected, adapter, model, report):
    """Delete only pure block statements feeding removed loops, after all their reads disappear.

    A declaration is not a data use. Assignment expressions embedded in another expression
    are excluded: their resulting value can be observed without reading the destination.
    """
    removed = 0
    try:
        for _ in range(model.limits.rounds):
            nodes = list(walk(adapter.hlil.root, model.work.tick))
            uses = reads(nodes, model.work.tick)
            plans = []
            for node in nodes:
                model.work.tick()
                if node.parent is None or node.parent.operation != H.HLIL_BLOCK:
                    continue
                if node.operation == H.HLIL_VAR_DECLARE:
                    target = node.var
                elif node.operation == H.HLIL_VAR_INIT:
                    target = node.dest
                elif node.operation == H.HLIL_ASSIGN and node.dest.operation == H.HLIL_VAR:
                    target = node.dest.var
                else:
                    continue
                if target.identifier not in affected or uses.get(target.identifier):
                    continue
                try:
                    model.check(node)
                    model.variable(target, node.size * 8 or 1)
                    dependencies = set()
                    if node.operation != H.HLIL_VAR_DECLARE:
                        model.block(node)  # every source operation must be pure and nontrapping
                        dependencies = {n.var.identifier for n in walk(node.src, model.work.tick) if n.operation == H.HLIL_VAR}
                    plans.append((node, dependencies))
                except BudgetExceeded:
                    raise
                except (ValueError, RecursionError):
                    continue
            if not plans:
                break
            for node, dependencies in plans:
                adapter.backups.append((node.expr_index, shallow_copy(adapter.hlil, node)))
                adapter.hlil.replace_expr(node.expr_index, adapter.hlil.block([]))
                affected.update(dependencies)
            adapter.finish()
            removed += len(plans)
    except BudgetExceeded:
        report.exhausted = True
        report.notes.append("orphan cleanup budget exhausted; proven loop removals retained")
    return removed


def run(il, limits, deadline):
    report = Report()
    started, cpu = time.monotonic(), time.thread_time()
    adapter = HLILAdapter(il, limits.nodes, deadline)
    work = Work(limits, deadline)
    remaining, committing = limits.loops, False
    try:
        if len(il) > limits.instructions:
            raise ValueError("dead-loop instruction limit")
        for _ in range(limits.rounds):
            if not any(node.operation in _LOOPS for node in structure(il.root, work.tick)):
                break
            nodes = list(walk(il.root, work.tick))
            uses = reads(nodes, work.tick)
            model = Model(il, limits, work)
            initial = entries(nodes, model)
            plans = []
            for root in nodes:
                if root.operation not in _LOOPS:
                    continue
                if remaining <= 0:
                    report.notes.append("dead-loop candidate limit")
                    break
                remaining -= 1
                try:
                    loop = model.loop(root)
                    changed = writes(loop.init + loop.body + loop.update, work.tick)
                    region_nodes = list(walk(root, work.tick))
                    region = {n.expr_index for n in region_nodes}
                    affected = {n.var.identifier for n in region_nodes if n.operation == H.HLIL_VAR}
                    for key, var in model.variables.items():
                        work.tick()
                        if var in changed:
                            affected.add(key)
                        if var in changed and uses.get(key, set()) - region:
                            raise ValueError(f"loop output {var.name} is read outside the loop")
                    count = terminates(loop, initial.get(root.expr_index, {}), iterations=limits.iterations,
                                       max_nodes=limits.nodes, tick=work.tick)
                    plans.append((root, count, affected))
                except (ValueError, RecursionError) as exc:
                    report.notes.append(f"{root.address:#x}: {exc}")
                    if work.left < 0 or deadline.expired():
                        report.exhausted = True
                        break
            if not plans:
                break
            affected = set().union(*(variables for _, _, variables in plans))
            committing = True
            for root, _, _ in plans:
                adapter.backups.append((root.expr_index, shallow_copy(il, root)))
                il.replace_expr(root.expr_index, il.block([]))
            adapter.finish()
            if not report.exhausted and not deadline.expired():
                removed = remove_orphans(affected, adapter, model, report)
                report.computations += removed
                if removed:
                    report.notes.append(f"removed {removed} unobserved scalar initializers, assignments and declarations")
            committing = False
            report.loops += len(plans)
            report.iterations += sum(count for _, count, _ in plans)
            report.notes.extend(f"{root.address:#x}: dead scalar loop proved to terminate after {count} iterations"
                                for root, count, _ in plans)
            if report.exhausted:
                break
    except (ValueError, RecursionError) as exc:
        if committing:
            adapter.restore()
            report.loops = report.iterations = report.computations = 0
            report.notes.append("dead-loop changes rolled back")
        report.notes.append(str(exc))
        report.exhausted |= work.left < 0 or deadline.expired()
    except Exception:
        adapter.restore()
        raise
    finally:
        report.seconds = time.monotonic() - started
        report.cpu_seconds = time.thread_time() - cpu
    return report
