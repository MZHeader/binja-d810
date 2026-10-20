"""The contract between the IL-independent engine and one IL level."""

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
    # The root lifted with shared values counted once (v3.1), when its tree is over maxNodes: only
    # a proven constant may come from it (constancy.fold_shared), and no tree algorithm touches it.
    shared_dag: Optional[Expr] = None
    # The statement HLIL shows the root in, beyond the root itself (v3.1): the kinds ("arith",
    # "bitwise") of the statements its value flows into through single uses, None if unknown, and
    # the kinds of the root's own statement. A replacement must not make that statement mixed.
    consumer_kinds: Optional[frozenset] = None
    root_kinds: frozenset = frozenset()
    # A condition over merged values that is no candidate itself (k != 0 with k = phi(...)) but whose
    # per-path variants are: offered constant-only with them, as alternative sets of
    # variants, cheapest first (ILAdapter.path_variants).
    paths: Optional[tuple] = None


Skip = Callable[..., None]  # skip(address, reason, expression=None): a root left as it is, and why


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

    def path_variants(self, candidate: Candidate) -> Optional[list[list[Expr]]]:
        """Optional. Alternative sets of a condition's lift once per incoming path of
        the merged values it reads (binja_d810.ir.paths), cheapest first; the engine folds it if every
        variant of one set is the same proven constant. None when there is nothing to split."""

    def cfg_shape(self) -> tuple:
        """A value that changes if and only if the function's control flow changes."""

    def restore(self) -> None:
        """Undo every replacement and removal made through this adapter (pass-level rollback)."""
