import pytest

from binja_d810.ir.expr import node_count, to_str
from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse
from binja_d810.rules.d810_adapter import D810Rule
from binja_d810.rules.instantiate import instantiate
from binja_d810.rules.registry import instance, load_rules
from binja_d810.semantic.verify import Verdict, check_equivalent

RULES = {r.name: r for r in load_rules()}

# Rules that cannot match their own instantiated pattern, each with a reason. Keep empty unless justified.
SELF_MATCH_XFAIL: dict[str, str] = {}


def ir(text, width=32):
    return normalize(parse(text, width))


def test_hackers_delight_xor():
    rw = RULES["Xor_HackersDelightRule_1"].match(ir("(a | b) - (a & b)"))
    assert rw is not None
    assert rw.replacement == ir("a ^ b")
    assert rw.source == "Xor_HackersDelightRule_1"


def test_rule_only_fires_at_proven_widths():
    rule = D810Rule(instance("Xor_HackersDelightRule_1"), widths=[32])
    assert rule.match(ir("(a | b) - (a & b)", 64)) is None


def test_defining_constraints_compute_constants():
    rule = RULES.get("CstSimplificationRule3")
    if rule is None:
        pytest.skip("CstSimplificationRule3 not proven in manifest")
    target = ir("(a - 3) + 5 * (a - 7)")
    rw = rule.match(target)
    assert rw is not None
    assert rw.replacement == ir("6 * a - 38")


def test_instantiate_resolves_bnot_leaves_in_both_d810_spellings():
    from binja_d810.ir.expr import UnOp, Var
    from binja_d810.third_party.d810.mba.dsl import Var as PVar

    z_not = UnOp("not", Var("z", 32), 32)
    assert instantiate(PVar("bnot_x_2"), 32) == z_not
    assert instantiate(PVar("bnot_x2"), 32) == z_not  # Xor_Rule_2/3 spell it without the underscore


def test_disabled_rules_are_not_loaded():
    assert "Xor_HackersDelightRule_1" not in {r.name for r in load_rules(disabled={"Xor_HackersDelightRule_1"})}


@pytest.mark.parametrize("name", sorted(RULES))
def test_rule_matches_its_own_instantiated_pattern(name):
    if name in SELF_MATCH_XFAIL:
        pytest.xfail(SELF_MATCH_XFAIL[name])
    rule = RULES[name]
    width = 32 if 32 in rule.widths else max(rule.widths)
    raw = instantiate(rule.pattern, width)
    if raw is None:
        pytest.skip("pattern needs constants satisfying its constraints")
    target = normalize(raw)
    expected = instantiate(rule.replacement, width)
    if expected is not None and node_count(target) <= node_count(normalize(expected)):
        pytest.skip("normalize() already simplifies this pattern")
    rw = rule.match(target)
    assert rw is not None, f"{name} did not match {to_str(target)}"
    assert check_equivalent(target, rw.replacement) is not Verdict.REFUTED
