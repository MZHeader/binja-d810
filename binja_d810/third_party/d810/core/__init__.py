"""Shim for d810.core: only what the vendored MBA modules import."""

import logging

from . import typing  # noqa: F401  (vendored core/typing.py, imported as d810.core.typing)
from .bits import AND_TABLE  # noqa: F401  (mba/dsl.py's when.equals_minus_two imports it lazily)


def getLogger(name: str) -> logging.Logger:
    return logging.getLogger(name)
