"""Deciding and applying the flow fixes (v4.0, spec 3.1 steps 2 and 3), per view.

Sites wait in the view's queue. Once analysis is idle (a completion event), a round decides at most
SITES_PER_ROUND of them within ROUND_SECONDS, saves the record in the view's metadata and reanalyzes what
changed. Their MLIL activity collects again; a decided site is not queued again, so rounds stop when nothing
new is found. Binary Ninja is never called with a state's lock held.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Optional

from binaryninja import ILSourceLocation, MediumLevelILOperation as Op

from ...deadline import Deadline
from ...flow.codecheck import call_problem, is_tail_call, jump_problem, split_starts
from ...flow.ranges import parse as parse_ranges
from ...flow.record import Change, Record
from ...flow.tables import decode, span_of
from ...ir.evaluate import evaluate
from .. import log
from . import collect, evidence, readonly, undo, wrappers

KEY = "binja_d810.flow"  # view metadata: the record, as JSON
SITES_PER_ROUND = 64
ROUND_SECONDS = 2.0
NOTES = 40  # report lines kept per function


@dataclass
class State:
    record: Record
    saved: Optional[str] = None  # the record's JSON as last stored
    lock: threading.RLock = field(default_factory=threading.RLock)
    pending: dict = field(default_factory=dict)  # site key -> Site
    decided: defaultdict = field(default_factory=lambda: defaultdict(set))  # function -> decided site keys
    claimants: set = field(default_factory=set)  # functions with a claim in the record
    rejected: dict = field(default_factory=dict)  # (start, end) -> why it may be written, for the session
    notes: defaultdict = field(default_factory=lambda: defaultdict(list))  # function -> report lines
    ranges: Optional[list] = None
    enabled: set = field(default_factory=set)  # functions currently observed with the flow fixes enabled
    events: list = field(default_factory=list)  # completion events, kept alive
    armed: bool = False
    running: bool = False

    def recount(self) -> None:
        """Who claims what, after the record changed (the caller holds the lock)."""
        self.claimants = {c for change in self.record.changes() for c in change.claims}


_STATES: dict[int, State] = {}
_STATES_LOCK = threading.Lock()


def state_of(view) -> State:
    with _STATES_LOCK:
        state = _STATES.get(view.file.session_id)
    if state is not None:
        return state
    text = _load(view)
    with _STATES_LOCK:
        state = _STATES.setdefault(view.file.session_id,
                                   State(Record.from_json(text) if text else Record(), saved=text))
    with state.lock:
        state.recount()
        recorded_ranges = [c for c in state.record.changes() if c.kind == "range"]
        if recorded_ranges:
            state.ranges = [(c.key, c.detail["end"]) for c in recorded_ranges]
            state.enabled = {f for c in recorded_ranges for f in c.detail.get("users", [])}
    return state


def _load(view) -> Optional[str]:
    try:
        return view.query_metadata(KEY)
    except KeyError:
        return None


def _store(view, text: Optional[str]) -> None:
    if text is not None:
        view.store_metadata(KEY, text)
        return
    try:
        view.remove_metadata(KEY)
    except KeyError:
        pass


def _arm(view, state: State) -> None:
    """Run a round once analysis is idle (called without the lock)."""
    with state.lock:
        if state.armed:
            return
        state.armed = True
    event = view.add_analysis_completion_event(lambda: _completed(view))
    with state.lock:
        state.events = state.events[-15:] + [event]


def _completed(view) -> None:
    try:
        run_round(view)
    except Exception as exc:  # never break analysis
        log.logger.log_error(f"binja-d810 flow round failed: {exc!r}")
        state = state_of(view)
        with state.lock:
            state.running = False


def enqueue(view, site) -> None:
    state = state_of(view)
    with state.lock:
        if site.key in state.decided[site.function] or site.key in state.pending:
            return
        state.pending[site.key] = site
    _arm(view, state)


def busy(view) -> bool:
    """Sites wait, or a round is armed or running."""
    with _STATES_LOCK:
        state = _STATES.get(view.file.session_id)
    if state is None:
        return False
    with state.lock:
        return bool(state.pending) or state.armed or state.running


def record_of(view) -> Record:
    """A copy of the view's record."""
    state = state_of(view)
    with state.lock:
        return Record.from_json(state.record.to_json())


