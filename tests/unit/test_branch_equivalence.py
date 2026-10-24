import pytest
from binja_d810.control.equivalence import Assignment, equivalent
from binja_d810.deadline import Deadline
from binja_d810.ir.expr import Var
from binja_d810.ir.parse import parse


def prove(left, right, outputs, width=32, **kwargs):
    assignments=lambda rows:[Assignment(Var(k,width),parse(v,width)) for k,v in rows]
    return equivalent(assignments(left),assignments(right),[Var(k,width) for k in outputs],
                      max_nodes=kwargs.get('max_nodes',200),proof_ms=200,deadline=Deadline.after(2),
                      tick=kwargs.get('tick',lambda:None))[0]


@pytest.mark.parametrize('width',(8,16,32,64))
def test_equivalent_outputs_with_different_intermediates(width):
    assert prove([('t','x ^ y'),('out','t ^ y')],[('t','x + y'),('out','t - y')],['out'],width)
    assert not prove([('t','x ^ y'),('out','t ^ y')],[('t','x + y'),('out','t - y')],['out','t'],width)


def test_ordered_writes_and_untouched_outputs():
    assert prove([('x','x + 1'),('x','x - 1')],[],['x'])
    assert not prove([('x','x + 1'),('y','x')],[('y','x'),('x','x + 1')],['x','y'])


def test_unknown_inputs_are_never_assumed_equal():
    assert not prove([('out','x')],[('out','y')],['out'])
    assert not prove([('out','x')],[],['out'])


def test_budget_and_expression_caps():
    def stop():raise ValueError('work limit')
    with pytest.raises(ValueError,match='work limit'):
        prove([('out','x')],[('out','x')],['out'],tick=stop)
    with pytest.raises(ValueError,match='expression limit'):
        prove([('out','x + y')],[('out','x + y')],['out'],max_nodes=2)


def test_inconclusive_proofs_do_not_authorize_a_rewrite(monkeypatch):
    from binja_d810.control import equivalence
    from binja_d810.semantic.verify import Check,Verdict
    monkeypatch.setattr(equivalence,'check',lambda *a,**k:Check(Verdict.TESTED,'sampling','timeout'))
    assert not prove([('out','x')],[('out','x')],['out'])
