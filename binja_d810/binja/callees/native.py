"""Optional native inlining for universally register-only summaries, recorded for undo.

Caller-specific frame proofs keep direct substitution: a global callee flag would also affect
callers whose frame uses have not been proved. Existing user/analysis flags always win.
"""

from dataclasses import dataclass

from binaryninja import InlineDuringAnalysis, InlineDuringAnalysisWithConfidence, Settings

from ...deadline import Deadline
from ...flow.record import Change
from .. import settings
from ..flow import apply
from .lift import summarize


@dataclass(frozen=True)
class Site:
    function: int
    address: int
    callee: int
    limits: object
    kind: str = "callee_inline"

    @property
    def key(self):
        return self.kind, self.function, self.callee


def _flag(func):
    flag = func.inline_during_analysis
    return [int(flag.value), flag.confidence]


def observe(func, enabled):
    state = apply.state_of(func.view)
    changed = False
    with state.lock:
        for change in state.record.claimed_by(func.start):
            if change.kind != "callee_inline":
                continue
            valid = enabled and all(func.view.read(a, len(raw)) == bytes(raw)
                                    for a, raw in change.detail["code"])
            if not valid:
                change.claims.discard(func.start)
                state.decided.pop(func.start, None)
                changed = True
        state.recount()
    if changed:
        apply._arm(func.view, state)


def request(func, sites, limits):
    for site in sites.values():
        summary = site.summary
        if summary.frame_outputs or summary.frame_inputs:
            continue
        apply.enqueue(func.view, Site(func.start, site.summary.function.start, summary.function.start, limits))


def decide(work, site):
    func = work.view.get_function_at(site.callee)
    caller = work.view.get_function_at(site.function)
    prefs = Settings()
    if func is None or caller is None or not prefs.get_bool(settings.CALLEES, caller) \
            or not prefs.get_bool(settings.CALLEE_NATIVE, caller):
        return
    existing = work.record.get("callee_inline", func.start)
    if existing is not None:
        work.add(Change("callee_inline", func.start, claims={site.function}))
        return
    result = summarize(func, site.limits, Deadline.after(site.limits.proof_ms / 1000))
    if result.summary is None or result.summary.frame_outputs or result.summary.frame_inputs:
        work.note(site.function, f"callee {func.start:#x}: native inline left unchanged; summary no longer register-only")
        return
    previous = _flag(func)
    if previous != [0, 0]:
        work.note(site.function, f"callee {func.start:#x}: native inline flag belongs to the user or other analysis")
        return
    code = []
    for node in result.summary.lifted.instructions:
        raw = work.view.read(node.address, func.arch.max_instr_length)
        info = func.arch.get_instruction_info(raw, node.address)
        if info is None or info.length <= 0:
            return
        pair = [node.address, list(raw[:info.length])]
        if pair not in code:
            code.append(pair)
    callers = sorted({f.start for f in func.callers} | {site.function})
    wanted = InlineDuringAnalysisWithConfidence(InlineDuringAnalysis.InlinePreservingTargetInstructionAddresses)
    undo = work.view.begin_undo_actions()
    try:
        func.set_user_inline_during_analysis(wanted)
    finally:
        work.view.commit_undo_actions(undo)
    work.add(Change("callee_inline", func.start,
                    {"callers": callers, "code": code, "previous_flag": previous,
                     "applied_flag": [int(wanted.value), wanted.confidence]},
                    claims={site.function}))
    work.touched |= {func.start, *callers}
    work.note(site.function, f"callee {func.start:#x}: native inline enabled after body-derived summary")
