import pytest

from binja_d810.ir.expr import Var, substitute, variables
from binja_d810.ir.normalize import normalize
from binja_d810.ir.parse import parse
from binja_d810.semantic import cobra_shim
from binja_d810.semantic.backend import default_backends
from binja_d810.semantic.cobra_backend import CobraBackend


def ir(text, width=32):
    return normalize(parse(text, width))


def test_cobra_backend_renames_variables_and_restores_refs():
    seen = []

    def fake_solve(text, width):
        seen.append((text, width))
        return "v0 + v1"

    ref = object()
    e = substitute(ir("(x ^ y) + 2 * (x & y)"), {"x": Var("x", 32, ref=ref)})
    result = CobraBackend(fake_solve).simplify(e)
    text, width = seen[0]
    assert width == 32 and "x" not in text and "y" not in text
    assert result == ir("x + y")
    assert next(v for v in variables(result) if v.name == "x").ref is ref


@pytest.mark.parametrize("text,atom", [("(x >> 1) + (x & y)", "x >> 1"), ("sar(x, 1) ^ y", "sar(x, 1)")])
def test_cobra_backend_treats_unsupported_subterms_as_atoms(text, atom):
    # CoBRA takes + - * & | ^ ~ only; anything else is passed as an opaque variable and restored.
    seen = []

    def fake_solve(t, w):
        seen.append(t)
        return "v0"

    result = CobraBackend(fake_solve).simplify(ir(text))
    assert ">>" not in seen[0] and "sar" not in seen[0]
    assert result in (ir(atom), ir("x"), ir("y"))


def test_cobra_backend_sees_narrow_reads_of_wide_storage_as_atoms():
    # trunc8(@x0#0): the low byte of a 64-bit register version (binja_d810.binja.storage).
    from binja_d810.ir.expr import BinOp, UnOp

    bx, by = Var("@x0#0", 64), Var("@x1#0", 64)
    x, y = UnOp("trunc", bx, 8), UnOp("trunc", by, 8)
    e = BinOp("add", BinOp("xor", x, y, 8), BinOp("mul", ir("2", 8), BinOp("and", x, y, 8), 8), 8)
    seen = []

    def fake_solve(t, w):
        seen.append((t, w))
        return "v0 + v1"

    result = CobraBackend(fake_solve).simplify(e)
    assert seen and seen[0][1] == 8 and "@" not in seen[0][0]
    assert result is not None and {n for n in map(str, variables(result))} == {str(bx), str(by)}


def test_cobra_backend_handles_solver_failure():
    assert CobraBackend(lambda t, w: None).simplify(ir("(x ^ y) + 2 * (x & y)")) is None


def test_default_backends_always_include_linear_first():
    names = [b.name for b in default_backends()]
    assert names[0] == "linear"
    assert ("cobra" in names) == (cobra_shim.load() is not None)


@pytest.mark.skipif(cobra_shim.load() is None, reason="CoBRA not available")
def test_real_cobra_simplifies_polynomial_mba():
    cobra = CobraBackend(cobra_shim.load())
    result = cobra.simplify(ir("(x & y) * (x | y) + (x & ~y) * (~x & y)"))
    assert result == ir("x * y")


def test_cobra_answers_are_reused_for_identical_queries():
    calls = []

    def fake_solve(text, width, deadline=None):
        calls.append(text)
        return "v0 + v1"

    backend = CobraBackend(fake_solve)
    e = ir("(x ^ y) + 2 * (x & y)")
    assert backend.simplify(e) == backend.simplify(e) == ir("x + y")
    assert len(calls) == 1
    other = ir("(a ^ b) + 2 * (a & b)")  # the same query once variables are renamed v0, v1
    assert backend.simplify(other) == ir("a + b") and len(calls) == 1


def test_cobra_is_not_asked_beyond_the_variable_limit_counting_atoms():
    # On real samples CoBRA answered none of 33 queries with 5 or more names (variables and atoms)
    # and spent up to 2.45 s on each, past its own time limit; a 4-variable expression with shifts
    # has more names than variables.
    from binja_d810.simplify import Limits, simplify_expression

    asked = []

    def fake_solve(text, width, deadline=None):
        asked.append(text)
        return None

    backend = CobraBackend(fake_solve)
    four_atoms = ir("((a >> 1) ^ (b >> 2)) + 2 * ((c >> 3) & (d >> 4)) + (a & b)")  # 4 variables; 4 atoms + a, b
    assert backend.names(four_atoms) == 6
    simplify_expression(four_atoms, [], [backend], Limits(max_vars=4))
    import re

    assert asked and all(len(set(re.findall(r"v\d+", text))) <= 4 for text in asked)  # subtrees only
    asked.clear()
    simplify_expression(ir("(a ^ b) + 2 * (a & b) + (c | (a >> 1))"), [], [backend], Limits(max_vars=4))
    assert asked  # 4 names: asked
