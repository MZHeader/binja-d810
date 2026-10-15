#!/usr/bin/env python3
"""Generate MBA test samples: C source plus a manifest of expected simplifications.

Usage: uv run python tests/samples/gen_mba.py --out tests/samples/build
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from binja_d810.ir.expr import BinOp, Const, Expr, UnOp, Var, is_mba, ops, substitute, to_str, variables  # noqa: E402
from binja_d810.ir.normalize import normalize  # noqa: E402
from binja_d810.ir.parse import parse  # noqa: E402
from binja_d810.rules.instantiate import instantiate  # noqa: E402
from binja_d810.rules.registry import load_rules  # noqa: E402
from binja_d810.semantic.verify import Verdict, check_equivalent  # noqa: E402

SEED = 1810
PARAMS = ("x", "y", "z")
CTYPE = {32: "uint32_t", 64: "uint64_t"}
SUFFIX = {32: "u", 64: "ull"}
SYMBOL = {"add": "+", "sub": "-", "mul": "*", "and": "&", "or": "|", "xor": "^", "shl": "<<", "lshr": ">>"}
C_OPS = frozenset(SYMBOL) | {"neg", "not"}

ZERO_IDENTITIES = [  # (simple, complex) with simple == complex for all x, y
    ("x + y", "(x ^ y) + 2 * (x & y)"),
    ("x + y", "(x | y) + (x & y)"),
    ("x ^ y", "(x | y) - (x & y)"),
    ("x | y", "(x & ~y) + y"),
    ("x & y", "(x | y) - (x ^ y)"),
    ("x - y", "(x ^ y) - 2 * (~x & y)"),
    ("~x", "-x - 1"),
    ("x", "(x & y) + (x & ~y)"),
]
TARGETS = ["x + y", "x - y", "x ^ y", "x | y", "x & y", "x + y + z", "x ^ y ^ z", "(x & y) | z", "2 * x + 3 * y", "x - (y & z)"]
POLY = [
    ("(x & y) * (x | y) + (x & ~y) * (~x & y)", "x * y"),
    ("(x & y) * (x | y) + (x & ~y) * (~x & y) + (x | y) - (x & y)", "x * y + (x ^ y)"),
]


@dataclass(frozen=True)
class Sample:
    name: str
    category: str
    width: int
    obfuscated: Expr
    expected: Expr


def _linear(rng: random.Random, width: int) -> tuple[Expr, Expr]:
    target_text = rng.choice(TARGETS)
    target = parse(target_text, width)
    complex_forms = [c for s, c in ZERO_IDENTITIES if s == target_text]
    obf = parse(rng.choice(complex_forms), width) if complex_forms else target
    for _ in range(rng.randint(2, 4)):
        simple, complex_ = rng.choice(ZERO_IDENTITIES)
        first, second = rng.sample(PARAMS, 2)
        mapping = {"x": Var(first, width), "y": Var(second, width)}
        zero = BinOp("sub", substitute(parse(complex_, width), mapping), substitute(parse(simple, width), mapping), width)
        coeff = rng.choice([1, 2, 3, 5, 7, -1, -3, 0x1337])
        obf = BinOp("add", obf, BinOp("mul", Const(coeff, width), zero, width), width)
    return obf, target


def _c_ok(e: Expr) -> bool:
    return ops(e) <= C_OPS and all(v.name in PARAMS for v in variables(e))


def generate() -> list[Sample]:
    rng = random.Random(SEED)
    samples: list[Sample] = []
    for rule in load_rules():
        if 32 not in rule.widths:
            continue
        obf, repl = instantiate(rule.pattern, 32), instantiate(rule.replacement, 32)
        if obf is None or repl is None or not _c_ok(obf) or not is_mba(normalize(obf)):
            continue
        if is_mba(normalize(repl)):  # rule output is still MBA: not a deobfuscation sample
            continue
        if rule.match(normalize(obf)) is None:
            continue
        samples.append(Sample(f"mba_d810_{rule.name}", "d810", 32, obf, normalize(repl)))
    for i in range(50):
        width = 64 if i % 5 == 4 else 32
        obf, target = _linear(rng, width)
        if is_mba(normalize(obf)):
            samples.append(Sample(f"mba_linear_{i:03d}", "linear", width, obf, normalize(target)))
    for i, (obf_text, expected_text) in enumerate(POLY):
        samples.append(Sample(f"mba_poly_{i}", "poly", 32, parse(obf_text, 32), normalize(parse(expected_text, 32))))
    for s in samples:
        if check_equivalent(s.obfuscated, s.expected, use_z3=False) is Verdict.REFUTED:
            raise AssertionError(f"generator bug: {s.name}")
    return samples


def _bitwise_only(e: Expr) -> bool:
    if isinstance(e, (Var, Const)):
        return True
    if isinstance(e, UnOp):
        return e.op == "not" and _bitwise_only(e.operand)
    return e.op in ("and", "or", "xor") and _bitwise_only(e.left) and _bitwise_only(e.right)


def _plain(e: Expr, width: int) -> str:
    if isinstance(e, Var):
        return e.name
    if isinstance(e, Const):
        return f"0x{e.value:x}{SUFFIX[width]}"
    if isinstance(e, UnOp):
        return f"(~{_plain(e.operand, width)})"
    return f"({_plain(e.left, width)} {SYMBOL[e.op]} {_plain(e.right, width)})"


def _c(e: Expr, temps: list[str], width: int) -> str:
    if isinstance(e, (Var, Const)):
        return _plain(e, width)
    if _bitwise_only(e):  # each bitwise subterm goes through an optimization barrier
        temps.append(_plain(e, width))
        return f"t{len(temps) - 1}"
    if isinstance(e, UnOp):
        return f"({'-' if e.op == 'neg' else '~'}{_c(e.operand, temps, width)})"
    return f"({_c(e.left, temps, width)} {SYMBOL[e.op]} {_c(e.right, temps, width)})"


def emit_function(s: Sample) -> str:
    t = CTYPE[s.width]
    temps: list[str] = []
    body = _c(s.obfuscated, temps, s.width)
    lines = [f"EXPORT {t} {s.name}({t} x, {t} y, {t} z) {{", "    (void)x; (void)y; (void)z;"]
    lines += [f"    {t} t{i} = {text}; B(t{i});" for i, text in enumerate(temps)]
    lines += [f"    return {body};", "}", ""]
    return "\n".join(lines)


def emit_c(samples: list[Sample]) -> str:
    header = (
        "// Generated by tests/samples/gen_mba.py. Do not edit.\n"
        "#include <stdint.h>\n"
        '#define B(v) __asm__ volatile("" : "+r"(v))\n'
        '#define EXPORT __attribute__((noinline, used, visibility("default")))\n\n'
    )
    return header + "\n".join(emit_function(s) for s in samples)


def write(out_dir: Path) -> None:
    samples = generate()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mba.c").write_text(emit_c(samples))
    manifest = [
        {"name": s.name, "category": s.category, "width": s.width,
         "obfuscated": to_str(normalize(s.obfuscated)), "expected": to_str(s.expected)}
        for s in samples
    ]
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {len(samples)} samples to {out_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    write(parser.parse_args().out)
