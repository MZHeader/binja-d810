"""Independent final-IL oracle: what does the function's final HLIL compute?

This module imports nothing from binja_d810. It reads the final HLIL of a straight-line function,
follows its assignments, and builds the returned value as an expression over the parameters with
its own evaluator and its own Z3 encoding; the reference answer is parsed with Python's `ast`.

Parameters are modelled as 64-bit values whose upper bits are unknown: a read of a narrow
parameter, or a field read of it (`x.d`), takes the corresponding bits. A final HLIL that depends
on bits the reference does not use is therefore reported as a mismatch, never assumed away.

Shift counts follow Binary Ninja's IL as measured with its own dataflow (count modulo
max(32, width); tests/binja/test_shift_reference.py).
"""

from __future__ import annotations

import ast
import itertools
import random
from dataclasses import dataclass
from typing import Optional

from binaryninja import HighLevelILOperation as H

PARAM_BITS = 64
_BINARY = {H.HLIL_ADD: "add", H.HLIL_SUB: "sub", H.HLIL_MUL: "mul", H.HLIL_AND: "and", H.HLIL_OR: "or",
           H.HLIL_XOR: "xor", H.HLIL_LSL: "shl", H.HLIL_LSR: "lshr", H.HLIL_ASR: "ashr", H.HLIL_ROL: "rol",
           H.HLIL_ROR: "ror"}
_COMPARE = {H.HLIL_CMP_E: "eq", H.HLIL_CMP_NE: "ne", H.HLIL_CMP_ULT: "ult", H.HLIL_CMP_ULE: "ule",
            H.HLIL_CMP_UGT: "ugt", H.HLIL_CMP_UGE: "uge", H.HLIL_CMP_SLT: "slt", H.HLIL_CMP_SLE: "sle",
            H.HLIL_CMP_SGT: "sgt", H.HLIL_CMP_SGE: "sge"}  # 0 or 1, at the operands' width
# Division (v3.2), with values for the cases hardware traps on that both sides of a comparison share:
# x / 0 = 0, x % 0 = x, MIN / -1 = MIN (the plugin only rewrites divisions proven never to trap).
_DIVIDE = {H.HLIL_DIVU: "udiv", H.HLIL_DIVS: "sdiv", H.HLIL_MODU: "urem", H.HLIL_MODS: "srem"}
_DIVIDE_DP = {H.HLIL_DIVU_DP: "udiv", H.HLIL_DIVS_DP: "sdiv", H.HLIL_MODU_DP: "urem", H.HLIL_MODS_DP: "srem"}
_UNARY = {H.HLIL_NEG: "neg", H.HLIL_NOT: "not", H.HLIL_ZX: "zx", H.HLIL_SX: "sx", H.HLIL_LOW_PART: "low"}
_SKIP = {H.HLIL_NOP, H.HLIL_VAR_DECLARE}


class Unvalidated(Exception):
    """The final HLIL uses something the oracle does not model."""


# Oracle expressions are tuples: ("param", name) is a 64-bit parameter; ("const", value, width);
# ("bits", expr, offset_bits, width); ("op", name, width, *args).

def function_result(func) -> tuple:
    """The value the function returns, as an oracle expression. Raises Unvalidated."""
    hlil = func.hlil
    if hlil is None:
        raise Unvalidated("no HLIL")
    params = {v.identifier for v in func.parameter_vars}
    env: dict[int, tuple] = {}
    result = None
    for statement in _statements(hlil.root):
        op = statement.operation
        if op in _SKIP:
            continue
        if result is not None:
            raise Unvalidated(f"statement after return: {statement}")
        if op == H.HLIL_DEREF:
            continue  # a load whose value is unused (OLLVM's global inputs once its predicate is folded)
        if op in (H.HLIL_VAR_INIT, H.HLIL_ASSIGN) and statement.src.operation == H.HLIL_DEREF:
            # A loaded value: unknown, like an uninitialized read (see _read).
            dest = statement.dest if op == H.HLIL_VAR_INIT else getattr(statement.dest, "var", None)
            if dest is not None:
                env[dest.identifier] = ("memory", str(statement.src))
                continue
        if op == H.HLIL_VAR_INIT:
            env[statement.dest.identifier] = _expr(statement.src, env, params)
        elif op == H.HLIL_ASSIGN and statement.dest.operation == H.HLIL_VAR:
            env[statement.dest.var.identifier] = _expr(statement.src, env, params)
        elif op == H.HLIL_ASSIGN and statement.dest.operation == H.HLIL_SPLIT:
            high,low=statement.dest.high,statement.dest.low
            if high.operation!=H.HLIL_VAR or low.operation!=H.HLIL_VAR:
                raise Unvalidated('non-variable split assignment')
            value=_fit(_expr(statement.src,env,params),(high.size+low.size)*8,statement)
            env[low.var.identifier]=('bits',value,0,low.size*8)
            env[high.var.identifier]=('bits',value,low.size*8,high.size*8)
        elif (op == H.HLIL_ASSIGN and statement.dest.operation == H.HLIL_STRUCT_FIELD
              and statement.dest.src.operation == H.HLIL_VAR and statement.dest.member_index is None):
            # var_8:5.b = value: replace those bytes of the variable, keep the others.
            var = statement.dest.src.var
            old = _read(var, 0, var.type.width * 8, env, params)
            value = _fit(_expr(statement.src, env, params), statement.dest.size * 8, statement)
            env[var.identifier] = _merge(old, value, statement.dest.offset * 8)

        elif op == H.HLIL_RET and len(statement.src) == 1:
            result = _expr(statement.src[0], env, params)
        else:
            raise Unvalidated(f"unsupported statement {op.name}: {statement}")
    if result is None:
        raise Unvalidated("no return value")
    return result


