"""Bounded domains for private mutable globals; every read is an independent observation."""

from dataclasses import dataclass, field
from itertools import islice
import time
from binaryninja import (ILInstructionAttribute as Attr, ILSourceLocation, MediumLevelILFunction,
                         MediumLevelILOperation as M, SymbolBinding)

from ..deadline import Deadline
from ..ir.expr import BinOp, Const, Var, variables
from ..ir.evaluate import evaluate
from ..ir.normalize import normalize
from ..value_sets.closure import Limits as ClosureLimits, Update, closure, embed_update
from ..value_sets.proof import check as constrained
from ..semantic.verify import Verdict, check
from .flow import evidence
from .mlil import lift as lifting
from .scratch import operands, walk
from .memory import fixed_load_is_safe


@dataclass(frozen=True)
class Limits:
    instructions: int = 20000
    work: int = 40000
    expressions: int = 2000
    globals: int = 32
    readers: int = 256
    values: int = 64
    writes: int = 1024
    closure_steps: int = 4096
    bits: int = 64
    proof_ms: int = 200
    candidates: int = 128
    evidence_bytes: int = 1048576


@dataclass
class Report:
    folded: int = 0
    domains: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    exhausted: bool = False
    evidence: tuple = ()
    deferred: bool = False


@dataclass(frozen=True)
class Domain:
    address: int
    size: int
    values: frozenset
    writes: tuple


_LOADS = {M.MLIL_LOAD_SSA, M.MLIL_LOAD_STRUCT_SSA}
_READS = {M.MLIL_LOAD, M.MLIL_LOAD_STRUCT}
_PROTECTED = {Attr.ILPreventDeadStoreElimination, Attr.ILPreventAliasAnalysis}


class InitialAnalysisPending(ValueError):
    pass


def address(node, root, max_nodes):
    expression = lifting.lift(node, root_instr=root, check_versions=False, dataflow=False, max_nodes=max_nodes)
    return evaluate(expression, {}) if expression is not None and not variables(expression) else None


class UpdateLifter(lifting._Lifter):
    def __init__(self, store, factory, span):
        super().__init__(store.function, store.non_ssa_form, False, factory.limits.expressions)
        self.dataflow = False
        self.factory, self.span, self.observations = factory, span, {}

    def origin(self, value, width):
        """Copies/phis of whole global reads, including compare-exchange retry cycles.
        Every dependency must have a path to a full global load; unknown initial values fail.
        """
        pending, graph, seeds = [value], {}, set()
        while pending:
            self.factory.tick(); current = pending.pop()
            if current in graph: continue
            definition = self.ssa.get_ssa_var_definition(current)
            graph[current] = []
            if definition is None: return False
            if definition.operation == M.MLIL_VAR_PHI:
                graph[current] = list(definition.src)
            elif definition.operation == M.MLIL_SET_VAR_SSA:
                src = definition.src
                if src.operation in _LOADS and src.size * 8 == width:
                    at = address(src.src, definition.non_ssa_form, self.factory.limits.expressions)
                    if at is None or at + getattr(src, "offset", 0) != self.span[0] or src.size != self.span[1] - self.span[0]: return False
                    seeds.add(current)
                elif src.operation == M.MLIL_VAR_SSA and src.size * 8 == width:
                    graph[current] = [src.src]
                else: return False
            else: return False
            pending.extend(graph[current])
        reached = set(seeds)
        while True:
            self.factory.tick()
            added = {v for v, deps in graph.items() if any(d in reached for d in deps)} - reached
            if not added: break
            reached |= added
        return bool(seeds) and reached == set(graph)

    def var(self, value, width):
        self.factory.tick()
        if width == (self.span[1] - self.span[0]) * 8 and self.origin(value, width):
            key = value.var.identifier, value.version
            return self.observations.setdefault(key, Var(f"observation_{len(self.observations)}", width))
        return super().var(value, width)

    def expr(self, node, *, inlined):
        self.factory.tick()
        if node.operation in (M.MLIL_ZX, M.MLIL_SX, M.MLIL_LOW_PART) and node.size == node.src.size:
            return self.expr(node.src, inlined=inlined)
        if node.operation in _LOADS:
            at = address(node.src, self.root, self.factory.limits.expressions)
            if at is None or at + getattr(node, "offset", 0) != self.span[0] or node.size != self.span[1] - self.span[0]: return None
            key = "load", node.expr_index
            return self.observations.setdefault(key, Var(f"observation_{len(self.observations)}", node.size * 8))
        return super().expr(node, inlined=inlined)