def notes_for(view, function: int) -> list[str]:
    """What the flow fixes did or skipped for a function, for its debug report."""
    with _STATES_LOCK:
        state = _STATES.get(view.file.session_id)
    if state is None:
        return []
    with state.lock:
        return list(state.notes.get(function, []))


def release(view, function: int) -> None:
    """D-810 or the flow fixes are off for `function`: forget its sites and notes and release its claims. The
    next round undoes what nobody claims any more. Called for every function analyzed with D-810 off, so a
    function the flow fixes never saw costs one lookup."""
    state = state_of(view)
    with state.lock:
        known = function in state.decided or function in state.claimants or function in state.notes
        if not known and not any(site.function == function for site in state.pending.values()):
            return
        state.decided.pop(function, None)
        state.notes.pop(function, None)
        for key in [key for key, site in state.pending.items() if site.function == function]:
            del state.pending[key]
        if function not in state.claimants:
            return
        state.record.release(function)
        state.recount()
    _arm(view, state)


def check_ranges(view, texts: list[str]) -> None:
    """The read-only ranges setting as an MLIL activity saw it ([] with the flow fixes off). A change from what
    was seen before arms a round, which brings the record in line."""
    ranges, errors = parse_ranges(texts)
    state = state_of(view)
    with state.lock:
        if state.ranges == ranges:
            return
        state.ranges = ranges
    for text in errors:
        log.logger.log_warn(f"binja-d810: read-only range {text!r} is not 0xSTART-0xEND; ignored")
    _arm(view, state)


def observe_enabled(func, enabled: bool, texts: list[str]) -> None:
    """Ranges live while at least one analyzed function uses the flow fixes; unrelated disabled functions
    must not clear another function's ranges. Runs from the unconditional flow activity."""
    state = state_of(func.view)
    with state.lock:
        was_enabled = func.start in state.enabled
        if enabled:
            state.enabled.add(func.start)
        else:
            state.enabled.discard(func.start)
        clear = not state.enabled and state.ranges is not None
        changed_users = was_enabled != enabled and bool(state.ranges)
    if enabled or clear:
        check_ranges(func.view, texts if enabled else [])
    if changed_users:
        _arm(func.view, state)


def trusted(view, site) -> bool:
    """Every table and key global the site reads is const or read-only already."""
    for load in site.target.loads:
        span = span_of(load)
        if span is None or not evidence.constant_data(view, span.start, span.end):
            return False
    return True


def after_pass(view, func, adapter, report) -> bool:
    """The end of the MLIL activity for a D-810-enabled function: collect its sites, make a trusted site's one
    target a constant where the MLIL may say so (spec 3.4), and queue the rest. True when the MLIL changed."""
    read = evidence.reader(view)
    code = evidence.BinaryCode(view, func.arch)
    rewrites = []  # (non-SSA instruction, its target, its loads' expression indices)
    for site, instr in collect.scan(adapter.mlil, Deadline.after(collect.SCAN_SECONDS), read):
        if site.target is not None:
            site = replace(site, trusted=trusted(view, site))
            if site.trusted:
                target = _direct(site, instr, read, code)
                if target is not None:
                    rewrites.append((instr.non_ssa_form, target, site.target.load_exprs))
                    continue
                if instr.operation == Op.MLIL_JUMP_TO:
                    continue  # resolved, to another target than the table's (the user's branches)
            if not site.target.loads:
                continue  # a constant target: no table to decide on
        enqueue(view, site)
    if not rewrites:
        return False
    _rewrite(adapter, rewrites)
    for instr, target, _ in rewrites:
        report.flow.append(f"{instr.address:#x}: target -> {_name(view, target)}")
    return True