def _statements(node):
    if node.operation == H.HLIL_BLOCK:
        for item in node.body:
            yield from _statements(item)
    elif node.operation in (H.HLIL_IF, H.HLIL_WHILE, H.HLIL_DO_WHILE, H.HLIL_FOR, H.HLIL_SWITCH):
        raise Unvalidated(f"control flow: {node.operation.name}")
    else:
        yield node


def _expr(e, env, params) -> tuple:
    op, width = e.operation, e.size * 8 or 1
    if op in (H.HLIL_CONST, H.HLIL_CONST_PTR):
        return ("const", e.constant & ((1 << width) - 1), width)
    if op == H.HLIL_VAR:
        return _read(e.var, 0, width, env, params)
    if op == H.HLIL_STRUCT_FIELD and e.member_index is None:
        backing,offset=e,0
        while backing.operation==H.HLIL_STRUCT_FIELD and backing.member_index is None:
            offset+=backing.offset;backing=backing.src
        if backing.operation==H.HLIL_VAR:
            # A nested storage view can be wider than an intermediate field (x.d.q).
            # Keep the backing variable's bits; never invent zero/sign extension.
            return _read(backing.var,offset,width,env,params)
    if op == H.HLIL_STRUCT_FIELD and e.member_index is None:  # y.w:1.b: a field of a field
        inner = _expr(e.src, env, params)
        if 8 * e.offset + width > _width(inner):
            raise Unvalidated(f"field beyond its value: {e}")
        return ("bits", inner, 8 * e.offset, width)
    if op in _BINARY:
        left, right = _expr(e.left, env, params), _expr(e.right, env, params)
        if op not in (H.HLIL_LSL, H.HLIL_LSR, H.HLIL_ASR, H.HLIL_ROL, H.HLIL_ROR):
            left, right = _fit(left, width, e), _fit(right, width, e)
        else:
            left = _fit(left, width, e)
        return ("op", _BINARY[op], width, left, right)
    if op in (H.HLIL_MULU_DP,H.HLIL_MULS_DP):
        extension="zx" if op==H.HLIL_MULU_DP else "sx"
        a,b=(_fit(_expr(v,env,params),width,e) for v in (e.left,e.right))
        return ("op","mul",2*width,("op",extension,2*width,a),("op",extension,2*width,b))
    if op in (H.HLIL_ADC,H.HLIL_SBB):
        a,b=(_fit(_expr(v,env,params),width,e) for v in (e.left,e.right))
        carry=_expr(e.carry,env,params)
        carry=("bits",carry,0,1) if _width(carry)>1 else carry
        if width>1:carry=("op","zx",width,carry)
        operation="add" if op==H.HLIL_ADC else "sub"
        return ("op",operation,width,("op",operation,width,a,b),carry)
    if op in _DIVIDE:
        return ("op", _DIVIDE[op], width, _fit(_expr(e.left, env, params), width, e),
                _fit(_expr(e.right, env, params), width, e))
    if op in _DIVIDE_DP:
        # x86 div/idiv: a double-width dividend (edx:eax), the quotient or remainder at the operation's
        # width (only the low half is modelled; the plugin never rewrites one whose quotient overflows).
        name, wide = _DIVIDE_DP[op], 2 * width
        dividend = _expr(e.left, env, params)
        if _width(dividend) != wide:
            raise Unvalidated(f"{_width(dividend)}-bit dividend of a {width}-bit division: {e}")
        divisor = ("op", "zx" if name[0] == "u" else "sx", wide, _fit(_expr(e.right, env, params), width, e))
        return ("bits", ("op", name, wide, dividend, divisor), 0, width)
    if op in _UNARY:
        return ("op", _UNARY[op], width, _expr(e.src, env, params))
    if op == H.HLIL_BOOL_TO_INT:  # (x & 1) != 0 ? 1 : 0
        inner = _expr(e.src, env, params)
        have = _width(inner)
        return inner if have == width else ("bits", inner, 0, width) if have > width else ("op", "zx", width, inner)
    if op in _COMPARE:
        return ("op", _COMPARE[op], width, _fit(_expr(e.left, env, params), width, e),
                _fit(_expr(e.right, env, params), width, e))
    if op == H.HLIL_SPLIT:
        return ("op", "concat", width, _expr(e.high, env, params), _expr(e.low, env, params))
    raise Unvalidated(f"unsupported expression {op.name}: {e}")


