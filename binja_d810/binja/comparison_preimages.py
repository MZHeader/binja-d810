"""Expose encoded equalities in MLIL/HLIL with exact proofs and version-safe inputs."""
from dataclasses import dataclass,field
import time
from binaryninja import MediumLevelILOperation as M,HighLevelILOperation as H,ILInstructionAttribute as Attr
from ..semantic.preimage import comparison
from ..semantic.verify import check,Verdict
from ..ir.expr import BinOp,tree_size
from .control_cleanup import Work,BudgetExceeded,tree_nodes
from .adapter import Candidate
from .mlil import lift as mlift
from .hlil import lift as hlift
from .mlil.adapter import MLILAdapter
from .hlil.adapter import HLILAdapter

_PROTECTED={Attr.ILPreventDeadStoreElimination,Attr.ILPreventAliasAnalysis}


@dataclass(frozen=True)
class Limits:
    instructions:int=20000
    nodes:int=2000
    work:int=200000
    sites:int=128
    steps:int=64
    rounds:int=2
    proof_ms:int=200


@dataclass
class Report:
    changed:int=0
    notes:list[str]=field(default_factory=list)
    seconds:float=0.0
    cpu_seconds:float=0.0
    exhausted:bool=False


class Guarded:
    def expr(self,node,*,inlined=False):
        self.work.tick()
        if node.attributes & _PROTECTED:
            raise ValueError('protected comparison input')
        return super().expr(node,inlined=inlined)

    def var(self,var,width):
        if var.var.type is not None and var.var.type.volatile:
            raise ValueError('volatile comparison input')
        definition=self.definition_of(var)
        if definition is not None and definition.attributes & _PROTECTED:
            raise ValueError('protected comparison definition')
        return super().var(var,width)


class MLifter(Guarded,mlift._Lifter):
    def __init__(self,il,root,limits,work):
        super().__init__(il.ssa_form,root,True,limits.nodes)
        self.work=work
        self.dataflow=False


class HLifter(Guarded,hlift._Lifter):
    def __init__(self,root,aliased,limits,work):
        super().__init__(root,aliased,True,limits.nodes,False)
        self.work=work


def roots(il,level,tick):
    if level=='hlil':
        protected={}
        for node in tree_nodes(il.root,tick):
            tick()
            blocker=protected.get(node.expr_index)
            if node.attributes & _PROTECTED:blocker=node
            for child in hlift.children(node):
                if blocker is not None:protected[child.expr_index]=blocker
            if node.operation in (H.HLIL_CMP_E,H.HLIL_CMP_NE):yield blocker if blocker is not None else node,node
    else:
        for statement in il.instructions:
            pending=[(statement,None)]
            while pending:
                tick();node,blocker=pending.pop()
                if node.attributes & _PROTECTED:blocker=node
                if node.operation in (M.MLIL_CMP_E,M.MLIL_CMP_NE):yield blocker if blocker is not None else statement,node
                for operand in node.operands:
                    if isinstance(operand,list):pending.extend((n,blocker) for n in operand if hasattr(n,'operation'))
                    elif hasattr(operand,'operation'):pending.append((operand,blocker))


def run(il,level,limits,deadline):
    report=Report();started,cpu=time.monotonic(),time.thread_time()
    adapter=(MLILAdapter if level=='mlil' else HLILAdapter)(il,limits.nodes,deadline)
    work=Work(limits,deadline);left=limits.sites;committing=False
    try:
        if len(il)>limits.instructions:raise ValueError('comparison instruction limit')
        for _ in range(limits.rounds):
            planned=[]
            for statement,root in roots(il,level,work.tick):
                if left<=0:
                    report.notes.append('comparison candidate limit');break
                left-=1
                try:
                    if statement.attributes & _PROTECTED:
                        raise ValueError('protected comparison statement')
                    lifter=MLifter(il,statement,limits,work) if level=='mlil' else HLifter(root.ssa_form,adapter.aliased,limits,work)
                    original=lifter.expr(root.ssa_form)
                    if original is None:
                        report.notes.append(f'{root.address:#x}: unmodeled or unstable comparison input');continue
                    original=lifter.relate(original) if level=='mlil' else hlift._relate(original)
                    if not isinstance(original,BinOp) or original.op not in ('eq','ne') or original.left.width==1:continue
                    found=comparison(original,steps=limits.steps,max_nodes=limits.nodes,tick=work.tick)
                    if found is None:continue
                    visible=adapter._visible(statement,root,lifter.inlined)[0] if level=='mlil' else adapter._visible(root,lifter.inlined)[0]
                    if tree_size(found.expression)>visible:
                        report.notes.append(f'{root.address:#x}: preimage would enlarge visible code');continue
                    proof=check(original,found.expression,z3_timeout_ms=limits.proof_ms,deadline=deadline)
                    if proof.verdict is not Verdict.PROVEN:
                        report.notes.append(f'{root.address:#x}: comparison not proven: {proof.reason or proof.verdict.value}');continue
                    planned.append((Candidate(root,root.address,original),found,proof))
                except BudgetExceeded:
                    report.exhausted=True;break
                except (ValueError,RecursionError,ArithmeticError) as exc:
                    report.notes.append(f'{root.address:#x}: {exc}')
            if not planned:break
            dead_before=adapter.dead_assignments()
            committing=True
            for candidate,found,_ in planned:adapter.replace(candidate,found.expression)
            adapter.finish()
            if not deadline.expired():
                newly_dead=adapter.dead_assignments()-dead_before
                if newly_dead:adapter.remove(newly_dead);adapter.finish()
            committing=False
            report.changed+=len(planned)
            report.notes.extend(f'{candidate.address:#x}: inverted {found.steps} scalar encodings, proven by {proof.method}'
                                for candidate,found,proof in planned)
            if report.exhausted:break
    except (ValueError,RecursionError) as exc:
        if committing:
            adapter.restore();report.changed=0
            report.notes.append('comparison rewrites rolled back')
        report.notes.append(str(exc))
        report.exhausted|=work.left<0 or deadline.expired()
    except Exception:
        adapter.restore();raise
    finally:
        report.seconds=time.monotonic()-started;report.cpu_seconds=time.thread_time()-cpu
    return report
