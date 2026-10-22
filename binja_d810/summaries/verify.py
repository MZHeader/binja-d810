"""An exact structural check for copied primitive IL expressions.

This is not a sampling test: operation, width, flags, attributes, register identities and every
operand must match. Control-flow labels require a separate mapping and are rejected here.
"""


def fingerprint(node, remaining, expired=lambda: False):
    remaining[0] -= 1
    if remaining[0] < 0 or expired():
        raise ValueError("copy proof budget exhausted")
    values = []
    for name, value, kind in node.detailed_operands:
        if kind == "LowLevelILInstruction":
            part = fingerprint(value, remaining, expired)
        elif kind in ("ILRegister", "ILFlag"):
            part = int(value.index)
        elif kind == "int":
            part = int(value)
        else:
            raise ValueError(f"unsupported copy proof operand: {kind}")
        values.append((name, kind, part))
    return (node.operation.name, node.size, node.raw_flags, tuple(sorted(int(x) for x in node.attributes)),
            tuple(values))


def identical(before, after, max_nodes, deadline):
    try:
        return fingerprint(before, [max_nodes], deadline.expired) == fingerprint(after, [max_nodes], deadline.expired)
    except (ValueError, TypeError, AttributeError):
        return False
