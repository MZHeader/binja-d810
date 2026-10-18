"""v3.5: which constant-address loads cannot fault (binja_d810.binja.memory)."""

from types import SimpleNamespace as NS

from binja_d810.binja.memory import FixedLoads, fixed_load_is_safe

DATA = NS(start=0x3000, end=0x3008, readable=True)
GUARD = NS(start=0x5000, end=0x6000, readable=False)
EXTERN_SEG = NS(start=0x8000, end=0x8100, readable=True)


def section(name, start, end, semantics="ReadWriteDataSectionSemantics"):
    return NS(name=name, start=start, end=end, semantics=NS(name=semantics))


class View:
    def __init__(self):
        self.segments = [DATA, GUARD, EXTERN_SEG]
        self.sections = [section(".data", 0x3000, 0x3008), section(".guard", 0x5000, 0x6000),
                         section(".extern", 0x8000, 0x8100, "ExternalSectionSemantics")]
        self.calls = 0

    def get_segment_at(self, address):
        self.calls += 1
        return next((s for s in self.segments if s.start <= address < s.end), None)

    def get_sections_at(self, address):
        return [s for s in self.sections if s.start <= address < s.end]


def test_a_read_inside_the_images_data_is_safe():
    assert fixed_load_is_safe(View(), 0x3004, 4)


def test_a_read_that_crosses_the_end_of_its_segment_is_not():
    assert not fixed_load_is_safe(View(), 0x3006, 4)


def test_unmapped_unreadable_and_external_reads_are_not():
    view = View()
    assert not fixed_load_is_safe(view, 0x4000, 4)  # no segment
    assert not fixed_load_is_safe(view, 0x5000, 4)  # not readable
    assert not fixed_load_is_safe(view, 0x8000, 8)  # Binary Ninja's synthetic .extern holds no memory


def test_a_read_outside_every_section_is_not():
    view = View()
    view.sections = []
    assert not fixed_load_is_safe(view, 0x3000, 4)


def test_empty_reads_are_not():
    assert not fixed_load_is_safe(View(), 0x3000, 0)


def test_decisions_are_remembered_per_range():
    view = View()
    loads = FixedLoads(view)
    assert loads(0x3000, 4) and loads(0x3000, 4)
    assert view.calls == 1
