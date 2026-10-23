"""Display heuristics over exact known bytes; never authorize an IL rewrite."""


def recognize(memory, offset, *, maximum, minimum, length=None):
    if offset < 0 or offset >= len(memory):
        return None
    if length is not None and (type(length) is not int or not minimum <= length <= maximum):
        return None
    end = min(len(memory), offset + (maximum if length is None else length))
    data = memory[offset:end]
    if length is not None:
        if len(data) != length or None in data:
            return None
    else:
        if 0 not in data:
            return None
        data = data[:data.index(0)]
    if len(data) < minimum or any(c is None or not (32 <= c < 127 or c in (9, 10, 13)) for c in data):
        return None
    return bytes(data).decode("ascii")
