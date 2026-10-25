from binja_d810 import deadline


def test_reserve_keeps_one_shared_deadline(monkeypatch):
    monkeypatch.setattr(deadline.time,'monotonic',lambda:10.0)
    outer=deadline.Deadline(30.0)
    assert outer.reserve(7).at==23.0
    assert outer.at==30.0 and outer.remaining()==20.0
    assert outer.reserve(-5)==outer
    assert deadline.Deadline(12).reserve(7).expired()
    assert deadline.Deadline(9).reserve(7).at==9
    assert deadline.Deadline.never().reserve(7)==deadline.Deadline.never()
