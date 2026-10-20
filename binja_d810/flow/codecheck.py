"""Whether decoded targets are code a jump or call may reach (v4.0, spec 3.3), over what the view knows.
No binaryninja import."""

from __future__ import annotations

from typing import Optional, Protocol, Sequence


class CodeView(Protocol):
    def is_code(self, address: int) -> bool:
        """In an executable segment, and in a code section when the binary has sections."""

    def decodes(self, address: int) -> bool:
        """An instruction of the function's architecture decodes there."""

    def owners(self, address: int) -> Sequence[int]:
        """The starts of the known functions containing the address."""

    def function_at(self, address: int) -> bool:
        """A known function starts there."""

    def split_off(self, function: int, address: int) -> bool:
        """A function starts at `address` that Binary Ninja's sweep split off `function`: auto, no symbol,
        no callers or code references, and no called or named function between the two (spec 3.3)."""


def _not_code(target: int, view: CodeView) -> Optional[str]:
    if not view.is_code(target):
        return f"{target:#x} is not code"
    if not view.decodes(target):
        return f"{target:#x} does not decode"
    return None


def _another_start(target: int, function: int, view: CodeView) -> bool:
    return target != function and view.function_at(target) and not view.split_off(function, target)


def is_tail_call(targets: Sequence[int], function: int, view: CodeView) -> bool:
    """One target, the start of another function: a tail call through the table."""
    return len(set(targets)) == 1 and _another_start(targets[0], function, view)


def is_self_call(targets: Sequence[int], function: int) -> bool:
    """One target, the function's own start: a tail call to itself (v4.3 spec 3.3), made a back edge later."""
    return len(set(targets)) == 1 and targets[0] == function


def split_starts(targets: Sequence[int], function: int, view: CodeView) -> list[int]:
    """The targets that start a function the sweep split off `function`, ascending: removed once the table
    passes, so the jump's own code joins it."""
    return sorted({t for t in targets if t != function and view.function_at(t) and view.split_off(function, t)})


def jump_problem(targets: Sequence[int], function: int, view: CodeView) -> Optional[str]:
    """None when a jump in `function` may reach the targets: plain code of no other function (code the sweep
    split off counts as the jump's own), or one function's start (a tail call). Otherwise why not."""
    for target in targets:
        problem = _not_code(target, view)
        if problem:
            return problem
    if is_tail_call(targets, function, view):
        return None
    for target in targets:
        if _another_start(target, function, view):
            return f"{target:#x} starts another function"
        owners = view.owners(target)
        others = [start for start in owners if start != function and not view.split_off(function, start)]
        if others and function not in owners:
            return f"{target:#x} is inside the function at {others[0]:#x}"
    return None


def call_problem(targets: Sequence[int], view: CodeView) -> Optional[str]:
    """None when every target starts a function, or is code inside none (it is then added as one)."""
    for target in targets:
        if view.function_at(target):
            continue
        problem = _not_code(target, view)
        if problem:
            return problem
        owners = view.owners(target)
        if owners:
            return f"{target:#x} is inside the function at {owners[0]:#x}"
    return None
