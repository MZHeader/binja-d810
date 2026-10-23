from binja_d810.consteval.strings import recognize


def test_termination_and_adjacent_length_are_only_display_heuristics():
    options = dict(maximum=12, minimum=4)
    assert recognize(list(b"hello\0tail"), 0, **options) == "hello"
    assert recognize(list(b"hello"), 0, length=5, **options) == "hello"
    assert recognize(list(b"hello"), 0, **options) is None
    assert recognize(list(b"hello"), 0, length=6, **options) is None
    assert recognize([None, 65, 66, 67, 68, 0], 0, **options) is None
    assert recognize(list(b"hello\0"), -1, **options) is None
    assert recognize(list(b"hello\0"), 0, maximum=4, minimum=4) is None
    assert recognize(list(b"abc\x01\0"), 0, **options) is None
