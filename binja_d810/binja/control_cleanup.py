"""Prove equivalent scalar branches in structured HLIL; retain all unknown effects."""
from dataclasses import dataclass, field
import time
from binaryninja import HighLevelILOperation as H, ILInstructionAttribute as Attr
from ..control.equivalence import Assignment, equivalent
from ..ir.expr import BinOp, Const, UnOp, Var, tree_size
from ..ir.normalize import normalize
from ..semantic.constant_guard import prove as constant_guard
from .hlil.adapter import HLILAdapter, shallow_copy
from .hlil.lift import _BINARY, _UNARY, _COMPARE, STRUCTURE, children

_PROTECTED = {Attr.ILPreventDeadStoreElimination, Attr.ILPreventAliasAnalysis}


@dataclass(frozen=True)
class Limits:
    equivalents: bool = True
    constants: bool = True
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
    constants: int = 0
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
        else:
            # Conditional values directly assigned/returned by a statement are not visible
            # to the normal structural walk; inspect these roots without expanding arithmetic.
            expressions = ([node.src] if node.operation in (H.HLIL_VAR_INIT,H.HLIL_ASSIGN) else
                           list(node.src) if node.operation==H.HLIL_RET else [])
            stack.extend(e for e in expressions if e.operation==H.HLIL_IF)


def tree_nodes(root, tick):
    """Use-region membership requires a tree, not expression indices shared between regions."""
    seen, result = set(), []
    for node in walk(root, tick):
        if node.expr_index in seen:
            raise ValueError("shared HLIL expression indices are unsupported")
        seen.add(node.expr_index)
        result.append(node)
    return result


def protected_nodes(root,tick):
    """Propagate protected attributes along reachable edges, not stale SDK parents."""
    result=set();stack=[(root,False)]
    while stack:
        tick();node,inherited=stack.pop()
        protected=inherited or bool(node.attributes & _PROTECTED)
        if protected:result.add(node.expr_index)
        stack.extend((child,protected) for child in children(node))
    return result


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
        # Classify operand roles from reachable edges. The SDK's single parent field can
        # refer to an older, now-unreachable copy after a structural replacement.
        operands = [node.src] if node.operation == H.HLIL_ASSIGN and node.dest.operation == H.HLIL_VAR else children(node)
        for operand in operands:
            if operand.operation == H.HLIL_VAR:
                out.setdefault(operand.var.identifier, set()).add(operand.expr_index)
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
            nodes = tree_nodes(il.root, work.tick)
            protected = protected_nodes(il.root,work.tick)
            uses = reads(nodes, work.tick)
            plans = [];covered=set()
            for root in nodes:
                if root.operation != H.HLIL_IF or root.expr_index in covered:
                    continue
                if root.expr_index in protected:
                    report.notes.append(f'{root.address:#x}: protected branch kept')
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
                    constant=normalize(guard)
                    if limits.constants and isinstance(constant,Const):
                        region=list(walk(root,work.tick))
                        rejected=root.false if constant.value else root.true
                        if any(node.operation==H.HLIL_LABEL for node in region):
                            raise ValueError('labelled constant branch kept')
                        if any(node.attributes & _PROTECTED for node in walk(rejected,work.tick)):
                            raise ValueError('protected untaken branch kept')
                        if constant_guard(guard,proof_ms=limits.proof_ms,deadline=deadline) is not None:
                            plans.append((root,root.true if constant.value else root.false,'constant'))
                            covered.update(node.expr_index for node in region)
                        else:report.notes.append(f'{root.address:#x}: constant guard not proven')
                        continue
                    if not limits.equivalents:continue
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
                        plans.append((root,root.true,'equivalent'))
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
            for root,selected,_ in plans:
                adapter.backups.append((root.expr_index, shallow_copy(il, root)))
                il.replace_expr(root.expr_index, shallow_copy(il, selected))
            adapter.finish()
            committing = False
            report.branches += sum(kind=='equivalent' for _,_,kind in plans)
            report.constants += sum(kind=='constant' for _,_,kind in plans)
            report.notes.extend(f"{root.address:#x}: " + ('proved constant guard; original selected arm retained' if kind=='constant'
                                else 'both scalar branches proved equivalent; pure guard removed') for root,_,kind in plans)
            if report.exhausted:
                break
    except (ValueError, RecursionError) as exc:
        if committing:
            adapter.restore()
            report.branches = report.constants = 0
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