def _direct(site, instr, read, code) -> Optional[int]:
    """The one target a trusted site decodes to, when the MLIL may say so: a call to a function's start, a tail
    jump to one, or a resolved jump's own target (its table read then dies). A target Binary Ninja already folded
    (it read the const table itself) is its constant."""
    if site.target.loads:
        decoded, _ = decode(site.target.expr, site.target.loads, read)
        if decoded is None or len(set(decoded.targets)) != 1:
            return None
        target = decoded.targets[0]
    else:
        target = evaluate(site.target.expr, {})
    if site.kind == "call":
        return target if code.function_at(target) else None
    if instr.operation == Op.MLIL_JUMP_TO:
        return target if set(instr.targets) == {target} else None
    return target if is_tail_call([target], site.function, code) else None


def _rewrite(adapter, rewrites) -> None:
    """Each target becomes its constant. What only fed the old targets dies, their table loads counted pure; as
    in v3.5, assignments dead before stay."""
    pure = frozenset().union(*(exprs for _, _, exprs in rewrites))
    dead_before = adapter.dead_assignments(pure)
    mlil = adapter.mlil
    for instr, target, _ in rewrites:
        dest = instr.dest
        adapter.backups.append((dest.expr_index, dest.copy_to(mlil)))
        constant = mlil.const_pointer(dest.size, target, ILSourceLocation.from_instruction(dest))
        mlil.replace_expr(dest.expr_index, constant)
    adapter.finish()
    newly_dead = adapter.dead_assignments(pure) - dead_before
    if newly_dead:
        adapter.remove(newly_dead)
        adapter.finish()


def branches_for(view, function: int) -> list[tuple[int, list[int]]]:
    """(jump address, targets) for each jump of `function` with recorded targets."""
    state = state_of(view)
    with state.lock:
        return state.record.branches_of(function)


def restore_inline(func) -> None:
    """Reapply a claimed wrapper's auto flag after Binary Ninja resets it on a full function update.

    The callers own the claim, so the wrapper itself need not have D-810 enabled. A non-default flag
    (including a user's explicit refusal to inline) always wins.
    """
    with _STATES_LOCK:
        state = _STATES.get(func.view.file.session_id)
    if state is None:
        if _load(func.view) is None:
            return
        state = state_of(func.view)
    with state.lock:
        change = state.record.get("inline", func.start)
        claimed = change is not None and bool(change.claims)
    if claimed:
        wrappers.set_inline_if_default(func)


def _affected(change: Change) -> set[int]:
    affected = set(change.detail.get("readers", [])) | set(change.detail.get("callers", []))
    if change.kind in ("branches", "split"):
        affected.add(change.detail["function"])
    if change.kind == "inline":
        affected.add(change.key)
    return affected


class Round:
    """One round's work: the view, its record, what to reanalyze, and caches."""

    def __init__(self, view, state: State):
        self.view, self.state, self.record = view, state, state.record
        self.read = evidence.reader(view)
        self.touched: set[int] = set()
        self.verdicts: dict[tuple[int, int], Optional[str]] = {}
        self._uses: dict[int, Optional[evidence.Uses]] = {}
        self._code: dict[str, evidence.BinaryCode] = {}

    def uses_of(self, func) -> Optional[evidence.Uses]:
        if func.start not in self._uses:
            try:
                mlil = func.mlil
                self._uses[func.start] = None if mlil is None else evidence.Uses(mlil.ssa_form)
            except Exception:  # noqa: BLE001 - no MLIL for this function
                self._uses[func.start] = None
        return self._uses[func.start]

    def code(self, func) -> evidence.BinaryCode:
        if func.arch.name not in self._code:
            self._code[func.arch.name] = evidence.BinaryCode(self.view, func.arch)
        return self._code[func.arch.name]

    def add(self, change: Change) -> Change:
        """Keep a change; one kept already for the same kind and key gains its claims."""
        existing = self.record.get(change.kind, change.key)
        affected = _affected(existing if existing is not None else change)
        # Resolving code changes inferred signatures, which full reanalysis does not clear. Keep the
        # original once, with the same claims as the changes that affect it. User signatures still win.
        signatures = []
        for start in sorted(affected):
            func = self.view.get_function_at(start)
            if func is not None:
                signatures.append(Change("signature", start, {"type": evidence.type_text(func.type)},
                                         claims=set(change.claims)))
        with self.state.lock:
            for signature in signatures:
                self.record.add(signature)
            return self.record.add(change)

    def drop(self, change: Change) -> None:
        with self.state.lock:
            self.record.drop(change.kind, change.key)

    def note(self, function: int, line: str) -> None:
        with self.state.lock:
            lines = self.state.notes[function]
            lines.append(line)
            del lines[:-NOTES]
        log.logger.log_info(f"flow: {function:#x}: {line}")


