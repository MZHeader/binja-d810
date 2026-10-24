"""Typed semantic contracts; no symbol-name or library-name catalogue.

Fresh ordinary storage is enough to reason about a private object's bytes. Erasing the calls
additionally requires an explicit nonnull allocation and an unobservable allocation/free pair.
Ordinary function signatures, and a generic `pure` flag, do not supply these guarantees.
"""

from dataclasses import dataclass
import json

ATTRIBUTE = "binja_d810.memory"


@dataclass(frozen=True)
class Contract:
    role: str
    parameter: int
    nonnull: bool = False
    elidable: bool = False


def parse(text: str, max_bytes: int):
    if not isinstance(text, str) or len(text.encode("utf-8")) > max_bytes:
        raise ValueError("allocation contract byte limit")
    spec = json.loads(text)
    if not isinstance(spec, dict) or type(spec.get("version")) is not int or spec["version"] != 1:
        raise ValueError("unsupported allocation contract version")
    allowed = {"version", "role", "parameter", "fresh", "ordinary", "nonnull", "elidable"}
    if set(spec) - allowed:
        raise ValueError("unknown allocation contract fields")
    role = spec.get("role")
    parameter = spec.get("parameter")
    if role not in ("allocate", "deallocate") or type(parameter) is not int or parameter < 0:
        raise ValueError("invalid allocation contract role or parameter")
    for key in ("fresh", "ordinary", "nonnull", "elidable"):
        if key in spec and type(spec[key]) is not bool:
            raise ValueError("allocation contract guarantees must be booleans")
    if role == "allocate" and (spec.get("fresh") is not True or spec.get("ordinary") is not True):
        raise ValueError("fresh ordinary storage is not guaranteed")
    if role == "deallocate" and any(k in spec for k in ("fresh", "ordinary", "nonnull")):
        raise ValueError("allocation-only guarantees on a deallocator")
    return Contract(role, parameter, spec.get("nonnull", False), spec.get("elidable", False))


def resolve(sources, max_bytes):
    """Every available declaration must agree; stronger assumptions never silently win."""
    found = {parse(source, max_bytes) for source in sources if source is not None}
    if len(found) > 1:
        raise ValueError("conflicting allocation contracts")
    return next(iter(found), None)
