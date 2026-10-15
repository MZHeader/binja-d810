import random

import pytest

from binja_d810.semantic import cobra_shim

solve = cobra_shim.load()
needs_cobra = pytest.mark.skipif(solve is None, reason="CoBRA not available")


def test_binding_is_found_when_d810_cobra_is_installed():
    pytest.importorskip("d810_cobra._cobra")
    assert cobra_shim._python_binding() is not None


@needs_cobra
@pytest.mark.parametrize(
    "expr, width, accepted",
    [
        ("(x&y)+(x|y)", 64, {"x+y", "y+x"}),
        ("(x ^ 0x10) + 2 * (x & 0x10)", 64, {"16+x", "x+16"}),
        ("((a^b)&c) | ((a&b)^c)", 64, {"c^a&b", "a&b^c", "c^(a&b)", "(a&b)^c"}),
    ],
)
def test_readme_examples(expr, width, accepted):
    assert solve(expr, width).replace(" ", "") in accepted


@needs_cobra
@pytest.mark.parametrize(
    "expr, width",
    [
        ("(v0 ^ v1) + 2 * (v0 & v1) + 3 * ((v0 | v1) - (v0 & ~v1) - v1)", 32),
        ("(v0 * v1) + (v0 & v1) * (v0 | v1) - 2 * (v0 * v1)", 32),
        ("(v0 | 0xff) - (v0 & (-256)) + v1", 16),
        # CoBRA renumbers the variables left after dropping an unused one; v0 here cancels out.
        ("v1 + 2 * v2 + (v0 ^ v1) - (v0 | v1) + (v0 & v1)", 32),
        ("(v0 & 256) + v1 + (v2 ^ v2)", 16),
    ],
)
def test_output_means_the_same_as_input(expr, width):
    out = solve(expr, width)
    assert out is not None
    mask = (1 << width) - 1
    rng = random.Random(0)
    for _ in range(64):
        env = {f"v{i}": rng.getrandbits(width) for i in range(3)}
        assert eval(out, {}, env) & mask == eval(expr, {}, env) & mask, out


@needs_cobra
def test_unsupported_operators_are_refused():
    assert solve("(v0 >> 3) + (v0 & v1)", 32) is None


def test_render_parenthesises_by_precedence():
    tree = {"kind": "bin", "op": "-", "a": {"kind": "var", "name": "a"},
            "b": {"kind": "bin", "op": "+", "a": {"kind": "var", "name": "b"},
                  "b": {"kind": "un", "op": "~", "a": {"kind": "const", "value": -3}}}}
    assert cobra_shim._render(tree).replace(" ", "") == "a-(b+~(-3))"
