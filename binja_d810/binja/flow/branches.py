"""The branches activity (v4.0, spec 3.4): sets the jump targets the flow fixes recorded for a function during
its own analysis, right after Binary Ninja resets auto branches (set from the completion callback, they would
be reset)."""

from . import apply


def apply_branches(func) -> int:
    """Set the recorded targets as auto indirect branches; a user's branches win. Returns the jumps set."""
    count = 0
    for address, targets in apply.branches_for(func.view, func.start):
        if any(not b.auto_defined for b in func.get_indirect_branches_at(address)):
            continue
        func.set_auto_indirect_branches(address, [(func.arch, t) for t in targets])
        count += 1
    return count
