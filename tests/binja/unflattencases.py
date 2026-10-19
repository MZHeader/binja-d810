"""The flatten fixtures' cases (tests/samples/src/flatten.c), shared by test_unflatten_graph.py and test_unflatten.py."""

# Each flattened function's state constants.
SHAPES = {
    "fl_tree": [0x7a3c19e5, 0x1b4d8c27, 0x5e91a3f0, 0x33c6d218, 0x6f2b7c41],
    "fl_dense": list(range(0x40, 0x47)),
    "fl_nested": [0x2d81f6a3, 0x49e07b12, 0x0c5a9e84, 0x71f3d250],
    "fl_loop": [0x58c2e13b, 0x26b9d4f7, 0x4e7a0d93, 0x1c95b2e6, 0x6a17c05e],
}
# (earlier, later) mark(k) calls: the order the machine runs its handlers in, on every path through both.
ORDERS = {
    "fl_tree": [(1, 2), (2, 3), (2, 4), (4, 5)],
    "fl_dense": [(11, 14), (14, 16), (16, 12), (12, 15), (15, 13), (13, 17)],
    "fl_nested": [(21, 22), (22, 23), (22, 24)],
    "fl_loop": [(31, 32), (32, 33), (33, 34)],
}
NEGATIVES = ["plain_for", "found_flag", "global_state", "escaped_state"]
# (build-level, function) -> why the compiler left no dispatcher there. Strict: a listed case whose state
# constants are still in the HLIL fails the baseline test, so the list stays true.
NOT_FLATTENED: dict[tuple[str, str], str] = {}
# (build-level, function) -> why the dense switch has no jump table there. Strict: a listed case whose
# dispatcher jumps fails, so the list stays true.
_COMPARE_TREE = "clang for arm64 lowers the 7-case switch to a tree of compares, not a jump table"
NO_JUMP_TABLE: dict[tuple[str, str], str] = {
    ("arm64-O0", "fl_dense"): _COMPARE_TREE,
    ("arm64-O2", "fl_dense"): _COMPARE_TREE,
}
