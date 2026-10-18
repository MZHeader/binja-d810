"""The flow fixtures' cases (tests/samples/src/flow.c), shared by test_flow.py and test_flow_undo.py."""

import re

KEY = 0x13371337
JUMPS = ["tbl_jump_const", "tbl_jump_sub", "tbl_jump_var", "tbl_jump_keyglobal", "tbl_jump_mbadecode",
         "tbl_jump_keyindex", "mba_jump", "written_table", "escaped_table", "bad_entry_table"]
CALLS = ["tbl_call_const", "tbl_call_shared", "tbl_call_sub", "tbl_call_user", "mba_call", "tbl_call_keyindex",
         "written_call", "escaped_call"]
RAW = {"tbl_call_keyindex"}  # plain function pointers: no decode to see
# (build-level, function) -> why the compiler did not emit the pattern there. Strict: a listed case that has
# the pattern fails the baseline test, so the list stays true.
_SEES_IDENT = "Binary Ninja sees through the identity wrapper itself: HLIL calls _off_fa(arg1) with no ident call"
NOT_EMITTED: dict[tuple[str, str], str] = {
    ("arm64-O0", "ident_call"): _SEES_IDENT,
    ("arm64-O2", "ident_call"): _SEES_IDENT,
}
# (build-level, function) -> why D-810 does not resolve it there. Strict: a listed case that resolves fails.
KNOWN_MISSES: dict[tuple[str, str], str] = {}


def text(func) -> str:
    return "\n".join(map(str, func.hlil.instructions))


def decoded(hlil: str) -> bool:
    """The decode is still visible: KEY, or -KEY as a 32-bit constant."""
    return "0x13371337" in hlil or "0xecc8ecc9" in hlil


def has(hlil: str, constant: str) -> bool:
    """`constant` appears as a whole token (0xa1, not 0xa10)."""
    return re.search(rf"\b{re.escape(constant)}\b", hlil) is not None


def check(key: str, name: str, ok: bool, detail: str) -> None:
    """Assert `ok`, unless the case is listed: not emitted (nothing to check) or a known miss (must still miss)."""
    if (key, name) in NOT_EMITTED:
        return
    if (key, name) in KNOWN_MISSES:
        assert not ok, f"listed as a miss but resolved: {(key, name)}"
        return
    assert ok, detail
