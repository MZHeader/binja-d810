"""Locates a usable CoBRA MBA solver."""

from __future__ import annotations

import ast
import random
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from typing import Callable, Optional

_SOLVE_LOCK = threading.Lock()

Solver = Callable[[str, int], Optional[str]]

# Solver budget per call. Tier 2 only sends expressions with at most Limits.max_vars (4) leaves.
MAX_VARS = 16
TIME_LIMIT_MS = 1000

# The operators CoBRA accepts. Shifts and extensions are refused (the caller gets None).
_BINOPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^"}
# Binding strength shared by C, Python and binja_d810.ir.parse (higher binds tighter).
_PRECEDENCE = {"|": 1, "^": 2, "&": 3, "+": 4, "-": 4, "*": 5}
_ASSOCIATIVE = frozenset("+*&|^")


@lru_cache(maxsize=1)
def load() -> Optional[Solver]:
    for finder in (_python_binding, _cli):
        solver = finder()
        if solver is not None:
            return solver
    return None


def _python_binding() -> Optional[Solver]:
    """The in-process binding shipped in the d810-cobra wheel (route A).

    `_cobra.simplify_budgeted` takes the expression's signature (its value at every 0/1
    assignment of the leaves) plus the original tree, and returns a simplified tree or None.
    """
    try:
        from d810_cobra import _cobra
        from d810_cobra.expr import evaluate, signature_of
    except ImportError:
        return None

    def solve(expr: str, bitwidth: int, deadline=None) -> Optional[str]:
        try:
            mask = (1 << bitwidth) - 1
            tree = _to_tree(ast.parse(expr, mode="eval").body, mask)
            tree, names = _drop_unused(tree, sorted(_names(tree)), bitwidth, evaluate)
            signature = signature_of(tree, names, bitwidth)
            # Binary Ninja analyzes on parallel threads; the binding was built for IDA. Waiting for
            # the lock counts against the caller's deadline and gives up when it passes.
            if not _acquire(deadline):
                return None
            try:
                limit = _time_limit_ms(deadline)
                if limit is None:
                    return None
                solved, _expired = _cobra.simplify_budgeted(signature, names, bitwidth, tree, MAX_VARS, limit, None)
            finally:
                _SOLVE_LOCK.release()
        except Exception:
            return None
        return None if solved is None else _render(solved)

    return solve


_WAIT = threading.local()


def lock_wait_seconds() -> float:
    """Seconds this thread has spent waiting for the solver lock (see reset_lock_wait)."""
    return getattr(_WAIT, "seconds", 0.0)


def reset_lock_wait() -> None:
    _WAIT.seconds = 0.0


def _acquire(deadline) -> bool:
    start = time.monotonic()
    if deadline is None:
        acquired = _SOLVE_LOCK.acquire()
    else:
        acquired = _SOLVE_LOCK.acquire(timeout=deadline.remaining()) if deadline.remaining() > 0 else \
            _SOLVE_LOCK.acquire(blocking=False)
    _WAIT.seconds = lock_wait_seconds() + time.monotonic() - start
    return acquired


def _time_limit_ms(deadline) -> Optional[int]:
    """The solver's own limit, clamped to what is left; None if nothing is."""
    if deadline is None:
        return TIME_LIMIT_MS
    remaining_ms = int(deadline.remaining() * 1000)
    return None if remaining_ms < 1 else min(TIME_LIMIT_MS, remaining_ms)


def _cli() -> Optional[Solver]:
    exe = shutil.which("cobra-cli")
    if exe is None:
        return None

    def solve(expr: str, bitwidth: int, deadline=None) -> Optional[str]:
        limit = _time_limit_ms(deadline)
        if limit is None:
            return None
        try:
            done = subprocess.run(
                [exe, "--mba", expr, "--bitwidth", str(bitwidth)],
                capture_output=True, text=True, timeout=min(10.0, limit / 1000 + 0.05),
            )
        except subprocess.TimeoutExpired:
            return None
        out = done.stdout.strip()
        return out if done.returncode == 0 and out else None

    return solve


def _to_tree(node: ast.AST, mask: int) -> dict:
    """Python expression AST -> d810_cobra tree dict (see d810_cobra.expr).

    Constants are masked to the width: the binding takes them as uint64 and raises on negatives.
    """
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return {"kind": "const", "value": node.value & mask}
    if isinstance(node, ast.Name):
        return {"kind": "var", "name": node.id}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Invert, ast.USub)):
        inner = _to_tree(node.operand, mask)
        if isinstance(node.op, ast.USub) and inner["kind"] == "const":
            return {"kind": "const", "value": -inner["value"] & mask}
        return {"kind": "un", "op": "~" if isinstance(node.op, ast.Invert) else "-", "a": inner}
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return {"kind": "bin", "op": _BINOPS[type(node.op)],
                "a": _to_tree(node.left, mask), "b": _to_tree(node.right, mask)}
    raise ValueError(f"CoBRA cannot take {ast.dump(node)}")


def _names(tree: dict) -> set[str]:
    if tree["kind"] == "var":
        return {tree["name"]}
    return set().union(*(_names(tree[k]) for k in ("a", "b") if k in tree))


def _drop_unused(tree: dict, names: list[str], bitwidth: int, evaluate) -> tuple[dict, list[str]]:
    """Replace variables the expression ignores with 0 and drop them from `names`.

    CoBRA drops such variables itself, renumbers the rest, and the binding then names the
    survivors by position: `v1 + (v0 & 0)` comes back as `v0`. Measured on d810-cobra 0.1.7.
    Unused is decided by full-width random evaluation, not the 0/1 signature, so `v0 & 256`
    (invisible to the signature) is kept, exactly as CoBRA keeps it.
    """
    mask = (1 << bitwidth) - 1
    rng = random.Random(0)
    envs = [{n: rng.getrandbits(bitwidth) for n in names} for _ in range(16)]
    unused = {
        name for name in names
        if all(evaluate(tree, env, mask) == evaluate(tree, {**env, name: rng.getrandbits(bitwidth)}, mask)
               for env in envs)
    }
    if not unused:
        return tree, names
    return _zero(tree, unused), [n for n in names if n not in unused]


def _zero(tree: dict, unused: set[str]) -> dict:
    if tree["kind"] == "var":
        return {"kind": "const", "value": 0} if tree["name"] in unused else tree
    return {**tree, **{k: _zero(tree[k], unused) for k in ("a", "b") if k in tree}}


def _render(tree: dict) -> str:
    """d810_cobra tree dict -> C-like text with only the parentheses precedence needs."""
    kind = tree["kind"]
    if kind == "const":
        value = tree["value"]
        return str(value) if value >= 0 else f"({value})"
    if kind == "var":
        return tree["name"]
    if kind == "un":
        inner = _render(tree["a"])
        return tree["op"] + (f"({inner})" if tree["a"]["kind"] == "bin" else inner)
    op = tree["op"]
    return f"{_operand(tree['a'], op, right=False)} {op} {_operand(tree['b'], op, right=True)}"


def _operand(tree: dict, parent_op: str, *, right: bool) -> str:
    text = _render(tree)
    if tree["kind"] != "bin":
        return text
    parent, child = _PRECEDENCE[parent_op], _PRECEDENCE[tree["op"]]
    same_chain = tree["op"] == parent_op and parent_op in _ASSOCIATIVE
    if child < parent or (right and child == parent and not same_chain):
        return f"({text})"
    return text
