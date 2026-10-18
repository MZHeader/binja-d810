from binja_d810.flow.ranges import layout, parse


def test_ranges_parse_merge_and_name_bad_entries():
    found, errors = parse(["0x2000-0x20ff", "0x1000-0x1fff", "0x1800-0x2003", "nonsense", "0x30-0x10", "0x1-0x2-0x3"])
    assert found == [(0x1000, 0x2100)]
    assert errors == ["nonsense", "0x30-0x10", "0x1-0x2-0x3"]


def test_adjacent_ranges_merge_and_the_end_is_included():
    assert parse(["0x10-0x1f", "0x20-0x2f"]) == ([(0x10, 0x30)], [])
    assert parse([" 0x10 - 0x10 "]) == ([(0x10, 0x11)], [])


def test_a_range_is_laid_over_its_variables():
    variables = [(0x101c, 8), (0x0ffc, 8), (0x1004, 8), (0x1010, 8), (0x2000, 4)]
    inside, gaps = layout(0x1000, 0x1020, variables)
    assert inside == [(0x1004, 8), (0x1010, 8)]  # the two straddling the edges are not retyped
    assert gaps == [(0x100C, 0x1010), (0x1018, 0x101C)]  # nor are their bytes gaps


def test_an_empty_range_is_one_gap():
    assert layout(0x1000, 0x1010, []) == ([], [(0x1000, 0x1010)])


def test_addresses_must_fit_binary_ninjas_unsigned_address_space():
    invalid = ["-1-0x10", "0x0-0x10000000000000000", "0x10000000000000000-0x10000000000000001"]
    assert parse(invalid) == ([], invalid)
