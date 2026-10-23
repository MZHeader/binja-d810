"""Remove only an unchanged owned line, preserving surrounding user text."""


def remove_line(current, owned):
    if current == owned:
        return ""
    if current.endswith("\n" + owned):
        return current[:-len(owned) - 1]
    lines = current.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.rstrip("\r\n") == owned:
            del lines[index]
            return "".join(lines)
    return current
