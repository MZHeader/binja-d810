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


@pytest.mark.parametrize("text", ["(x >> 1) + (x & y)", "sar(x, 1) ^ y"])
def test_cobra_backend_refuses_unsupported_ops(text):
    assert CobraBackend(lambda t, w: "v0").simplify(ir(text)) is None


def test_cobra_backend_handles_solver_failure():
    assert CobraBackend(lambda t, w: None).simplify(ir("(x ^ y) + 2 * (x & y)")) is None


def test_default_backends_always_include_linear():
    names = [b.name for b in default_backends()]
    assert names[-1] == "linear"
    assert ("cobra" in names) == (cobra_shim.load() is not None)


@pytest.mark.skipif(cobra_shim.load() is None, reason="CoBRA not available")
def test_real_cobra_simplifies_polynomial_mba():
    cobra = CobraBackend(cobra_shim.load())
    result = cobra.simplify(ir("(x & y) * (x | y) + (x & ~y) * (~x & y)"))
    assert result == ir("x * y")
