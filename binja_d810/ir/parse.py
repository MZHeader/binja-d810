"""Parse C-like expression text into IR (tests, sample manifests, CoBRA output)."""

from __future__ import annotations

import re
from typing import Mapping, Optional

from .expr import CMP_OPS, DIV_OPS, BinOp, Const, Expr, UnOp, Var

_TOKEN = re.compile(r"\s*(?:(0[xX][0-9a-fA-F]+|\d+)|([A-Za-z_][A-Za-z0-9_#@]*)|(<<|>>|[-+*&|^~(),]))")
_LEVELS = [("|",), ("^",), ("&",), ("<<", ">>"), ("+", "-"), ("*",)]
_BINARY = {"|": "or", "^": "xor", "&": "and", "<<": "shl", ">>": "lshr", "+": "add", "-": "sub", "*": "mul"}
_EXTENSION = re.compile(r"(zx|sx|trunc)(\d+)$")


class ParseError(ValueError):
    pass


def parse(text: str, width: int, var_widths: Optional[Mapping[str, int]] = None) -> Expr:
    tokens = _tokenize(text)
    parser = _Parser(tokens)
    tree = parser.expr(0)
    if parser.pos != len(tokens):
        raise ParseError(f"unexpected {tokens[parser.pos]!r} in {text!r}")
    return _build(tree, width, var_widths or {})


def _tokenize(text: str) -> list[str]:
    tokens, pos, text = [], 0, text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            raise ParseError(f"bad character at {pos} in {text!r}")
        tokens.append(m.group(m.lastindex))
        pos = m.end()
    return tokens


def _number(token: str) -> int:
    return int(token, 16) if token.lower().startswith("0x") else int(token, 10)


class _Parser:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, expected: Optional[str] = None) -> str:
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise ParseError(f"expected {expected or 'a token'}, got {token!r}")
        self.pos += 1
        return token

    def expr(self, level: int):
        if level == len(_LEVELS):
            return self.unary()
        node = self.expr(level + 1)
        while self.peek() in _LEVELS[level]:
            symbol = self.take()
            node = ("bin", symbol, node, self.expr(level + 1))
        return node

    def unary(self):
        if self.peek() in ("-", "~"):
            symbol = self.take()
            following = self.peek()
            if symbol == "-" and following is not None and following[0].isdigit():
                return ("num", -_number(self.take()))  # only a literal directly after '-' folds
            return ("un", symbol, self.unary())
        return self.primary()

    def primary(self):
        token = self.take()
        if token == "(":
            node = self.expr(0)
            self.take(")")
            return node
        if token[0].isdigit():
            return ("num", _number(token))
        if not (token[0].isalpha() or token[0] == "_"):
            raise ParseError(f"unexpected {token!r}")
        if self.peek() == "(":
            self.take("(")
            args = [self.expr(0)]
            while self.peek() == ",":
                self.take(",")
                args.append(self.expr(0))
            self.take(")")
            return ("call", token, args)
        return ("var", token)


def _natural_width(node, var_widths: Mapping[str, int]) -> Optional[int]:
    kind = node[0]
    if kind == "num":
        return None
    if kind == "var":
        return var_widths.get(node[1])
    if kind == "un":
        return _natural_width(node[2], var_widths)
    if kind == "call":
        m = _EXTENSION.match(node[1])
        if m:
            return int(m.group(2))
        if node[1] in CMP_OPS:
            return 1
        return _natural_width(node[2][0], var_widths)
    if node[1] in ("<<", ">>"):
        return _natural_width(node[2], var_widths)
    return _natural_width(node[2], var_widths) or _natural_width(node[3], var_widths)


def _build(node, width: int, var_widths: Mapping[str, int]) -> Expr:
    kind = node[0]
    if kind == "num":
        return Const(node[1], width)
    if kind == "var":
        return Var(node[1], var_widths.get(node[1], width))
    if kind == "un":
        return UnOp("neg" if node[1] == "-" else "not", _build(node[2], width, var_widths), width)
    if kind == "call":
        name, args = node[1], node[2]
        if name in CMP_OPS and len(args) == 2:
            # Operands of the width either one has, else 32 bits.
            inner = _natural_width(args[0], var_widths) or _natural_width(args[1], var_widths) or 32
            try:
                return BinOp(name, _build(args[0], inner, var_widths), _build(args[1], inner, var_widths), 1)
            except ValueError as exc:
                raise ParseError(str(exc)) from exc
        if (name in ("sar", "rol", "ror") or name in DIV_OPS) and len(args) == 2:
            op = "ashr" if name == "sar" else name
            return BinOp(op, _build(args[0], width, var_widths), _build(args[1], width, var_widths), width)
        m = _EXTENSION.match(name)
        if m is None or len(args) != 1:
            raise ParseError(f"unknown function {name}")
        if int(m.group(2)) != width:
            raise ParseError(f"{name} used where a {width}-bit value is expected")
        inner = _natural_width(args[0], var_widths)
        if inner is None:
            raise ParseError(f"cannot infer operand width of {name}")
        try:
            return UnOp(m.group(1), _build(args[0], inner, var_widths), width)
        except ValueError as exc:
            raise ParseError(str(exc)) from exc
    return BinOp(_BINARY[node[1]], _build(node[2], width, var_widths), _build(node[3], width, var_widths), width)
