"""The Tier 1 rule index must never change which rule wins (v2.1 Task 5)."""

import random

from exprgen import DEFAULT_OPS, random_expr

from binja_d810.ir.expr import BinOp, Const, UnOp, Var, walk
from binja_d810.ir.normalize import normalize
from binja_d810.rules.instantiate import instantiate
from binja_d810.rules.registry import load_rules
from binja_d810.simplify import RuleIndex, _best_rule

RULES = load_rules()


def _targets():
    rng = random.Random(5)
    out = [normalize(random_expr(rng, ops=DEFAULT_OPS + ("shl",))) for _ in range(150)]
    for rule in RULES:  # every rule's own pattern, as the corpus has them
        for width in (8, 32):
            e = instantiate(rule.pattern, width)
            if e is not None:
                out.append(normalize(e))
    out += [Const(5, 32), UnOp("not", Var("x", 32), 32), UnOp("neg", Var("x", 32), 32),
            BinOp("mul", Const(4, 32), Var("x", 32), 32)]
    return out


def test_index_and_full_scan_pick_the_same_rule():
    index = RuleIndex(RULES)
    checked = 0
    for target in _targets():
        for node in walk(target):
            full = _best_rule(node, RULES, set(), [], frozenset())
            indexed = _best_rule(node, index.candidates(node), set(), [], frozenset())
            assert (full and (full.source, full.replacement)) == (indexed and (indexed.source, indexed.replacement)), node
            checked += 1
    assert checked > 1000


def test_rules_without_a_pattern_are_always_candidates():
    class Opaque:
        name = "opaque"

        def match(self, e):
            return None

    rule = Opaque()
    assert rule in RuleIndex([rule]).candidates(Var("x", 32))
