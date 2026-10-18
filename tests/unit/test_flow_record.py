from binja_d810.flow.record import SETTING, Change, Record


def table(start, end, *claims):
    return Change("table", start, {"end": end, "defined": [[start, "uint64_t const __d810[2]"]]},
                  [[start, "int64_t __d810"]], set(claims))


def test_a_change_is_kept_by_kind_and_key():
    record = Record()
    record.add(table(0x1000, 0x1010, 0x400))
    assert record.get("table", 0x1000).detail["end"] == 0x1010
    assert record.get("range", 0x1000) is None and len(record) == 1


def test_adding_the_same_change_again_adds_its_claims():
    record = Record()
    first = record.add(table(0x1000, 0x1010, 0x400))
    assert record.add(table(0x1000, 0x1010, 0x500)) is first
    assert first.claims == {0x400, 0x500}


def test_a_change_is_unclaimed_once_every_claimant_releases_it():
    record = Record([table(0x1000, 0x1010, 0x400, 0x500)])
    record.release(0x400)
    assert record.unclaimed() == [] and [c.key for c in record.claimed_by(0x500)] == [0x1000]
    record.release(0x500)
    assert [c.key for c in record.unclaimed()] == [0x1000]
    record.drop("table", 0x1000)
    assert len(record) == 0


def test_covering_and_overlapping():
    record = Record([table(0x1000, 0x1010, 1), Change("range", 0x2000, {"end": 0x2100}, [], {SETTING})])
    assert record.covering(0x1008, 0x1010).key == 0x1000
    assert record.covering(0x1008, 0x1018) is None
    assert record.overlapping(0x1008, 0x1018).key == 0x1000
    assert record.covering(0x2010, 0x2018).kind == "range"
    assert record.overlapping(0x1010, 0x2000) is None


def test_branches_by_function():
    record = Record([Change("branches", 0x1234, {"function": 0x1200, "targets": [0x1240]}, [], {0x1200}),
                     Change("branches", 0x5678, {"function": 0x5600, "targets": [0x5680, 0x5690]}, [], {0x5600})])
    assert record.branches_of(0x1200) == [(0x1234, [0x1240])]
    assert record.branches_of(0x9999) == []


def test_the_record_round_trips_through_json():
    record = Record([table(0x1000, 0x1010, 0x400), Change("range", 0x2000, {"end": 0x2100}, [], {SETTING})])
    again = Record.from_json(record.to_json())
    assert again.to_json() == record.to_json()
    assert again.get("range", 0x2000).claims == {SETTING}
    assert again.get("table", 0x1000).previous == [[0x1000, "int64_t __d810"]]
