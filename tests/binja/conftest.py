import pytest

binaryninja = pytest.importorskip("binaryninja")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
