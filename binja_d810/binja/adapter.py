"""The contract between the IL-independent engine and one IL level (spec section 4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Hashable, Iterator, Optional, Protocol

from ..ir.expr import Expr


@dataclass(frozen=True)
class Candidate:
    """A maximal effect-free supported expression, lifted with version-safe leaves."""

    root: object  # the IL expression to replace
    address: int
    ir: Expr
    # For skipping work (v2.1): the SSA value this root is the whole definition of, whether that
    # value has exactly one use, and the definitions the lift inlined.
    defines: Optional[Hashable] = None
    single_use: bool = False
    consumes: frozenset = frozenset()
    # What the user sees today: the root as written plus the definitions that die with it (all of
    # their uses are in this root). None: unknown, no visible-cost check.
    visible_nodes: Optional[int] = None
    visible_mixed: Optional[bool] = None
    # For ILAdapter.shallow: the root's instruction or statement, and the inlined definitions that
    # stay alive after a rewrite (used elsewhere too).
    context: object = None
    shared: frozenset = frozenset()
    # The root lifted without version safety (through inputs whose registers were reused before the
    # root). Only a proven constant may come from it: a constant reads no variable. constant_only:
    # there is no version-safe lift (ir is the unchecked one).
    unchecked: Optional[Expr] = None
    constant_only: bool = False


Skip = Callable[[int, str], None]


class ILAdapter(Protocol):
    level: str  # "mlil" or "hlil"

    def has_candidates(self) -> bool:
        """Cheap pre-check: does the function mix arithmetic and bitwise operations at all?"""

    def candidates(self, skip: Skip) -> Iterator[Candidate]:
        """Every candidate whose lift is an MBA, last first (a candidate before the definitions it
        inlines), lifted lazily; roots split by effects are reported to `skip`."""

    def shallow(self, candidate: Candidate, keep: Optional[frozenset] = None) -> Optional[Candidate]:
        """Optional. The same root lifted with the definitions in `keep` (default: those that other
        instructions also use, which stay visible) left as leaves where they are live; None if that
        changes nothing or is not possible."""

    def replace(self, candidate: Candidate, replacement: Expr) -> None:
        """Lower `replacement` and put it in place of the candidate's root, keeping a backup."""

    def finish(self) -> None:
        """Finalize the IL and regenerate SSA after a batch of replacements or removals."""

    def dead_assignments(self) -> set[Hashable]:
        """Pure assignments whose value is never used (shared policy: binja_d810.binja.deadcode)."""

    def remove(self, dead: set[Hashable]) -> None:
        """Remove those assignments, keeping backups."""

    def cfg_shape(self) -> tuple:
        """A value that changes if and only if the function's control flow changes."""

    def restore(self) -> None:
        """Undo every replacement and removal made through this adapter (pass-level rollback)."""
