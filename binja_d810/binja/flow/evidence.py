"""What the view knows, for the flow fixes (v4.0): how addresses are used in a function's MLIL SSA (a
flow.uses.UseGraph), whether addresses are code (a flow.codecheck.CodeView), the file's contents, and the data
variables and types a change touches."""

from __future__ import annotations

import itertools
from typing import Callable, Optional

from binaryninja import MediumLevelILOperation as Op, SectionSemantics, SSAVariable, SymbolBinding, TypeClass

from ...deadline import Deadline
from ...flow import uses as use_rules
from ...flow.tables import MAX_ENTRIES, Read, Span
from . import lowlevel

_KINDS = {
    Op.MLIL_LOAD_SSA: "load", Op.MLIL_LOAD_STRUCT_SSA: "load",
    Op.MLIL_STORE_SSA: "store", Op.MLIL_STORE_STRUCT_SSA: "store",
    Op.MLIL_SET_VAR_SSA: "set_var", Op.MLIL_VAR_PHI: "phi", Op.MLIL_VAR_SSA: "var",
    Op.MLIL_CONST: "const", Op.MLIL_CONST_PTR: "const",
    Op.MLIL_ADD: "add", Op.MLIL_SUB: "sub", Op.MLIL_MUL: "mul", Op.MLIL_LSL: "shl",
    Op.MLIL_XOR: "xor", Op.MLIL_NEG: "neg", Op.MLIL_AND: "and", Op.MLIL_OR: "or", Op.MLIL_NOT: "not",
    Op.MLIL_ZX: "zx", Op.MLIL_SX: "sx", Op.MLIL_LOW_PART: "trunc",
    Op.MLIL_CALL_SSA: "call", Op.MLIL_TAILCALL_SSA: "call", Op.MLIL_CALL_UNTYPED_SSA: "call",
    Op.MLIL_TAILCALL_UNTYPED_SSA: "call", Op.MLIL_JUMP: "jump", Op.MLIL_JUMP_TO: "jump",
}
_FIELD_READS = frozenset({Op.MLIL_VAR_SSA_FIELD, Op.MLIL_VAR_ALIASED, Op.MLIL_VAR_ALIASED_FIELD})
_CONSTANTS = frozenset({Op.MLIL_CONST, Op.MLIL_CONST_PTR})
_LOADS = frozenset({Op.MLIL_LOAD_SSA, Op.MLIL_LOAD_STRUCT_SSA})
_DEFINES = frozenset({"dest", "output"})  # SSA variables named so are written, not read
_SPLIT_SCAN = 64  # function starts looked at between a jump's function and a target
_STEP_BACK = 8  # data variables looked at below an entry read at a constant address
_STRUCT = frozenset({Op.MLIL_LOAD_STRUCT_SSA, Op.MLIL_STORE_STRUCT_SSA})


def _key(ssa_var) -> tuple[int, int]:
    return ssa_var.var.identifier, ssa_var.version


def _operands(node):
    """(name, operand) for each of a node's operands, lists flattened."""
    for name, value, _ in node.detailed_operands:
        for item in value if isinstance(value, list) else (value,):
            yield name, item


