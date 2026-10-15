import json
from pathlib import Path

import binja_d810
from binja_d810.rules.d810_ops import unsupported_ops
from binja_d810.third_party.d810.mba.dsl import SymbolicExpression
from binja_d810.third_party.d810.mba.rules import VerifiableRule

MANIFEST_PATH = Path(binja_d810.__file__).parent / "rules" / "verified_rules.json"


def manifest():
    return json.loads(MANIFEST_PATH.read_text())["rules"]


def instances():
    return {type(r).__name__: r for r in VerifiableRule.instantiate_all()}


def test_manifest_covers_exactly_the_vendored_rules():
    assert set(manifest()) == set(instances())


def test_classic_identity_is_proven_at_every_width():
    assert manifest()["Xor_HackersDelightRule_1"]["widths"] == [8, 16, 32, 64]


def test_known_incorrect_and_unsupported_rules_are_never_enabled():
    m = manifest()
    for name, rule in instances().items():
        if getattr(rule, "KNOWN_INCORRECT", False) or unsupported_ops(rule.pattern) or unsupported_ops(rule.replacement):
            assert m[name]["widths"] == [], name


def test_rules_without_symbolic_sides_are_never_enabled():
    # verify_rule returns True without proving anything when either side is not symbolic
    # (e.g. PredFFRule1, whose REPLACEMENT is a bare DynamicConst).
    m = manifest()
    for name, rule in instances().items():
        if not (isinstance(rule.pattern, SymbolicExpression) and isinstance(rule.replacement, SymbolicExpression)):
            assert m[name]["widths"] == [], name


def test_most_rules_are_usable():
    assert sum(1 for e in manifest().values() if e["widths"]) >= 100
