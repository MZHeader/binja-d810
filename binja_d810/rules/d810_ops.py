"""The d810 DSL operations binja-d810 can match and build."""

SUPPORTED_D810_OPS = frozenset({"add", "sub", "mul", "and", "or", "xor", "shl", "shr", "sar", "neg", "bnot", "zext"})


def pattern_ops(sym) -> set[str]:
    found: set[str] = set()
    stack = [sym]
    while stack:
        node = stack.pop()
        op = getattr(node, "operation", None)
        if op is None:
            continue
        found.add(op)
        for child in (node.left, node.right):
            if child is not None:
                stack.append(child)
    return found


def unsupported_ops(sym) -> set[str]:
    return pattern_ops(sym) - SUPPORTED_D810_OPS