class Uses:
    """A flow.uses.UseGraph over one function's MLIL SSA. Nodes are expression indices. A statement is indexed
    when first needed (the statements at a reference, then the readers of each variable followed), so a large
    function costs only the chains that are followed."""

    def __init__(self, ssa):
        self.ssa = ssa
        self.kinds: dict[int, str] = {}
        self.parents: dict[int, tuple[int, str]] = {}
        self.addresses: dict[int, int] = {}
        self.offsets: dict[int, int] = {}  # a struct load's or store's offset from its address
        self.defined: dict[int, list] = {}  # node -> the SSA variables it defines (set_var, phi)
        self.reads: dict[tuple[int, int], list[int]] = {}  # (identifier, version) -> the nodes reading it
        self.children: dict[int, list[tuple[str, int]]] = {}
        self.indexed: set[int] = set()
        self._lowlevel = None

    def reference_read_only(self, address: int, start: int, end: int) -> bool:
        """Only a proven LLIL read can replace missing MLIL evidence; unknown uses still reject."""
        try:
            if self._lowlevel is None:
                self._lowlevel = lowlevel.Uses(self.ssa.source_function)
            return self._lowlevel.read_only(address, start, end)
        except Exception:  # no usable LLIL evidence
            return False

    def index(self, statement) -> None:
        if statement.expr_index in self.indexed:
            return
        self.indexed.add(statement.expr_index)
        stack = [(statement, None)]
        while stack:
            node, up = stack.pop()
            at, op = node.expr_index, node.operation
            self.kinds[at] = "field" if op in _FIELD_READS else _KINDS.get(op, op.name.lower())
            self.addresses[at] = node.address
            if op in _STRUCT:
                self.offsets[at] = node.offset
            if up is not None:
                self.parents[at] = up
                self.children.setdefault(up[0], []).append((up[1], at))
            for name, item in _operands(node):
                if hasattr(item, "operation"):
                    stack.append((item, (at, name)))
                elif isinstance(item, SSAVariable):
                    if name in _DEFINES:
                        self.defined.setdefault(at, []).append(item)
                    else:
                        self.reads.setdefault(_key(item), []).append(at)

    def kind(self, node: int) -> str:
        return self.kinds[node]

    def parent(self, node: int) -> Optional[tuple[int, str]]:
        return self.parents.get(node)

    def readers(self, node: int) -> list[int]:
        return [reader for ssa_var in self.defined.get(node, []) for reader in self.readers_of(ssa_var)]

    def operands(self, node: int) -> list[tuple[str, int]]:
        return self.children.get(node, [])

    def readers_of(self, ssa_var) -> list[int]:
        for use in self.ssa.get_ssa_var_uses(ssa_var):
            self.index(use)
        return list(self.reads.get(_key(ssa_var), []))

    def address(self, node: int) -> int:
        return self.addresses.get(node, 0)

    def constants_in(self, start: int, end: int, at: Optional[int] = None) -> list[int]:
        """The constant nodes in [start, end): those in the statements at address `at` when there are any,
        else every one in the function."""
        found = [n for s in self._statements_at(at) for n in self._constants(s, start, end)] if at is not None else []
        if not found:
            found = [n for s in self.ssa.instructions for n in self._constants(s, start, end)]
        return found

    def _constants(self, statement, start: int, end: int) -> list[int]:
        self.index(statement)
        found, stack = [], [statement]
        while stack:
            node = stack.pop()
            if node.operation in _CONSTANTS and start <= node.constant < end:
                found.append(node.expr_index)
            stack.extend(item for _, item in _operands(node) if hasattr(item, "operation"))
        return found

    def _statements_at(self, address: int) -> list:
        """The SSA statements of the instruction at `address` (through LLIL: Function has no MLIL-at lookup)."""
        try:
            return [self.ssa[m.ssa_form.instr_index] for llil in self.ssa.source_function.get_llils_at(address)
                    for m in llil.mlils]
        except Exception:  # noqa: BLE001 - no IL at that address
            return []


def never_written(view, start: int, end: int, uses_of: Callable,
                  deadline: Deadline = Deadline.never()) -> Optional[str]:
    """None when no visible code writes [start, end) or lets its address escape (spec 3.2), else why not.
    `uses_of(func)` gives a function's Uses, or None without MLIL (shared within a round). A key read across
    many functions (v4.3 spec 3.2) is checked until `deadline`."""
    for source in view.get_data_refs(start, end - start):
        return f"data reference from {source:#x}"
    for ref in view.get_code_refs(start, end - start):
        if deadline.expired():
            return "too many readers to check in time"
        func = ref.function
        if func is None:
            return f"reference at {ref.address:#x} outside a function"
        uses = uses_of(func)
        if uses is None:
            return f"no MLIL for the function at {func.start:#x}"
        starts = uses.constants_in(start, end, at=ref.address)
        if not starts:
            if uses.reference_read_only(ref.address, start, end):
                continue
            return f"reference at {ref.address:#x} not seen in MLIL"
        verdict, where = use_rules.classify(starts, uses)
        if verdict != use_rules.READ:
            return f"{verdict} at {uses.address(where):#x}"
    return None


