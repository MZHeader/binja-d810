"""Rules whose constraints name negative or width-relative constants (v2.1 Task 1)."""

import pytest

from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse
from binja_d810.rules.d810_adapter import D810Rule, ModInt
from binja_d810.rules.registry import instance
from binja_d810.semantic.verify import Verdict, check_equivalent


def test_modular_integers_compare_and_compute_modulo_the_width():
    c = ModInt(0xFE, 8)
    assert c == -2 and -2 == c and c != 2
    assert c + 3 == 1 and isinstance(c + 3, ModInt)
    assert ~c == 1 and -c == 2 and (c & 0xF) == 0xE and (c << 1) == 0xFC
    assert ModInt(0xFFFFFFFE, 32) == -2 and ModInt(0x7FFFFFFF, 32) + 1 == 0x80000000


@pytest.mark.parametrize("width", [8, 16, 32, 64])
@pytest.mark.parametrize("name,text,expected", [
    ("Add_OllvmRule_4", "(x ^ y) - (-2) * (x & y)", "x + y"),
    ("NegAdd_HackersDelightRule_1", "(-2) * (x | y) + (x ^ y)", "-(x + y)"),
    ("Xor_SpecialConstantRule_2", "(x + y) + (-2) * (x & y)", "x ^ y"),
    ("Sub1_FactorRule_1", "((-x) - 1) - (-2) * x", "x - 1"),
])
def test_constraints_with_negative_constants_hold_at_every_width(name, text, expected, width):
    rule = D810Rule(instance(name), [8, 16, 32, 64])
    e = normalize(parse(text, width))
    rewrite = rule.match(e)
    assert rewrite is not None, (name, width, e)
    assert check_equivalent(rewrite.replacement, normalize(parse(expected, width))) is Verdict.PROVEN


# --- offline per-width proofs ---------------------------------------------------------------

from binja_d810.rules.prove import prove_rule  # noqa: E402

THIRTEEN = ["Add_OllvmRule_2", "Add_OllvmRule_4", "Add_SpecialConstantRule_2", "Add_SpecialConstantRule_3",
            "Bnot_Hodur_1", "CstSimplificationRule14", "CstSimplificationRule20", "CstSimplificationRule21",
            "CstSimplificationRule22", "CstSimplificationRule3", "NegAdd_HackersDelightRule_1", "Sub1_FactorRule_1",
            "Xor_SpecialConstantRule_2"]


@pytest.mark.parametrize("name", ["Add_OllvmRule_4", "NegAdd_HackersDelightRule_1", "Xor_SpecialConstantRule_2"])
@pytest.mark.parametrize("width", [8, 16, 32, 64])
def test_width_independent_identities_are_proven_at_every_width(name, width):
    assert prove_rule(instance(name), width) == "proven"


def test_proof_agrees_with_d810_where_d810_could_prove():
    for name in ("Add_HackersDelightRule_2", "Xor_HackersDelightRule_1", "Or_HackersDelightRule_1"):
        for width in (8, 32):
            assert prove_rule(instance(name), width) == "proven", (name, width)


def test_a_wrong_identity_is_refuted():
    class Wrong:
        """(x ^ y) - c * (x & y) => x - y, c == -2: false."""

    rule = instance("Add_OllvmRule_4")
    from binja_d810.third_party.d810.mba.dsl import Var

    Wrong.pattern, Wrong.replacement = rule.pattern, Var("x_0") - Var("x_1")
    Wrong.CONSTRAINTS = rule.CONSTRAINTS
    Wrong.get_constraints = lambda self, z3_vars: []
    assert prove_rule(Wrong(), 32).startswith("refuted")


# --- constraint-satisfying instances (v2.1 Task 3) ------------------------------------------

from binja_d810.ir.expr import Const as IRConst, walk  # noqa: E402
from binja_d810.rules.instantiate import instantiate  # noqa: E402
from binja_d810.rules.prove import solve_constants  # noqa: E402


def test_constants_are_solved_from_the_constraints():
    rule = instance("CstSimplificationRule7")  # (x & c_1) >> c_2 => (x >> c_2) & c_res, c_res == c_1 >> c_2
    constants = solve_constants(rule, 32)
    assert constants is not None and constants["c_res"] == constants["c_1"] >> constants["c_2"]
    assert constants["c_2"] < 32
    e = instantiate(rule.pattern, 32, constants=constants)
    assert e is not None and any(isinstance(n, IRConst) for n in walk(e))


def test_unsatisfiable_constraints_give_none():
    class Impossible:
        pattern = instance("Add_OllvmRule_4").pattern
        replacement = instance("Add_OllvmRule_4").replacement
        from binja_d810.third_party.d810.mba.dsl import Const as DslConst

        c = DslConst("c")
        CONSTRAINTS = [c == 1, c == 2]

        def get_constraints(self, z3_vars):
            return []

    assert solve_constants(Impossible(), 32) is None
