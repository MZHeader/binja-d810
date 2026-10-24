"""Prove equivalent scalar branches in structured HLIL; retain all unknown effects."""
from dataclasses import dataclass, field
import time
from binaryninja import HighLevelILOperation as H, ILInstructionAttribute as Attr
from ..control.equivalence import Assignment, equivalent
from ..ir.expr import BinOp, Const, UnOp, Var, tree_size
from .hlil.adapter import HLILAdapter, shallow_copy
from .hlil.lift import _BINARY, _UNARY, _COMPARE, STRUCTURE, children

_PROTECTED = {Attr.ILPreventDeadStoreElimination, Attr.ILPreventAliasAnalysis}


@dataclass(frozen=True)
class Limits:
    instructions: int = 20000
    nodes: int = 2000
    work: int = 80000
    branches: int = 64
    rounds: int = 3
    bits: int = 64
    proof_ms: int = 200


@dataclass
class Report:
    branches: int = 0
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    exhausted: bool = False


class BudgetExceeded(ValueError):
    pass


class Work:
    def __init__(self, limits, deadline):
        self.left, self.deadline = limits.work, deadline

    def tick(self):
        self.left -= 1
        if self.left < 0 or self.deadline.expired():
            raise BudgetExceeded("control cleanup budget exhausted")


def walk(root, tick):
    stack = [root]
    while stack:
        tick()
        node = stack.pop()
        yield node
        stack.extend(reversed(list(children(node))))


def structure(root, tick):
    """Inspect control structure without walking scalar expression trees."""
    stack = [root]
    while stack:
        tick()
        node = stack.pop()
        if node.operation in STRUCTURE:
            yield node
            stack.extend(reversed(list(children(node))))


class Scalar:
    def __init__(self, il, limits, work):
        self.aliased = frozenset(il.aliased_vars)
        self.limits, self.work = limits, work
        self.variables = {}

    def variable(self, var, fallback):
        if var in self.aliased or var.type is not None and var.type.volatile:
            raise ValueError("aliased or volatile scalar")
        width = var.type.width * 8 if var.type is not None and var.type.width else fallback
        if not 1 <= width <= self.limits.bits:
            raise ValueError("scalar width limit")
        found = self.variables.setdefault(var.identifier, Var(f"control@{var.identifier}", width))
        if found.width != width:
            raise ValueError("ambiguous scalar width")
        return found

    def check(self, node):
        self.work.tick()
        if node.attributes & _PROTECTED:
            raise ValueError("protected control operation")

    def expression(self, node):
        self.check(node)
        op, width = node.operation, node.size * 8 or 1
        if width > self.limits.bits:
            raise ValueError("scalar width limit")
        if op in (H.HLIL_CONST, H.HLIL_CONST_PTR):
            return Const(node.constant, width)
        if op == H.HLIL_VAR:
            value = self.variable(node.var, width)
            if width > value.width:
                raise ValueError("read beyond scalar width")
            return value if width == value.width else UnOp("trunc", value, width)
        if op in _BINARY or op in _COMPARE:
            a, b = self.expression(node.left), self.expression(node.right)
            return BinOp(_COMPARE[op], a, b, 1) if op in _COMPARE else BinOp(_BINARY[op], a, b, width)
        if op in _UNARY or op == H.HLIL_BOOL_TO_INT:
            value = self.expression(node.src)
            name = "zx" if op == H.HLIL_BOOL_TO_INT else _UNARY[op]
            if name in ("zx", "sx", "trunc") and width == value.width:
                return value
            return UnOp(name, value, width)
        raise ValueError(f"effect or unsupported scalar: {op.name}")

    def block(self, node):
        self.check(node)
        if node.operation == H.HLIL_BLOCK:
            return [assignment for child in node.body for assignment in self.block(child)]
        if node.operation in (H.HLIL_NOP, H.HLIL_VAR_DECLARE):
            return []
        if node.operation == H.HLIL_VAR_INIT:
            target = self.variable(node.dest, node.size * 8 or 1)
        elif node.operation == H.HLIL_ASSIGN and node.dest.operation == H.HLIL_VAR:
            self.check(node.dest)
            target = self.variable(node.dest.var, node.dest.size * 8 or 1)
        else:
            raise ValueError(f"non-scalar branch statement: {node.operation.name}")
        value = self.expression(node.src)
        if value.width == 1 and target.width > 1:
            value = UnOp("zx", value, target.width)
        if value.width != target.width:
            raise ValueError("partial or ambiguous scalar assignment")
        if tree_size(value) > self.limits.nodes:
            raise ValueError("branch expression limit")
        return [Assignment(target, value)]