def _fit(x: tuple, width: int, e) -> tuple:
    """An operand wider than its operation keeps its low bits; a narrower one is not modelled."""
    have = _width(x)
    if have == width:
        return x
    if have > width:
        return ("bits", x, 0, width)
    raise Unvalidated(f"{have}-bit operand in a {width}-bit operation: {e}")


def _read(var, offset: int, width: int, env, params) -> tuple:
    if var.identifier in env:
        value = env[var.identifier]
        if value[0] == "memory":  # an unknown loaded value: a result that depends on it is a mismatch
            return ("bits", ("param", f"?{value[1]}"), offset * 8, width)
        if offset == 0 and width == _width(value):
            return value
        if offset * 8 + width > _width(value):
            raise Unvalidated(f"read of {var.name} beyond its assigned value")
        return ("bits", value, offset * 8, width)
    if offset * 8 + width > PARAM_BITS:
        raise Unvalidated(f"read of {var.name} beyond {PARAM_BITS} bits")
    if var.identifier in params:
        return ("bits", ("param", var.name), offset * 8, width)
    # Read before any assignment (an uninitialized register, e.g. int64_t var_8 = rax at -O0): an
    # unknown value. The reference never uses it, so a result that depends on it is a mismatch.
    return ("bits", ("param", f"?{var.name}"), offset * 8, width)


def _merge(old: tuple, value: tuple, offset_bits: int) -> tuple:
    width, value_width = _width(old), _width(value)
    if offset_bits + value_width > width:
        raise Unvalidated("partial assignment beyond the variable")
    keep = ("const", ~(((1 << value_width) - 1) << offset_bits) & _mask(width), width)
    placed = ("op", "zx", width, value) if value_width < width else value
    if offset_bits:
        placed = ("op", "shl", width, placed, ("const", offset_bits, width))
    return ("op", "or", width, ("op", "and", width, old, keep), placed)


def _width(x: tuple) -> int:
    kind = x[0]
    if kind == "param":
        return PARAM_BITS
    if kind == "const":
        return x[2]
    if kind == "bits":
        return x[3]
    return x[2]


def _mask(width: int) -> int:
    return (1 << width) - 1


def _signed(value: int, width: int) -> int:
    value &= _mask(width)
    return value - (1 << width) if value >> (width - 1) else value


def evaluate(x: tuple, env: dict[str, int]) -> int:
    memo={}
    def read(node):
        key=id(node)
        if key not in memo:memo[key]=_evaluate(node,env,read)
        return memo[key]
    return read(x)


def _evaluate(x,env,read):
    kind = x[0]
    if kind == "param":
        return env[x[1]] & _mask(PARAM_BITS)
    if kind == "const":
        return x[1]
    if kind == "bits":
        return (read(x[1]) >> x[2]) & _mask(x[3])
    _, name, width, *args = x
    m = _mask(width)
    vals = [read(a) for a in args]
    if name == "neg":
        return -vals[0] & m
    if name == "not":
        return ~vals[0] & m
    if name == "zx":
        return vals[0] & m
    if name == "sx":
        return _signed(vals[0], _width(args[0])) & m
    if name == "low":
        return vals[0] & m
    if name == "concat":
        return ((vals[0] << _width(args[1])) | vals[1]) & m
    a, b = vals
    if name in _COMPARE.values():
        if name[0] == "s":
            a, b = _signed(a, width), _signed(b, width)
        return int({"eq": a == b, "ne": a != b, "ult": a < b, "ule": a <= b, "ugt": a > b, "uge": a >= b,
                    "slt": a < b, "sle": a <= b, "sgt": a > b, "sge": a >= b}[name])
    if name in ("udiv", "urem", "sdiv", "srem"):
        if b == 0:
            return 0 if name in ("udiv", "sdiv") else a
        if name[0] == "u":
            return a // b if name == "udiv" else a % b
        sa, sb = _signed(a, width), _signed(b, width)
        q = abs(sa) // abs(sb) * (1 if (sa < 0) == (sb < 0) else -1)  # toward zero
        return (q if name == "sdiv" else sa - q * sb) & m
    if name in ("rol", "ror"):  # count modulo the width (Binary Ninja's dataflow)
        c = b % width
        if c == 0:
            return a & m
        return ((a << c) | (a >> (width - c))) & m if name == "rol" else ((a >> c) | (a << (width - c))) & m
    if name in ("shl", "lshr", "ashr"):
        count = b % max(32, width)
        if name == "shl":
            return (a << count) & m
        if name == "lshr":
            return a >> count
        return (_signed(a, width) >> count) & m
    return {"add": a + b, "sub": a - b, "mul": a * b, "and": a & b, "or": a | b, "xor": a ^ b}[name] & m