def run_round(view) -> None:
    state = state_of(view)
    with state.lock:
        state.armed, state.running = False, True
        sites = list(state.pending.values())[:SITES_PER_ROUND]
    work = Round(view, state)
    with state.lock:
        ranges = list(state.ranges or [])
    readonly.release_stale(work, ranges)
    with state.lock:
        dependencies = defaultdict(set)
        for change in state.record.changes():
            for start in _affected(change):
                dependencies[start] |= change.claims
        for change in state.record.changes():
            if change.kind == "signature":
                change.claims = set(dependencies[change.key])
        stale = state.record.unclaimed()
    defer_signatures = (any(change.kind == "signature" for change in stale)
                        and any(change.kind != "signature" for change in stale))
    restored = set()
    for change in stale:
        if change.kind == "signature" and defer_signatures:
            continue  # first regenerate the original body; its old IL would re-infer the changed signature
        restored |= undo.revert(view, change)
        work.drop(change)
    work.touched |= restored
    with state.lock:
        for start in restored:
            state.decided.pop(start, None)
    readonly.apply_wanted(work, ranges)
    deadline = Deadline.after(ROUND_SECONDS)
    done = []
    for site in sites:
        if deadline.expired():
            break
        decide(work, site)
        done.append(site)
    with state.lock:
        for site in done:
            state.pending.pop(site.key, None)
            state.decided[site.function].add(site.key)
        left = next(iter(state.pending.values()), None)
        state.recount()
        text = state.record.to_json() if len(state.record) else None
        changed, state.saved = text != state.saved, text
    if changed:
        _store(view, text)
    if left is not None or defer_signatures:
        _arm(view, state)
        if not work.touched and left is not None:
            work.touched.add(left.function)  # analysis must run for the next round to start
    for start in sorted(work.touched):
        func = view.get_function_at(start)
        if func is not None:
            func.reanalyze()
    with state.lock:
        state.running = False
    view.update_analysis()


def _name(view, address: int) -> str:
    func = view.get_function_at(address)
    return func.name if func is not None else f"{address:#x}"


def decide(work: Round, site) -> None:
    """Decide one site: check its decoded targets, type its tables const when nothing writes them, add call
    targets that are not functions yet, join the code the sweep split off a jump, or inline an identity wrapper."""
    if site.kind == "wrapper":
        wrappers.decide(work, site)
        return
    func = work.view.get_function_at(site.function)
    if func is None:
        return
    decoded, why = decode(site.target.expr, site.target.loads, work.read)
    if decoded is None:
        work.note(site.function, f"{site.address:#x}: skipped: {why}")
        return
    code = work.code(func)
    if site.kind == "jump":
        problem = jump_problem(decoded.targets, site.function, code)
    else:
        problem = call_problem(decoded.targets, code)
    if problem:
        work.note(site.function, f"{site.address:#x}: skipped: {problem}")
        return
    if not site.trusted:
        for load, span in zip(site.target.loads, decoded.spans):
            why = _unwritable(work, load, span)
            if why:
                work.note(site.function, f"{site.address:#x}: skipped: {why}")
                return
        for load, span in zip(site.target.loads, decoded.spans):
            _type_table(work, site, load, span, decoded.targets)
    if site.kind == "jump" and site.trusted and not is_tail_call(decoded.targets, site.function, code):
        _record_branches(work, site, decoded.targets)
    if site.kind == "call":
        _add_functions(work, site, func, decoded.targets, code)
    else:
        _join_split_code(work, site, decoded.targets, code)


