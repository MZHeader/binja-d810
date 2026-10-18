from binja_d810.flow.tables import MAX_ENTRIES, Decoded, Load, Span, addresses_from, decode, span_of
from binja_d810.ir.expr import BinOp, Const, Var

KEY = 0x13371337
M64 = (1 << 64) - 1


def memory(words: dict):
    return lambda address, size: words.get(address)


def minus_key(e, width=64):
    return BinOp("sub", e, Const(KEY, width), width)


def test_a_constant_index():
    decoded, why = decode(minus_key(Var("t", 64)), [Load("t", (0x1008,), 8)], memory({0x1008: 0x4000 + KEY}))
    assert why == "" and decoded == Decoded((0x4000,), (Span(0x1008, 8, 1),))


def test_four_entries_decoded_with_xor_and_a_key_global():
    key = 0x5A5A
    words = {0x1000 + 8 * i: (0x4000 + 0x10 * i) ^ key for i in range(4)} | {0x2000: key}
    loads = [Load("t", (0x1000, 0x1008, 0x1010, 0x1018), 8), Load("k", (0x2000,), 8)]
    decoded, _ = decode(BinOp("xor", Var("t", 64), Var("k", 64), 64), loads, memory(words))
    assert decoded.targets == (0x4000, 0x4010, 0x4020, 0x4030)
    assert decoded.spans == (Span(0x1000, 8, 4), Span(0x2000, 8, 1))


def test_an_mba_decode():
    t, k = Var("t", 64), Const(-KEY & M64, 64)
    mba = BinOp("add", BinOp("xor", t, k, 64), BinOp("shl", BinOp("and", t, k, 64), Const(1, 64), 64), 64)  # t + k
    decoded, _ = decode(mba, [Load("t", (0x1008,), 8)], memory({0x1008: 0x4000 + KEY}))
    assert decoded.targets == (0x4000,)


def test_a_32_bit_table():
    decoded, _ = decode(minus_key(Var("t", 32), 32), [Load("t", (0x1004,), 4)], memory({0x1004: 0x401000 + KEY}))
    assert decoded.targets == (0x401000,) and decoded.spans == (Span(0x1004, 4, 1),)


def test_a_target_that_reads_a_variable_is_not_decoded():
    decoded, why = decode(BinOp("add", Var("t", 64), Var("x", 64), 64), [Load("t", (0x1000,), 8)],
                          memory({0x1000: 1}))
    assert decoded is None and "x" in why


def test_two_indexed_tables_are_not_decoded():
    loads = [Load("t", (0x1000, 0x1008), 8), Load("u", (0x2000, 0x2008), 8)]
    decoded, why = decode(BinOp("add", Var("t", 64), Var("u", 64), 64), loads, memory({}))
    assert decoded is None and "more than one" in why


def test_an_unreadable_entry_is_not_decoded():
    decoded, why = decode(minus_key(Var("t", 64)), [Load("t", (0x1000, 0x1008), 8)], memory({0x1000: KEY}))
    assert decoded is None and "0x1008" in why


def test_entries_must_be_whole_entries_apart():
    assert span_of(Load("t", (0x1000, 0x1004), 8)) is None
    assert span_of(Load("t", (0x1000, 0x1010), 8)) == Span(0x1000, 8, 3)  # every other entry: the span covers both


def test_value_sets():
    assert addresses_from("ConstantPointerValue", value=0x1000) == (0x1000,)
    assert addresses_from("UnsignedRangeValue", ranges=[(0x1000, 0x1018, 8)]) == (0x1000, 0x1008, 0x1010, 0x1018)
    assert addresses_from("InSetOfValues", values=[0x1010, 0x1000]) == (0x1000, 0x1010)
    assert addresses_from("UndeterminedValue") is None
    assert addresses_from("SignedRangeValue", ranges=[(0x1018, 0x1000, 8)]) is None


def test_at_most_max_entries():
    last = 0x1000 + 8 * (MAX_ENTRIES - 1)
    assert len(addresses_from("SignedRangeValue", ranges=[(0x1000, last, 8)])) == MAX_ENTRIES
    assert addresses_from("SignedRangeValue", ranges=[(0x1000, last + 8, 8)]) is None