def reads(nodes, tick):
    out = {}
    for node in nodes:
        tick()
        if node.operation != H.HLIL_VAR:
            continue
        parent = node.parent
        if parent is not None and parent.operation == H.HLIL_ASSIGN and parent.dest.expr_index == node.expr_index:
            continue
        out.setdefault(node.var.identifier, set()).add(node.expr_index)
    return out


def run(il, limits, deadline):
    report = Report()
    started, cpu = time.monotonic(), time.thread_time()
    adapter = HLILAdapter(il, limits.nodes, deadline)
    work = Work(limits, deadline)
    remaining = limits.branches
    committing = False
    try:
        if len(il) > limits.instructions:
            raise ValueError("control instruction limit")
        for _ in range(limits.rounds):
            if not any(node.operation == H.HLIL_IF for node in structure(il.root, work.tick)):
                break
            nodes = list(walk(il.root, work.tick))
            uses = reads(nodes, work.tick)
            plans = []
            for root in nodes:
                if root.operation != H.HLIL_IF:
                    continue
                if remaining <= 0:
                    report.notes.append("control branch limit")
                    break
                remaining -= 1
                try:
                    scalar = Scalar(il, limits, work)
                    scalar.check(root)
                    guard = scalar.expression(root.condition)
                    if guard.width != 1:
                        raise ValueError("non-Boolean branch guard")
                    if tree_size(guard) > limits.nodes:
                        raise ValueError("branch expression limit")
                    left, right = scalar.block(root.true), scalar.block(root.false)
                    region = {n.expr_index for n in walk(root, work.tick)}
                    written = {a.target for a in left + right}
                    outputs = []
                    for key, var in scalar.variables.items():
                        work.tick()
                        if var in written and uses.get(key, set()) - region:
                            outputs.append(var)
                    proven, reason = equivalent(left, right, outputs, max_nodes=limits.nodes,
                                                proof_ms=limits.proof_ms, deadline=deadline, tick=work.tick)
                    if proven:
                        plans.append(root)
                    else:
                        report.notes.append(f"{root.address:#x}: {reason}")
                except (ValueError, RecursionError) as exc:
                    report.notes.append(f"{root.address:#x}: {exc}")
                    if work.left < 0 or deadline.expired():
                        report.exhausted = True
                        break
            if not plans:
                break
            committing = True
            for root in plans:
                adapter.backups.append((root.expr_index, shallow_copy(il, root)))
                il.replace_expr(root.expr_index, shallow_copy(il, root.true))
            adapter.finish()
            committing = False
            report.branches += len(plans)
            report.notes.extend(f"{root.address:#x}: both scalar branches proved equivalent; pure guard removed" for root in plans)
            if report.exhausted:
                break
    except (ValueError, RecursionError) as exc:
        if committing:
            adapter.restore()
            report.branches = 0
            report.notes.append("control changes rolled back")
        report.notes.append(str(exc))
        report.exhausted |= work.left < 0 or deadline.expired()
    except Exception:
        adapter.restore()
        raise
    finally:
        report.seconds = time.monotonic() - started
        report.cpu_seconds = time.thread_time() - cpu
    return report