def to_z3(x: tuple, symbols: dict):
    memo={}
    def read(node):
        key=id(node)
        if key not in memo:memo[key]=_to_z3(node,symbols,read)
        return memo[key]
    return read(x)


def _to_z3(x,symbols,read):
    import z3

    kind = x[0]
    if kind == "param":
        return symbols[x[1]]
    if kind == "const":
        return z3.BitVecVal(x[1], x[2])
    if kind == "bits":
        inner = read(x[1])
        return z3.Extract(x[2] + x[3] - 1, x[2], inner)
    _, name, width, *args = x
    vals = [read(a) for a in args]
    if name == "neg":
        return -vals[0]
    if name == "not":
        return ~vals[0]
    if name in ("zx", "sx", "low"):
        inner_width = vals[0].size()
        if name == "low" or inner_width >= width:
            return z3.Extract(width - 1, 0, vals[0]) if inner_width > width else vals[0]
        return (z3.ZeroExt if name == "zx" else z3.SignExt)(width - inner_width, vals[0])
    if name == "concat":
        return z3.Concat(vals[0], vals[1])
    a, b = vals
    if name in _COMPARE.values():
        cond = {"eq": lambda: a == b, "ne": lambda: a != b, "ult": lambda: z3.ULT(a, b), "ule": lambda: z3.ULE(a, b),
                "ugt": lambda: z3.UGT(a, b), "uge": lambda: z3.UGE(a, b), "slt": lambda: a < b, "sle": lambda: a <= b,
                "sgt": lambda: a > b, "sge": lambda: a >= b}[name]()
        return z3.If(cond, z3.BitVecVal(1, width), z3.BitVecVal(0, width))
    if name in ("udiv", "urem", "sdiv", "srem"):
        zero = z3.BitVecVal(0, width)
        if name == "udiv":
            return z3.If(b == zero, zero, z3.UDiv(a, b))
        if name == "sdiv":
            return z3.If(b == zero, zero, a / b)
        return z3.URem(a, b) if name == "urem" else z3.SRem(a, b)
    if name in ("rol", "ror"):
        bw = b.size()
        b = z3.ZeroExt(width - bw, b) if bw < width else z3.Extract(width - 1, 0, z3.URem(b, z3.BitVecVal(width, bw))) if bw > width else b
        b = z3.URem(b, z3.BitVecVal(width, width))
        return z3.RotateLeft(a, b) if name == "rol" else z3.RotateRight(a, b)
    if name in ("shl", "lshr", "ashr"):
        bw = b.size()
        b = b & z3.BitVecVal(max(32, width) - 1, bw)
        b = z3.ZeroExt(width - bw, b) if bw < width else z3.Extract(width - 1, 0, b) if bw > width else b
        return a << b if name == "shl" else z3.LShR(a, b) if name == "lshr" else a >> b
    return {"add": a + b, "sub": a - b, "mul": a * b, "and": a & b, "or": a | b, "xor": a ^ b}[name]


def reference(text: str, width: int) -> tuple:
    """Parse a C-like reference answer (+ - * & | ^ ~ << >> == != and parentheses) with Python's ast.
    Every name is a parameter read at `width` bits; a comparison is 0 or 1 at `width` bits."""
    def build(node) -> tuple:
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return ("const", node.value & _mask(width), width)
        if isinstance(node, ast.Name):
            return ("bits", ("param", node.id), 0, width)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.Invert)):
            return ("op", "neg" if isinstance(node.op, ast.USub) else "not", width, build(node.operand))
        if isinstance(node, ast.BinOp):
            names = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.BitAnd: "and", ast.BitOr: "or",
                     ast.BitXor: "xor", ast.LShift: "shl", ast.RShift: "lshr"}
            if type(node.op) in names:
                return ("op", names[type(node.op)], width, build(node.left), build(node.right))
        if (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], (ast.Eq, ast.NotEq))):
            name = "eq" if isinstance(node.ops[0], ast.Eq) else "ne"
            return ("op", name, width, build(node.left), build(node.comparators[0]))
        raise ValueError(f"unsupported reference syntax: {ast.dump(node)}")
    return build(ast.parse(text, mode="eval").body)


