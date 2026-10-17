"""SSA definitions by variable, else by storage and version (v3.3). No binaryninja import.

Binary Ninja numbers SSA versions per storage location (binja_d810.binja.mlil.lift). After rewrites
and SSA regeneration, a read can name another variable of the same storage than its definition does:
x-tunnel.bin 0x424760 reads edx_98#97, which edx_103#97 = phi(edx_98#90, edx_98#96) defines, and asking
for edx_98#97's definition finds nothing.
"""

from __future__ import annotations

_BY_STORAGE = ("ssa definitions", "by storage")  # cache key, apart from the (identifier, version) keys


def definition_of(ssa_func, ssa_var, cache: dict, by_storage: bool = True):
    """The instruction defining `ssa_var`, or None (an input, or nothing of its storage at that version).
    `cache` is shared by the lifts of one SSA form. The storage index covers every SSA value of the
    function: `by_storage` only with a cache that lives through many lookups (a lift's own short-lived
    cache rebuilding it made a large function's analysis run past Binary Ninja's limit)."""
    key = (ssa_var.var.identifier, ssa_var.version)
    if key in cache:
        return cache[key]
    found = ssa_func.get_ssa_var_definition(ssa_var)
    if found is None and ssa_var.version > 0 and by_storage:
        index = cache.get(_BY_STORAGE)
        if index is None:
            index = cache[_BY_STORAGE] = {}
            for v in ssa_func.ssa_vars:
                index.setdefault((int(v.var.source_type), v.var.storage, v.version), []).append(v)
        storage = (int(ssa_var.var.source_type), ssa_var.var.storage, ssa_var.version)
        for other in index.get(storage, ()):
            if other.var.identifier != ssa_var.var.identifier:
                found = ssa_func.get_ssa_var_definition(other)
                if found is not None:
                    break
    cache[key] = found
    return found