def _unwritable(work: Round, load, span) -> Optional[str]:
    """None when the span may be typed const: typed for another site already, or no other change and no user
    type there and nothing visible writes it (checked from where its object starts). Else why not."""
    view, record = work.view, work.record
    if record.covering(span.start, span.end) is not None:
        return None
    other = record.overlapping(span.start, span.end)
    if other is not None:
        return f"{span.start:#x} overlaps the {other.kind} at {other.key:#x}"
    for var in evidence.data_vars(view, span.start, span.end):
        if not var.auto_discovered:
            return f"{var.address:#x} has a user type"
    key = (evidence.object_start(view, span.start, load.base, work.uses_of), span.end)
    with work.state.lock:
        rejected = work.state.rejected.get(key)
    if rejected is not None:
        return rejected
    if key not in work.verdicts:
        work.verdicts[key] = evidence.never_written(view, key[0], key[1], work.uses_of)
        if work.verdicts[key] is not None:
            with work.state.lock:
                work.state.rejected[key] = work.verdicts[key]
    return work.verdicts[key]


def _type_table(work: Round, site, load, span, targets) -> None:
    view = work.view
    existing = work.record.covering(span.start, span.end)
    if existing is not None:  # a table typed for another site, or a read-only range (the setting's alone)
        if existing.kind == "table" and site.function not in existing.claims:
            work.add(Change(existing.kind, existing.key, claims={site.function}))
            work.note(site.function, f"{span.start:#x} is const already")
        return
    t = evidence.table_type(view, span)
    if t is None:
        work.note(site.function, f"{span.start:#x}: skipped: no const type parses for it")
        return
    first = evidence.object_start(view, span.start, load.base, work.uses_of)
    previous = [[v.address, evidence.type_text(v.type)] for v in evidence.data_vars(view, span.start, span.end)]
    readers = evidence.referencing(view, first, span.end) | {site.function}
    symbols = [[s.address, s.raw_name] for s in view.get_symbols(span.start, span.end - span.start)]
    evidence.define_type(view, span.start, t)
    targets = sorted(set(targets))
    work.add(Change("table", span.start, {"end": span.end, "from": first,
                                          "defined": [[span.start, evidence.type_text(t)]], "targets": targets,
                                          "readers": sorted(readers), "symbols": symbols},
                    previous, {site.function}))
    work.touched |= readers
    work.note(site.function, f"typed {span.start:#x}[{span.count}] const, read by a {site.kind} to "
                             + ", ".join(_name(view, target) for target in targets))


def _add_functions(work: Round, site, func, targets, code) -> None:
    """Call targets that are plain code become functions (auto, recorded)."""
    for target in sorted(set(targets)):
        if work.record.get("function", target) is not None:
            work.add(Change("function", target, claims={site.function}))
            continue
        if code.function_at(target):
            continue
        work.view.add_function(target, func.platform)
        work.add(Change("function", target, {}, [], {site.function}))
        work.touched.add(site.function)
        work.note(site.function, f"added a function at {target:#x}, a call target")


def _join_split_code(work: Round, site, targets, code) -> None:
    """Functions the sweep split off at a jump's own targets are removed (auto, recorded), so its code joins
    the jump's function."""
    for target in split_starts(targets, site.function, code):
        if work.record.get("split", target) is not None:
            work.add(Change("split", target, claims={site.function}))
            continue
        found = work.view.get_function_at(target)
        if found is None or not found.auto:
            continue
        name = found.name
        work.view.remove_function(found)
        work.add(Change("split", target, {"function": site.function}, [], {site.function}))
        work.touched.add(site.function)
        work.note(site.function, f"removed {name}: code the sweep split off this jump")


def _record_branches(work: Round, site, targets) -> None:
    """A jump Binary Ninja cannot resolve itself gets its targets from the branches activity (spec 3.4)."""
    if work.record.get("branches", site.address) is not None:
        work.add(Change("branches", site.address, claims={site.function}))
        return
    targets = sorted(set(targets))
    work.add(Change("branches", site.address, {"function": site.function, "targets": targets}, [], {site.function}))
    work.touched.add(site.function)
    work.note(site.function, f"{site.address:#x}: jump targets " + ", ".join(f"{t:#x}" for t in targets))
