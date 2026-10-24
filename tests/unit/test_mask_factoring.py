import random
import pytest
from binja_d810.deadline import Deadline
from binja_d810.ir.expr import BinOp,Const,Var,mask,tree_size
from binja_d810.semantic.bitwise import BitwiseBackend
from binja_d810.semantic.verify import Verdict,check_equivalent


@pytest.mark.parametrize('width',(8,16,32,64))
def test_shared_terms_across_mask_partitions_are_factored(width):
    x,y=Var('x',width),Var('y',width)
    m=mask(width)//3
    source=BinOp('or',BinOp('and',x,Const(~m,width),width),
                 BinOp('and',BinOp('xor',x,y,width),Const(m,width),width),width)
    found=BitwiseBackend().simplify(source)
    expected=BinOp('xor',x,BinOp('and',y,Const(m,width),width),width)
    assert found is not None and tree_size(found)<=tree_size(expected)
    assert check_equivalent(source,found) is Verdict.PROVEN


def test_disabled_and_configured_limits():
    from binja_d810.ir.parse import parse
    source=parse('(x & 0xaa) | ((x ^ y) & 0x55)',8)
    assert BitwiseBackend(mask_factoring=False).simplify(source) is None
    assert BitwiseBackend(max_names=1).simplify(source) is None
    assert BitwiseBackend(max_constants=1).simplify(source) is None
    assert BitwiseBackend().simplify(source,Deadline.after(0)) is None


def test_deadline_during_enumeration():
    from binja_d810.ir.parse import parse
    class DeadlineProbe:
        calls=0
        def expired(self):
            self.calls+=1
            return self.calls>=3
    deadline=DeadlineProbe()
    assert BitwiseBackend().simplify(parse('(x & 0xaa) | ((x ^ y) & 0x55)',8),deadline) is None
    assert deadline.calls==3


def test_pipeline_refuses_sampling_only_rewrites():
    from binja_d810.ir.parse import parse
    from binja_d810.semantic.verify import Check
    from binja_d810.simplify import simplify_expression
    source=parse('(x & 0xaa) | ((x ^ y) & 0x55)',8)
    tested=simplify_expression(source,[],[BitwiseBackend()],check=lambda a,b:Check(Verdict.TESTED,'sampling','z3 timeout'))
    assert tested.rewrite is None
    assert tested.skipped=='rewrite not proven: z3 timeout'
    proven=simplify_expression(source,[],[BitwiseBackend()])
    assert proven.rewrite and proven.rewrite.verdict is Verdict.PROVEN


@pytest.mark.parametrize('width',(8,16,32,64))
def test_random_masks_and_arithmetic_atoms(width):
    rng=random.Random(width)
    x,y,z=(Var(n,width) for n in ('x','y','z'))
    a=BinOp('add',x,z,width)
    for _ in range(50):
        m=rng.randrange(1,mask(width))
        source=BinOp('or',BinOp('and',a,Const(~m,width),width),
                     BinOp('and',BinOp('xor',a,y,width),Const(m,width),width),width)
        found=BitwiseBackend().simplify(source)
        assert found is not None
        assert check_equivalent(source,found) is Verdict.PROVEN


def test_general_truth_tables_match_exact_proofs():
    from binja_d810.semantic.bitwise import _masked_polynomial
    from binja_d810.ir.evaluate import evaluate
    rng=random.Random(81)
    names=[Var(n,16) for n in ('a','b','c','d')]
    for _ in range(100):
        source=rng.choice(names)
        for _ in range(12):
            leaf=rng.choice(names+[Const(rng.getrandbits(16),16)])
            source=BinOp(rng.choice(('and','or','xor')),source,leaf,16)
        table=[evaluate(source,{n.name:65535 if row&(1<<i) else 0 for i,n in enumerate(names)}) for row in range(16)]
        found=_masked_polynomial(table,names,16,Deadline.never())
        assert check_equivalent(source,found) is Verdict.PROVEN
