"""Opt-in real-sample harness. Runs only when BINJA_D810_SAMPLES names a directory.

The samples include live malware: they are opened only with binaryninja.load (static analysis),
never executed, debugged or emulated, and never copied into the repository.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_collection_modifyitems(config, items):
    if os.environ.get("BINJA_D810_SAMPLES"):
        return
    skip = pytest.mark.skip(reason="BINJA_D810_SAMPLES not set (opt-in real-sample harness)")
    for item in items:
        if "binja/real" in str(item.fspath):
            item.add_marker(skip)
