"""Spike evidence (docs/design/spikes/2026-10-15-v2-hlil.md). Runs only with BINJA_D810_SPIKE=1."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_collection_modifyitems(config, items):
    if os.environ.get("BINJA_D810_SPIKE"):
        return
    skip = pytest.mark.skip(reason="spike evidence; set BINJA_D810_SPIKE=1")
    for item in items:
        if "binja/spikes" in str(item.fspath):
            item.add_marker(skip)
