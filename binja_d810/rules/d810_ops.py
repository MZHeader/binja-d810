"""The d810 DSL operations binja-d810 can match and build."""

SUPPORTED_D810_OPS = frozenset({"add", "sub", "mul", "and", "or", "xor", "shl", "shr", "sar", "neg", "bnot", "zext",
                                "bool_to_int"})


def rule_replacement(rule):
    """A rule's replacement: its symbolic expression, or a constant computed at match time
    (DynamicConst; the rule's `replacement` property is None for those: PredOdd1, PredFFRule1)."""
    if rule.replacement is not None:
        return rule.replacement
    from ..third_party.d810.mba.dsl import DynamicConst

    found = getattr(type(rule), "REPLACEMENT", None)
    return found if isinstance(found, DynamicConst) else None


def pattern_ops(sym) -> set[str]:
    found: set[str] = set()
    stack = [sym]
    while stack:
        node = stack.pop()
        op = getattr(node, "operation", None)
        if op is None:
            continue
        found.add(op)
        constraint = getattr(node, "constraint", None)  # bool_to_int: the compared sides
        for child in (node.left, node.right, getattr(constraint, "left", None), getattr(constraint, "right", None)):
            if child is not None:
                stack.append(child)
    return found


def unsupported_ops(sym) -> set[str]:
    return pattern_ops(sym) - SUPPORTED_D810_OPS