def key_problem(view, start: int, end: int, read: Read) -> Optional[str]:
    """None when [start, end) may hold a key (v4.3 spec 3.2): the program's own value, not exported (another
    module may write it), not relocated, not an address in the image (the loader's value, or a pointer) and not
    zero (an uninitialized global reads as zero until something writes it; a zero key encodes nothing)."""
    for symbol in view.get_symbols(start, end - start):
        if symbol.binding in (SymbolBinding.GlobalBinding, SymbolBinding.WeakBinding):
            return f"{symbol.name} is exported"
    if view.range_contains_relocation(start, end - start):
        return "relocated"
    value = read(start, end - start)
    if value is None:
        return f"cannot read {start:#x}"
    if view.get_segment_at(value) is not None:
        return "an address in the image"
    if value == 0:
        return "zero"
    return None


def widest_read(view, address: int, size: int, uses_of: Callable, cap: int, limit: int = 256) -> int:
    """The widest load at exactly `address` (at least `size`, at most `cap` bytes) in the functions that reference
    it, so a key read whole by one function and a byte at a time by another is typed once, whole (v4.3 spec 3.2)."""
    widest = size
    for ref in itertools.islice(view.get_code_refs(address, 1), limit):
        uses = uses_of(ref.function) if ref.function is not None else None
        for statement in uses._statements_at(ref.address) if uses is not None else ():
            for node in statement.traverse(lambda n: n if n.operation in _LOADS else None):
                offset = node.offset if node.operation == Op.MLIL_LOAD_STRUCT_SSA else 0
                if node.src.operation in _CONSTANTS and node.src.constant + offset == address:
                    widest = max(widest, node.size)
    return min(widest, cap)


def writable_data(view, start: int, end: int) -> bool:
    """[start, end) lies in a writable, non-executable segment and is not constant already."""
    segment = view.get_segment_at(start)
    return (segment is not None and segment.writable and not segment.executable and end <= segment.end
            and not constant_data(view, start, end))


class BinaryCode:
    """flow.codecheck.CodeView over a view, for one architecture."""

    def __init__(self, view, arch):
        self.view, self.arch = view, arch
        self.code_sections = [s for s in view.sections.values()
                              if s.semantics == SectionSemantics.ReadOnlyCodeSectionSemantics]

    def is_code(self, address: int) -> bool:
        if not self.view.is_offset_executable(address):
            return False
        return not self.view.sections or any(s.start <= address < s.end for s in self.code_sections)

    def decodes(self, address: int) -> bool:
        data = self.view.read(address, self.arch.max_instr_length)
        if not data:
            return False
        info = self.arch.get_instruction_info(data, address)
        return info is not None and info.length > 0

    def owners(self, address: int) -> list[int]:
        return [f.start for f in self.view.get_functions_containing(address)]

    def function_at(self, address: int) -> bool:
        return self.view.get_function_at(address) is not None

    def split_off(self, function: int, address: int) -> bool:
        view = self.view
        if address <= function or view.get_symbol_at(address) is not None:
            return False
        found = view.get_function_at(address)
        if found is None or not found.auto or found.callers or next(iter(view.get_code_refs(address)), None):
            return False
        start = view.get_next_function_start_after(function)
        for _ in range(_SPLIT_SCAN):
            if start >= address:  # view.end when there is none
                return True
            between = view.get_function_at(start)
            if view.get_symbol_at(start) is not None or (between is not None and between.callers):
                return False  # a called or named function lies between: not the jumping function's code
            start = view.get_next_function_start_after(start)
        return False


def reader(view) -> Read:
    """flow.tables.Read over the view: the unsigned value at an address, or None where it cannot be read."""
    def read(address: int, size: int) -> Optional[int]:
        try:
            return view.read_int(address, size, False)
        except ValueError:
            return None
    return read


def object_start(view, address: int, base: Optional[int] = None, uses_of: Optional[Callable] = None) -> int:
    """Where the never-written check of the object holding `address` starts: the constant an indexed or offset
    address starts from (within MAX_ENTRIES words below); else the start of the data variable holding
    `address`. With `uses_of`, an entry read at a constant address starts at the nearest code-referenced data
    variable below it instead, when code uses that one as a pointer: its table, passed or indexed elsewhere
    (spec section 7)."""
    reach = 8 * MAX_ENTRIES
    if base is not None and address - reach <= base <= address:
        return base
    first = view.get_previous_data_var_start_before(address + 1)  # the last start at or before `address`
    if not address - reach <= first <= address:
        first = address
    if uses_of is None:
        return first
    at = first
    for _ in range(_STEP_BACK):
        below = view.get_previous_data_var_start_before(at)  # `at` when there is none
        if below >= at or below < address - reach:
            break
        if next(iter(view.get_code_refs(below, 1)), None) is not None:
            return below if pointer_use(view, below, uses_of) else first
        at = below
    return first


