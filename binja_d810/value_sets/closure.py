"""Closure under every observed constant update, independent of guard ordering."""

from collections import deque
from dataclasses import dataclass

from ..deadline import Deadline


@dataclass(frozen=True)
class Update:
    operation: str
    constant: int


def embed_update(update, offset, size, total, *, little_endian):
    """Lift one exact byte-slice update without inventing carries into neighboring bytes."""
    if size <= 0 or offset < 0 or offset + size > total or update.operation not in ("xor", "add"):
        raise ValueError("invalid mutable write span")
    constant = update.constant & ((1 << (size * 8)) - 1)
    if size == total:
        return Update(update.operation, constant)
    if update.operation != "xor":
        raise ValueError("partial mutable addition has unproved carries")
    shift = 8 * (offset if little_endian else total - offset - size)
    return Update("xor", constant << shift)


@dataclass(frozen=True)
class Limits:
    values: int = 64
    writes: int = 1024
    steps: int = 4096
    bits: int = 64


@dataclass(frozen=True)
class Result:
    values: frozenset[int] = frozenset()
    reason: str = ""


def closure(initial, width, updates, *, limits=Limits(), deadline=Deadline.never()):
    if type(initial) is not int:
        return Result(reason="mutable initial value is unknown")
    if type(width) is not int or width < 1 or width > limits.bits or limits.values < 1:
        return Result(reason="mutable value width or set limit")
    if len(updates) > limits.writes:
        return Result(reason="mutable write limit")
    if any(not isinstance(update, Update) or update.operation not in ("xor", "add") or type(update.constant) is not int for update in updates):
        return Result(reason="unmodeled mutable write")
    mask = (1 << width) - 1
    found = {initial & mask}
    pending, steps = deque(found), 0
    while pending:
        if deadline.expired():
            return Result(reason="mutable closure deadline exhausted")
        value = pending.popleft()
        for update in updates:
            if steps >= limits.steps or deadline.expired():
                return Result(reason="mutable closure work limit")
            steps += 1
            successor = ((value ^ update.constant) if update.operation == "xor" else value + update.constant) & mask
            if successor in found:
                continue
            if len(found) >= limits.values:
                return Result(reason="mutable value set limit")
            found.add(successor)
            pending.append(successor)
    return Result(frozenset(found))
