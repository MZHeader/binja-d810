"""Select a Boolean guard only after an exact equivalence proof."""
from ..ir.expr import Const
from ..ir.normalize import normalize
from .verify import check,Verdict


def prove(guard,*,proof_ms,deadline):
    if guard.width!=1 or deadline.expired():return None
    candidate=normalize(guard)
    if not isinstance(candidate,Const):return None
    result=check(guard,candidate,z3_timeout_ms=proof_ms,deadline=deadline)
    return candidate if result.verdict is Verdict.PROVEN else None