def pointer_use(view, address: int, uses_of: Callable) -> bool:
    """Code uses `address` as a pointer: anything but a plain load or store of it (passed, stored, copied or
    offset), so the object there may extend past it."""
    for ref in view.get_code_refs(address, 1):
        uses = uses_of(ref.function) if ref.function is not None else None
        if uses is None:
            return True
        for node in uses.constants_in(address, address + 1, at=ref.address):
            up = uses.parent(node)
            if up is None:
                return True
            parent, operand = up
            if (uses.kind(parent), operand) not in (("load", "src"), ("store", "dest")) or uses.offsets.get(parent):
                return True
    return False


def referencing(view, start: int, end: int) -> set[int]:
    """The starts of the functions with a code reference into [start, end): reanalyzed after a retype."""
    return {ref.function.start for ref in view.get_code_refs(start, end - start) if ref.function is not None}


def data_vars(view, start: int, end: int) -> list:
    """The data variables holding any byte of [start, end), ascending."""
    starts = []
    first = view.get_previous_data_var_start_before(start + 1)  # start + 1 when there is none
    if first <= start:
        starts.append(first)
    at = view.get_next_data_var_start_after(start)  # view.end when there is none
    while start < at < end:
        starts.append(at)
        following = view.get_next_data_var_start_after(at)
        if following <= at:
            break
        at = following
    found = []
    for address in starts:
        var = view.get_data_var_at(address)
        if var is not None and var.address == address and address + max(var.type.width, 1) > start:
            found.append(var)
    return found


def is_const(t) -> bool:
    """Whether the type's value is const (an array's elements, a pointer itself)."""
    if t.type_class == TypeClass.ArrayTypeClass:
        return is_const(t.element_type)
    flag = t.const
    return bool(getattr(flag, "value", flag))


def constant_data(view, start: int, end: int) -> bool:
    """Every byte of [start, end) is constant already: in a segment that is not writable, or inside one const
    data variable."""
    segment = view.get_segment_at(start)
    if segment is not None and not segment.writable and end <= segment.end:
        return True
    return any(v.address <= start and end <= v.address + v.type.width and is_const(v.type)
               for v in data_vars(view, start, end))


def define_type(view, address: int, t) -> None:
    """A data variable the flow fixes define: a user type, as Binary Ninja's analysis replaces an auto one (its
    jump-table analysis retypes a table it resolved), kept out of the user's undo history."""
    state = view.begin_undo_actions()
    try:
        view.define_user_data_var(address, t)
    finally:
        view.forget_undo_actions(state)


def undefine_type(view, address: int) -> None:
    """Remove a data variable define_type made, kept out of the user's undo history."""
    state = view.begin_undo_actions()
    try:
        view.undefine_user_data_var(address)
    finally:
        view.forget_undo_actions(state)


def type_text(t) -> str:
    """A type as text that parses back, named __d810: "uint64_t const __d810[2]"."""
    return f"{t.get_string_before_name()} __d810{t.get_string_after_name()}"


def parse_type(view, text: str):
    """The type `text` declares (type_text's form), or None."""
    try:
        return view.parse_type_string(text)[0]
    except Exception:  # noqa: BLE001 - Binary Ninja raises SyntaxError, or worse, on text it cannot parse
        return None


def const_type(view, size: int, count: int):
    """const uintN_t, or an array of `count` of them."""
    return parse_type(view, f"uint{8 * size}_t const __d810" + (f"[{count}]" if count > 1 else ""))


def const_copy(view, t):
    """`t` made const: the value itself (for a pointer the pointer, not what it points to). None if that does not
    parse."""
    return parse_type(view, f"{t.get_string_before_name()} const __d810{t.get_string_after_name()}")


def table_type(view, span: Span):
    """The type a passing table or key global gets: a const copy of the variable already spanning exactly its one
    entry, else const uintN_t[count]."""
    var = view.get_data_var_at(span.start)
    if span.count == 1 and var is not None and var.type.width == span.size:
        return const_copy(view, var.type)
    return const_type(view, span.size, span.count)