def params_of(*xs: tuple) -> list[str]:
    found: set[str] = set()
    stack = list(xs)
    seen=set()
    while stack:
        x = stack.pop()
        if id(x) in seen:continue
        seen.add(id(x))
        if x[0] == "param":
            found.add(x[1])
        elif x[0] == "bits":
            stack.append(x[1])
        elif x[0] == "op":
            stack.extend(x[3:])
    return sorted(found)


def used_bits(x: tuple) -> dict[str, int]:
    """Highest bit (exclusive) read of each parameter."""
    out: dict[str, int] = {}
    stack = [x]
    seen=set()
    while stack:
        item = stack.pop()
        if id(item) in seen:continue
        seen.add(id(item))
        if item[0] == "bits" and item[1][0] == "param":
            out[item[1][1]] = max(out.get(item[1][1], 0), item[2] + item[3])
        elif item[0] == "bits":
            stack.append(item[1])
        elif item[0] == "op":
            stack.extend(item[3:])
        elif item[0] == "param":
            out[item[1]] = PARAM_BITS
    return out


@dataclass(frozen=True)
class OracleResult:
    kind: str  # "proven", "exhaustive", "sampled", "mismatch"
    detail: str = ""


def compare(final: tuple, expected: tuple, width: int, *, z3_timeout_ms: int = 2000, samples: int = 2000,
            seed: int = 0) -> OracleResult:
    """Compare the low `width` bits of the final value with the reference."""
    final = ("bits", final, 0, width) if _width(final) != width else final
    names = params_of(final, expected)
    bits = {**used_bits(final)}
    for name, top in used_bits(expected).items():
        bits[name] = max(bits.get(name, 0), top)
    total = sum(bits.get(n, 0) for n in names)
    if total <= 16:
        for values in itertools.product(*(range(1 << bits.get(n, 0)) for n in names)):
            env = dict(zip(names, values))
            if evaluate(final, env) != evaluate(expected, env):
                return OracleResult("mismatch", f"at {env}")
        return OracleResult("exhaustive")
    rng = random.Random(seed)
    for _ in range(samples):
        env = {n: rng.getrandbits(PARAM_BITS) for n in names}
        if evaluate(final, env) != evaluate(expected, env):
            return OracleResult("mismatch", f"at {env}")
    try:
        import z3
    except ImportError:
        return OracleResult("sampled", "z3 unavailable")
    symbols = {n: z3.BitVec(n, PARAM_BITS) for n in names}
    solver = z3.Solver()
    solver.set("timeout", z3_timeout_ms)
    solver.add(to_z3(final, symbols) != to_z3(expected, symbols))
    verdict = solver.check()
    if verdict == z3.unsat:
        return OracleResult("proven")
    if verdict == z3.sat:
        return OracleResult("mismatch", f"z3 model {solver.model()}")
    return OracleResult("sampled", f"z3 {solver.reason_unknown()}")


def mutations(x: tuple) -> list[tuple]:
    """Single-point corruptions of an oracle expression: each constant + 1, and each binary op's
    operands swapped (for non-commutative ops) or replaced by its left operand."""
    out: list[tuple] = []

    def rebuild(node: tuple, path: list[int], replacement: tuple) -> tuple:
        if not path:
            return replacement
        head, rest = path[0], path[1:]
        items = list(node)
        items[head] = rebuild(node[head], rest, replacement)
        return tuple(items)

    def visit(node: tuple, path: list[int]) -> None:
        kind = node[0]
        if kind == "const":
            out.append(rebuild(x, path, ("const", (node[1] + 1) & _mask(node[2]), node[2])))
        elif kind == "bits":
            visit(node[1], path + [1])
        elif kind == "op":
            name, args = node[1], node[3:]
            if len(args) == 2:
                out.append(rebuild(x, path, args[0] if _width(args[0]) == node[2] else node))
                if name in ("sub", "shl", "lshr", "ashr"):
                    out.append(rebuild(x, path, ("op", name, node[2], args[1], args[0])))
            for i, arg in enumerate(args):
                visit(arg, path + [3 + i])

    visit(x, [])
    return [m for m in out if m != x]
