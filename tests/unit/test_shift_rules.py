"""d810 rules with pattern-constant shift counts (v2.1 Task 4).

CstSimplificationRule7/15/19 were proven with Z3's shifts, which never reduce the count; Binary
Ninja's IL reduces it modulo max(32, width). The two agree for counts below the width, so the
rules may only fire there.
"""

import pytest

from binja_d810.ir.expr import BinOp, Const, Var
from binja_d810.ir.normalize import normalize
from binja_d810.rules.registry import load_rules
from binja_d810.semantic.verify import Verdict, check_equivalent

RULES = {r.name: r for r in load_rules()}
x = Var("x", 32)


def shr(e, c, w=32):
    return BinOp("lshr", e, Const(c, w), w)


@pytest.mark.parametrize("count,fires", [(1, True), (31, True), (32, False), (33, False), (40, False)])
def test_rule7_fires_only_below_the_width(count, fires):
    e = normalize(shr(BinOp("and", x, Const(0xF0F0, 32), 32), count))
    rewrite = RULES["CstSimplificationRule7"].match(e)
    assert (rewrite is not None) is fires
    if rewrite is not None:
        assert check_equivalent(e, rewrite.replacement) is not Verdict.REFUTED


@pytest.mark.parametrize("c1,c2,fires", [(3, 4, True), (16, 15, True), (16, 16, False), (20, 20, False)])
def test_rule15_needs_the_combined_count_below_the_width(c1, c2, fires):
    e = normalize(shr(shr(x, c1), c2))
    rewrite = RULES["CstSimplificationRule15"].match(e)
    assert (rewrite is not None) is fires
    if rewrite is not None:
        assert check_equivalent(e, rewrite.replacement) is not Verdict.REFUTED