class Factory:
    def __init__(self, il, limits, deadline):
        self.il, self.view, self.limits, self.deadline = il, il.source_function.view, limits, deadline
        self.left = limits.work
        self.uses, self.cache, self.updates, self.notes = {}, {}, {}, []

    def tick(self):
        self.left -= 1
        if self.left < 0 or self.deadline.expired(): raise ValueError("mutable-global proof budget exhausted")

    def uses_of(self, func):
        self.tick()
        if func is None: return None
        if func.start not in self.uses:
            il = self.il if func.start == self.il.source_function.start else func.mlil_if_available
            if il is None or len(il) > self.limits.instructions: return None
            self.uses[func.start] = evidence.Uses(il.ssa_form)
        return self.uses[func.start]

    def accept_store(self, uses, store, span):
        self.tick()
        key = uses.ssa.source_function.start, store.expr_index, span
        if key in self.updates: return self.updates[key] is not None
        self.updates[key] = None
        if len(self.updates) > self.limits.writes: return False
        target = address(store.dest, store.non_ssa_form, self.limits.expressions)
        if target is None: return False
        target += getattr(store, "offset", 0)
        if target < span[0] or target + store.size > span[1]: return False
        lifter = UpdateLifter(store, self, (target, target + store.size))
        source = lifter.expr(store.src, inlined=False)
        if source is None or source.width != 8 * store.size: return False
        source = normalize(source)
        if not isinstance(source, BinOp) or source.op not in ("xor", "add", "sub"): return False
        a, b = source.left, source.right
        if source.op != "sub" and isinstance(a, Const): a, b = b, a
        if not isinstance(a, Var) or not isinstance(b, Const) or a not in lifter.observations.values(): return False
        operation = "add" if source.op == "sub" else source.op
        constant = (-b.value if source.op == "sub" else b.value) & ((1 << source.width) - 1)
        proposed = BinOp(operation, a, Const(constant, source.width), source.width)
        if check(source, proposed, z3_timeout_ms=self.limits.proof_ms, deadline=self.deadline).verdict is not Verdict.PROVEN: return False
        try:
            update = embed_update(Update(operation, constant), target - span[0], store.size, span[1] - span[0],
                                  little_endian=self.il.arch.endianness.name == "LittleEndian")
        except ValueError: return False
        self.updates[key] = (update, store.address)
        return True

    def domain(self, at, size):
        self.tick()
        key = at, size
        if key in self.cache: return self.cache[key]
        if len(self.cache) >= self.limits.globals: raise ValueError("mutable-global candidate limit")
        self.cache[key] = None
        if not fixed_load_is_safe(self.view, at, size) or not evidence.writable_data(self.view, at, at + size): return None
        if not self.view.has_initial_analysis():
            raise InitialAnalysisPending("mutable writes await the initial cross-reference index")
        previous = self.view.get_previous_data_var_start_before(at + 1)
        var = self.view.get_data_var_at(previous) if previous <= at else None
        if var is not None and previous + var.type.width <= at: var = None
        if var is not None and var.type.volatile:
            self.notes.append(f"{at:#x}: volatile global is excluded"); return None
        width = evidence.widest_read(self.view, at, size, self.uses_of, self.limits.bits // 8, self.limits.readers)
        if width < size: return None
        read = evidence.reader(self.view)
        why = evidence.key_problem(self.view, at, at + width, read)
        if why not in (None, "zero"):
            self.notes.append(f"{at:#x}: {why}"); return None
        first = evidence.object_start(self.view, at, uses_of=self.uses_of)
        # A byte read inside an exported/relocated object inherits its external writers.
        # Looking only at symbols exactly on the byte would incorrectly prove it private.
        for symbol in self.view.get_symbols(first, at + width - first):
            self.tick()
            if symbol.binding in (SymbolBinding.GlobalBinding, SymbolBinding.WeakBinding):
                self.notes.append(f"{at:#x}: containing object {symbol.name} is exported"); return None
        if self.view.range_contains_relocation(first, at + width - first):
            self.notes.append(f"{at:#x}: containing object is relocated"); return None
        refs = list(islice(self.view.get_code_refs(first, at + width - first), self.limits.readers + 1))
        if len(refs) > self.limits.readers:
            self.notes.append(f"{at:#x}: mutable reader limit"); return None
        why = evidence.never_written(self.view, at, at + width, self.uses_of, self.deadline, first=first,
                                     accepted_store=self.accept_store, proof_limit=self.limits.work)
        if why:
            self.notes.append(f"{at:#x}: skipped: {why}"); return None
        updates = [v for k, v in self.updates.items() if k[2] == (at, at + width) and v is not None]
        if not updates: return None  # Ordinary immutable-key handling remains separate.
        initial = read(at, width)
        found = closure(initial, width * 8, [u for u, _ in updates],
                        limits=ClosureLimits(self.limits.values, self.limits.writes, self.limits.closure_steps, self.limits.bits),
                        deadline=self.deadline)
        if found.reason:
            self.notes.append(f"{at:#x}: {found.reason}"); return None
        domain = Domain(at, width, found.values, tuple(sorted({where for _, where in updates})))
        self.cache[key] = domain
        self.notes.append(f"{at:#x}: skipped constant key: write at {domain.writes[0]:#x}; possible values " + ", ".join(hex(v) for v in sorted(domain.values)))
        return domain


def run(il, limits=Limits(), deadline=Deadline.never()):
    started, cpu = time.monotonic(), time.thread_time(); report = Report()
    factory = Factory(il, limits, deadline)
    replacements, preserved, count = {}, {}, 0
    try:
        if not 1 <= limits.bits <= 64: raise ValueError("native mutable scalar widths are limited to 64 bits")
        if len(il) > limits.instructions: raise ValueError("mutable instruction limit")
        # Discover supported domains once before lifting consumers. Most functions have none;
        # do not spend their MBA budget trying constant-only arithmetic or readonly decoders.
        for instruction in il.instructions:
            factory.tick()
            # Report domains even for an unmodified, bare read.
            for node in walk(instruction):
                factory.tick()
                if node.operation not in _READS: continue
                at = address(node.src.ssa_form, instruction, limits.expressions)
                if at is not None: factory.domain(at + getattr(node, "offset", 0), node.size)
        report.domains = sorted({(d.address, d.size, tuple(sorted(d.values))) for d in factory.cache.values() if d is not None})
        if not report.domains: return il, report
        for instruction in il.instructions:
            factory.tick()
            # Never change a writer's memory operation or an intrinsic/atomic operation.
            if instruction.operation not in (M.MLIL_SET_VAR, M.MLIL_RET, M.MLIL_IF): continue
            pending = [value for _, value in operands(instruction) if hasattr(value, "operation")]
            while pending:
                factory.tick(); root = pending.pop()
                if root.operation not in lifting._BINARY and root.operation not in lifting._COMPARE:
                    pending.extend(value for _, value in operands(root) if hasattr(value, "operation")); continue
                if count >= limits.candidates: raise ValueError("mutable expression candidate limit")
                count += 1
                reads, nodes = [], [root]
                protected = False
                while nodes:
                    factory.tick(); node = nodes.pop()
                    protected |= bool(node.attributes & _PROTECTED)
                    if node.operation in _READS and node.expr_type is not None:
                        protected |= bool(node.expr_type.volatile)
                    if node.operation in _READS: reads.append(node)
                    nodes.extend(reversed([value for _, value in operands(node) if hasattr(value, "operation")]))
                if protected:
                    report.notes.append(f"{root.address:#x}: protected or volatile consumer stays unchanged")
                    continue
                if len({n.expr_index for n in reads}) != len(reads):
                    report.notes.append(f"{root.address:#x}: repeated raw load expression is not one observation")
                    continue
                expression = lifting.lift(root.ssa_form, root_instr=instruction, check_versions=False,
                                          load_leaves=True, dataflow=False, max_nodes=limits.expressions)
                domains, supported = {}, expression is not None
                if expression is not None and expression.width > limits.bits:
                    supported = False
                    report.notes.append(f"{root.address:#x}: mutable consumer width exceeds the scalar limit")
                if expression is not None:
                    for leaf in variables(expression):
                        node = leaf.ref
                        if not hasattr(node, "operation") or node.operation not in _LOADS: continue
                        if node.attributes & _PROTECTED or node.expr_type is not None and node.expr_type.volatile:
                            supported = False; break
                        at = address(node.src, instruction, limits.expressions)
                        domain = None if at is None else factory.domain(at + getattr(node, "offset", 0), node.size)
                        if domain is None: supported = False; break
                        mask = (1 << leaf.width) - 1
                        # The load starts at the proved object's base; project its actual width.
                        shift = 0 if il.arch.endianness.name == "LittleEndian" else 8 * (domain.size - node.size)
                        domains[leaf.name] = frozenset((value >> shift) & mask for value in domain.values)
                if supported and domains:
                    env = {v.name: min(domains[v.name]) if v.name in domains else 0 for v in variables(expression)}
                    candidate = Const(evaluate(expression, env), expression.width)
                    proof = constrained(expression, candidate, domains, timeout_ms=limits.proof_ms,
                                        max_values=limits.values, max_domains=limits.globals, max_nodes=limits.expressions,
                                        deadline=deadline)
                    if proof.verdict is Verdict.PROVEN:
                        replacements[root.expr_index] = candidate
                        # Keep every read performed by this root, in operand order. Reads already
                        # materialized by earlier assignments remain in those assignments.
                        preserved.setdefault(instruction.instr_index, []).extend(reads)
                        continue
                    report.notes.append(f"{root.address:#x}: value-set proof {proof.verdict.value}: " +
                                        (proof.reason or "expression is not invariant across the permitted inputs"))
                pending.extend(value for _, value in operands(root) if hasattr(value, "operation"))
        if report.domains:
            footprint, size = {}, 0
            for uses in factory.uses.values():
                for block in uses.ssa.source_function.basic_blocks:
                    factory.tick()
                    key = block.start, block.end - block.start
                    if key in footprint: continue
                    size += key[1]
                    if size > limits.evidence_bytes: raise ValueError("mutable evidence byte limit")
                    raw = factory.view.read(*key)
                    if len(raw) != key[1]: raise ValueError("mutable code bytes unavailable")
                    footprint[key] = tuple(raw)
            for at, width, _ in report.domains:
                raw = factory.view.read(at, width)
                if len(raw) != width: raise ValueError("mutable initial bytes unavailable")
                footprint[at, width] = tuple(raw)
            report.evidence = tuple((at, raw) for (at, _), raw in footprint.items())
        if not replacements: return il, report
        new = MediumLevelILFunction(il.arch, low_level_il=il.llil)
        new.prepare_to_copy_function(il)
        def copy(node):
            factory.tick()
            if node.expr_index in replacements:
                value = replacements[node.expr_index]
                return new.const(value.width // 8, value.value, ILSourceLocation.from_instruction(node))
            result = node.copy_to(new, copy)
            attributes = node.attributes | {Attr.ILPreventDeadStoreElimination} if node.operation in _READS else node.attributes
            new.set_expr_attributes(result, attributes)
            return result
        for block in il.basic_blocks:
            new.prepare_to_copy_block(block)
            for instruction in block:
                loc = ILSourceLocation.from_instruction(instruction)
                new.set_current_address(instruction.address, block.arch)
                for read in preserved.get(instruction.instr_index, ()):
                    value = read.copy_to(new)
                    new.set_expr_attributes(value, read.attributes | {Attr.ILPreventDeadStoreElimination})
                    new.append(value, ILSourceLocation.from_instruction(read))
                new.append(copy(instruction), loc)
        new.finalize(); new.generate_ssa_form()
        report.folded = len(replacements)
        return new, report
    except InitialAnalysisPending as exc:
        report.deferred = True
        report.notes.append(str(exc))
        return il, report
    except (ValueError, NotImplementedError, RecursionError) as exc:
        report.notes.append(f"left unchanged: {exc}")
        return il, report
    finally:
        report.notes.extend(factory.notes)
        report.seconds, report.cpu_seconds = time.monotonic() - started, time.thread_time() - cpu
        report.exhausted = deadline.expired()
