from binja_d810.flow.codecheck import call_problem, is_self_call, is_tail_call, jump_problem, split_starts


class Code:
    """Code in [start, end) ranges; functions by start -> end; `split`: starts the sweep split off the
    jumping function."""

    def __init__(self, code=((0x1000, 0x2000),), undecodable=(), functions=None, split=()):
        self.code, self.undecodable = code, set(undecodable)
        self.functions, self.split = functions or {}, set(split)

    def is_code(self, address):
        return any(start <= address < end for start, end in self.code)

    def decodes(self, address):
        return address not in self.undecodable

    def owners(self, address):
        return [start for start, end in self.functions.items() if start <= address < end]

    def function_at(self, address):
        return address in self.functions

    def split_off(self, function, address):
        return address in self.split


F = 0x1000  # the jumping function, 0x1000-0x1100


def test_jump_targets_in_plain_code():
    assert jump_problem([0x1200, 0x1210], F, Code(functions={F: 0x1100})) is None


def test_a_jump_target_outside_code_or_not_decoding():
    assert "not code" in jump_problem([0x3000], F, Code())
    assert "does not decode" in jump_problem([0x1200], F, Code(undecodable=[0x1200]))


def test_a_jump_target_inside_another_function():
    code = Code(functions={F: 0x1100, 0x1800: 0x1900})
    assert "inside the function at 0x1800" in jump_problem([0x1810], F, code)


def test_a_jump_target_inside_its_own_function_and_another():
    code = Code(functions={F: 0x1300, 0x1200: 0x1300})
    assert jump_problem([0x1210], F, code) is None


def test_one_function_start_is_a_tail_call():
    code = Code(functions={F: 0x1100, 0x1800: 0x1900})
    assert jump_problem([0x1800], F, code) is None
    assert is_tail_call([0x1800], F, code)


def test_a_function_start_among_several_targets():
    code = Code(functions={F: 0x1100, 0x1800: 0x1900})
    assert "starts another function" in jump_problem([0x1200, 0x1800], F, code)
    assert not is_tail_call([0x1200, 0x1800], F, code)


def test_code_the_sweep_split_off_is_the_jumps_own():
    code = Code(functions={F: 0x1100, 0x1100: 0x1110, 0x1110: 0x1120}, split=[0x1100, 0x1110])
    assert jump_problem([0x1100, 0x1110], F, code) is None
    assert not is_tail_call([0x1110], F, code)
    assert split_starts([0x1100, 0x1110, 0x1110], F, code) == [0x1100, 0x1110]


def test_call_targets():
    code = Code(functions={0x1800: 0x1900})
    assert call_problem([0x1800], code) is None
    assert call_problem([0x1200], code) is None  # code inside no function: it becomes one
    assert "inside the function at 0x1800" in call_problem([0x1810], code)
    assert "not code" in call_problem([0x3000], code)


def test_a_jump_to_the_function_itself_is_a_self_call_not_a_tail_call():
    code = Code(functions={F: 0x1100, 0x1800: 0x1900})
    assert is_self_call([F, F], F) and not is_tail_call([F], F, code) and jump_problem([F], F, code) is None
    assert not is_self_call([F, 0x1800], F) and not is_self_call([0x1800], F)
