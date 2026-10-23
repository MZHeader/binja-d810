"""Owned analysis comments applied through the existing idle/undo queue."""

from dataclasses import dataclass
from binaryninja import Settings
from ..flow.record import Change
from .flow import apply
from . import settings
from ..consteval.comments import remove_line


@dataclass(frozen=True)
class Site:
    function: int
    address: int
    owner: str
    text: str
    evidence: tuple = ()
    requires: tuple = ()
    kind: str = "annotation"

    @property
    def key(self):
        return self.kind, self.owner, self.function, self.address, self.text


def change_kind(owner):
    return "comment:" + owner


def reconcile(func, owner, wanted, enabled):
    state = apply.state_of(func.view)
    wanted = {site.address: site for site in wanted} if enabled else {}
    changed = False
    with state.lock:
        for change in state.record.claimed_by(func.start):
            if change.kind != change_kind(owner): continue
            site = wanted.get(change.key)
            if site is None or change.detail["text"] != site.text:
                change.claims.discard(func.start); changed = True
        for key, site in list(state.pending.items()):
            if site.function == func.start and site.kind == "annotation" and site.owner == owner and wanted.get(site.address) != site:
                del state.pending[key]; changed = True
        if changed:
            state.decided[func.start] = {k for k in state.decided[func.start]
                                       if not (len(k) > 1 and k[0] == "annotation" and k[1] == owner)}
        state.recount()
    if changed: apply._arm(func.view, state)
    for site in wanted.values(): apply.enqueue(func.view, site)


def decide(work, site):
    func = work.view.get_function_at(site.function)
    prefs = Settings()
    if func is None or not prefs.get_bool(site.owner, func) or not prefs.get_bool(settings.ENABLED, func) or not prefs.get_bool(settings.FLOW_FIXES, func):
        return
    if any(not prefs.get_bool(key, func) for key in site.requires): return
    if any(work.view.read(address, len(raw)) != bytes(raw) for address, raw in site.evidence):
        work.note(site.function, f"{site.address:#x}: comment skipped; analysis inputs changed")
        return
    kind = change_kind(site.owner)
    existing = work.record.get(kind, site.address)
    if existing is not None:
        if existing.detail["function"] == site.function and existing.detail["text"] == site.text:
            work.add(Change(kind, site.address, claims={site.function}))
        return  # User edits and explicit undo always win.
    previous = func.get_comment_at(site.address)
    if site.text in previous.splitlines(): return
    wanted = previous + ("\n" if previous else "") + site.text
    action = work.view.begin_undo_actions()
    try: func.set_comment_at(site.address, wanted)
    finally: work.view.commit_undo_actions(action)
    work.add(Change(kind, site.address, {"function": site.function, "text": site.text}, claims={site.function}))
    work.note(site.function, f"{site.address:#x}: {site.text}")


def revert(view, change):
    func = view.get_function_at(change.detail["function"])
    if func is None: return set()
    current = func.get_comment_at(change.key)
    restored = remove_line(current, change.detail["text"])
    if restored != current:
        action = view.begin_undo_actions()
        try: func.set_comment_at(change.key, restored)
        finally: view.commit_undo_actions(action)
    return set()
