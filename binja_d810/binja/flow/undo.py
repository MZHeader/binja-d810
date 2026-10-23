"""Undoing the flow fixes' changes (v4.0, spec 3.1): each goes back to what it replaced, unless the user changed
it since (a user type, a user function, a user inline flag stays). Each returns the functions to reanalyze."""

from __future__ import annotations

from binaryninja.enums import InlineDuringAnalysis
from binaryninja.types import InlineDuringAnalysisWithConfidence

from . import evidence


def revert(view, change) -> set[int]:
    if change.kind.startswith("comment:"):
        from .. import annotations
        return annotations.revert(view, change)
    if change.kind == "callee_inline":
        func = view.get_function_at(change.key)
        if func is not None:
            flag = func.inline_during_analysis
            if [int(flag.value), flag.confidence] == change.detail["applied_flag"]:
                previous, confidence = change.detail["previous_flag"]
                state = view.begin_undo_actions()
                try:
                    func.set_user_inline_during_analysis(
                        InlineDuringAnalysisWithConfidence(InlineDuringAnalysis(previous), confidence))
                finally:
                    view.commit_undo_actions(state)
        return {change.key, *change.detail["callers"]}
    if change.kind == "signature":
        func = view.get_function_at(change.key)
        if func is not None and not func.has_user_type:
            t = evidence.parse_type(view, change.detail["type"])
            if t is not None:
                func.set_auto_type(t)
        return {change.key}
    if change.kind in ("table", "range"):
        return _retype(view, change)
    if change.kind == "function":
        func = view.get_function_at(change.key)
        if func is None or not func.auto or func.has_user_type:
            return set()
        callers = {caller.start for caller in func.callers}
        view.remove_function(func)
        return callers
    if change.kind == "split":
        owner = view.get_function_at(change.detail["function"])
        if view.get_function_at(change.key) is None:
            view.add_function(change.key, owner.platform if owner is not None else None, auto_discovered=True)
        return {change.detail["function"]}
    if change.kind == "inline":
        func = view.get_function_at(change.key)
        if func is not None:
            func.set_auto_inline_during_analysis(
                InlineDuringAnalysisWithConfidence(InlineDuringAnalysis.DoNotInlineCall, 0))
        return {change.key, *change.detail["callers"]}
    if change.kind == "branches":
        return {change.detail["function"]}
    return set()


def _retype(view, change) -> set[int]:
    """Our types go, where they are still ours (define_type makes user types: Task 7 ruling); what they replaced
    comes back where nothing else is."""
    for address, text in change.detail["defined"]:
        var = view.get_data_var_at(address)
        if var is not None and var.address == address and evidence.type_text(var.type) == text:
            evidence.undefine_type(view, address)
    for address, text in change.previous:
        var = view.get_data_var_at(address)
        if var is None or (var.address == address and var.auto_discovered):
            t = evidence.parse_type(view, text)
            if t is not None:
                view.define_data_var(address, t)
    if "symbols" in change.detail:
        before = {tuple(s) for s in change.detail["symbols"]}
        for symbol in view.get_symbols(change.key, change.detail["end"] - change.key):
            if (symbol.auto and symbol.raw_name == f"jump_table_{symbol.address:x}"
                    and (symbol.address, symbol.raw_name) not in before):
                view.undefine_auto_symbol(symbol)
    return (evidence.referencing(view, change.detail.get("from", change.key), change.detail["end"])
            | set(change.detail.get("readers", [])))
