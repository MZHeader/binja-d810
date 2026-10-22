"""The small architecture boundary for ordinary call links; no instruction-text matching."""

from binaryninja import LowLevelILOperation as Op


CALL_MODELS = {"aarch64": "link", "x86_64": "stack", "x86": "stack"}


def target(func, node):
    if node.operation != Op.LLIL_CALL:
        raise ValueError("not an ordinary call")
    if node.attributes:
        raise ValueError("call has unmodeled attributes")
    if node.dest.operation not in (Op.LLIL_CONST, Op.LLIL_CONST_PTR):
        raise ValueError("indirect call")
    if func.get_call_type_adjustment(node.address) is not None:
        raise ValueError("user call type")
    callee = func.view.get_function_at(node.dest.constant)
    if callee is None or callee.arch != func.arch:
        raise ValueError("callee unavailable")
    flag = callee.inline_during_analysis
    if not flag.value and flag.confidence:
        raise ValueError("callee has an explicit no-inline flag")
    return callee


def return_address(func, node):
    info = func.arch.get_instruction_info(func.view.read(node.address, func.arch.max_instr_length), node.address)
    if info is None or info.length <= 0:
        raise ValueError("call length unavailable")
    return node.address + info.length
