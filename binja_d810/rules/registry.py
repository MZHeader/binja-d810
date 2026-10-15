"""Loads the Tier 1 rules enabled by verified_rules.json."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Collection

from .d810_adapter import D810Rule

MANIFEST = Path(__file__).with_name("verified_rules.json")


@lru_cache(maxsize=1)
def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())["rules"]


@lru_cache(maxsize=1)
def _instances() -> tuple:
    from ..third_party.d810.mba.rules import VerifiableRule

    return tuple(VerifiableRule.instantiate_all())


def instance(name: str) -> Any:
    return next(r for r in _instances() if type(r).__name__ == name)


def rule_status() -> dict:
    return dict(_manifest())


def load_rules(disabled: Collection[str] = ()) -> list[D810Rule]:
    manifest = _manifest()
    rules = []
    for rule in _instances():
        name = type(rule).__name__
        widths = manifest.get(name, {}).get("widths", [])
        if widths and name not in disabled:
            rules.append(D810Rule(rule, widths))
    return sorted(rules, key=lambda r: r.name)
