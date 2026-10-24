"""Replace predicates over bounded pure recurrences by complete input preimages."""
from dataclasses import dataclass,field
from types import SimpleNamespace
import time
from binaryninja import HighLevelILOperation as H,Endianness
from ..control.symbolic_loop import summarize
from ..control.finite import abstract_value
from ..semantic.finite_preimage import Limits as SearchLimits,recover
from ..semantic.preimage import comparison
from ..semantic.project_preimage import project
from ..semantic.verify import Verdict,check
from ..ir.expr import BinOp,Const,UnOp,Var,mask,substitute,tree_size,variables
from ..ir.normalize import normalize
from .control_cleanup import Work,BudgetExceeded,tree_nodes,structure,protected_nodes
from .dead_loops import Model as LoopModel,_LOOPS
from .hlil.adapter import HLILAdapter
from .adapter import Candidate
from .storage import field_read
from .hlil.lift import _BINARY


@dataclass(frozen=True)
class Limits:
    instructions:int=20000
    nodes:int=2000
    work:int=16000000
    loops:int=64
    sites:int=128
    iterations:int=64
    stages:int=64
    domain_bits:int=8
    states:int=65536
    solutions:int=16
    rounds:int=2
    bits:int=64
    proof_ms:int=200


@dataclass
class Report:
    changed:int=0
    states:int=0
    notes:list[str]=field(default_factory=list)
    seconds:float=0.0
    cpu_seconds:float=0.0
    exhausted:bool=False


@dataclass
class Value:
    expression:object
    invalid:set


class Model(LoopModel):
    def __init__(self,il,limits,work):
        super().__init__(il,limits,work)
        self.little=il.arch.endianness==Endianness.LittleEndian

    def variable(self,var,fallback):
        value=super().variable(var,fallback)
        if value.ref is None:
            value=Var(value.name,value.width,SimpleNamespace(var=var))
            self.variables[var.identifier]=value
        return value

    def field(self,node):
        if not self.little:raise ValueError('big-endian scalar field layout is unproved')
        width=node.size*8;offset=0;source=node
        while source.operation==H.HLIL_STRUCT_FIELD:
            self.check(source)
            if source.member_index is not None:raise ValueError('named structure field is unsupported')
            offset+=source.offset*8;source=source.src
        if source.operation!=H.HLIL_VAR:raise ValueError('non-scalar field backing')
        self.check(source)
        backing=self.variable(source.var,source.size*8 or 1)
        if width<=0 or offset<0 or offset+width>backing.width:raise ValueError('field outside scalar backing')
        return backing,offset,width

    def expression(self,node):
        if node.operation==H.HLIL_STRUCT_FIELD:
            backing,offset,width=self.field(node)
            return field_read(backing,offset,width)
        if node.operation in _BINARY:
            self.check(node);width=node.size*8 or 1
            if width>self.limits.bits:raise ValueError('scalar width limit')
            a,b=self.expression(node.left),self.expression(node.right)
            # Native HLIL can embed a Boolean result in integer arithmetic without a cast.
            if width>1:
                if a.width==1:a=UnOp('zx',a,width)
                if b.width==1:b=UnOp('zx',b,width)
            return BinOp(_BINARY[node.operation],a,b,width)
        return super().expression(node)

    def partial(self,node):
        self.check(node)
        if node.operation!=H.HLIL_ASSIGN or node.dest.operation!=H.HLIL_STRUCT_FIELD:return None
        return self.field(node.dest)


class Scope:
    def __init__(self,model,limits,work,report):
        self.model,self.limits,self.work,self.report=model,limits,work,report
        self.constants={};self.summaries={};self.cache={}

    def invalidate(self,names):
        for name in names:self.summaries.pop(name,None)
        for value in self.summaries.values():value.invalid.update(names)

    def ordinary(self,node):
        if node.operation==H.HLIL_VAR_DECLARE:
            name=self.model.variable(node.var,node.size*8 or 1).name
            self.invalidate({name});self.constants.pop(name,None);return
        partial=self.model.partial(node)
        if partial is not None:
            target,offset,width=partial
            value=abstract_value(self.model.expression(node.src),self.constants,max_nodes=self.limits.nodes,tick=self.work.tick)
            old=self.constants.get(target.name)
            self.invalidate({target.name})
            if value is None or old is None:self.constants.pop(target.name,None)
            else:self.constants[target.name]=(old&~(mask(width)<<offset))|((value&mask(width))<<offset)
            return
        for assignment in self.model.block(node):
            value=abstract_value(assignment.value,self.constants,max_nodes=self.limits.nodes,tick=self.work.tick)
            self.invalidate({assignment.target.name})
            if value is None:self.constants.pop(assignment.target.name,None)
            else:self.constants[assignment.target.name]=value

    def loop(self,node):
        program=self.model.loop(node)
        initial={v.name:Const(self.constants[v.name],v.width) for v in self.model.variables.values() if v.name in self.constants}
        summary=summarize(program,initial,iterations=self.limits.iterations,max_nodes=self.limits.nodes,tick=self.work.tick)
        self.invalidate(summary.written)
        for name,expression in summary.values.items():
            self.summaries[name]=Value(expression,set(summary.written))
            if isinstance(expression,Const):self.constants[name]=expression.value
            else:self.constants.pop(name,None)

    def predicate(self,expression):
        self.work.tick()
        if not isinstance(expression,BinOp) or expression.op not in ('eq','ne'):return None
        simplified=comparison(expression,steps=self.limits.stages,max_nodes=self.limits.nodes,tick=self.work.tick)
        condition=simplified.expression if simplified is not None else expression
        if not isinstance(condition,BinOp) or condition.op not in ('eq','ne'):return None
        source,target=condition.left,condition.right
        if isinstance(source,Const):source,target=target,source
        if not isinstance(target,Const):return None
        if isinstance(source,Var) and source.name in self.summaries:
            value=self.summaries[source.name]
            if source.width!=value.expression.width:return None
            expanded,invalid=value.expression,value.invalid
        else:
            # Earlier passes can peel the final operation out of a loop result. Rebuild that
            # expression from current loop outputs only when their entry inputs are still live.
            mapping={}
            for variable in variables(source):
                value=self.summaries.get(variable.name)
                if value is not None and value.expression.width==variable.width and not any(
                    v.name in value.invalid for v in variables(value.expression)):
                    mapping[variable.name]=value.expression
            if not mapping:return None
            expanded=normalize(substitute(source,mapping));invalid=set()
            if tree_size(expanded)>self.limits.nodes:raise ValueError('composed recurrence expression limit')
        if simplified is not None:
            proof=check(expression,condition,z3_timeout_ms=self.limits.proof_ms,deadline=self.work.deadline)
            if proof.verdict is not Verdict.PROVEN:raise ValueError('recurrence comparison encoding not proven')
        key=(expanded,target.value)
        if key not in self.cache:
            if isinstance(expanded,Const):
                predicate=Const(int(expanded.value==target.value),1);states=solutions=0
            else:
                result=recover(expanded,target.value,
                               limits=SearchLimits(self.limits.nodes,self.limits.stages,self.limits.domain_bits,
                                                   self.limits.states,self.limits.solutions),tick=self.work.tick)
                if result is None:return None
                predicate=result.expression;states=result.states;solutions=result.solutions
                predicate=project(predicate,proof_ms=self.limits.proof_ms,deadline=self.work.deadline)
            self.cache[key]=(predicate,states,solutions)
        predicate,states,solutions=self.cache[key]
        if condition.op=='ne':predicate=normalize(UnOp('not',predicate,1))
        if any(v.name in invalid or v.ref is None for v in variables(predicate)):
            raise ValueError('recurrence inputs changed before their predicate')
        if tree_size(predicate)>min(self.limits.nodes,tree_size(expanded)):
            raise ValueError('recurrence predicate would enlarge the summarized expression')
        return predicate,states,solutions

    def rewrite(self,expression):
        changes=[]
        def visit(node):
            self.work.tick()
            found=self.predicate(node)
            if found is not None:
                changes.append(found);return found[0]
            if isinstance(node,BinOp):return BinOp(node.op,visit(node.left),visit(node.right),node.width)
            if isinstance(node,UnOp):return UnOp(node.op,visit(node.operand),node.width)
            return node
        result=normalize(visit(expression))
        if changes:
            for bit in (0,1):
                constant=Const(bit,result.width)
                proof=check(result,constant,z3_timeout_ms=self.limits.proof_ms,deadline=self.work.deadline)
                if proof.verdict is Verdict.PROVEN:
                    result=constant;break
        return result,changes


def expression_roots(node):
    if node.operation==H.HLIL_IF:return [node.condition]
    if node.operation==H.HLIL_VAR_INIT:return [node.src]
    if node.operation==H.HLIL_ASSIGN and node.dest.operation in (H.HLIL_VAR,H.HLIL_STRUCT_FIELD):return [node.src]
    if node.operation==H.HLIL_RET:return list(node.src)
    return []


def plans(il,limits,work,report,remaining):
    nodes=tree_nodes(il.root,work.tick)
    protected=protected_nodes(il.root,work.tick)
    for block in nodes:
        if block.operation!=H.HLIL_BLOCK or block.expr_index in protected:continue
        scope=Scope(Model(il,limits,work),limits,work,report)
        for statement in block.body:
            work.tick()
            try:
                scope.model.check(statement)
                if statement.operation in _LOOPS:
                    if remaining['loops']<=0:raise ValueError('recurrence loop limit')
                    remaining['loops']-=1;scope.loop(statement);continue
                if scope.summaries:
                    roots=expression_roots(statement)
                    if statement.operation in (H.HLIL_VAR_INIT,H.HLIL_ASSIGN):
                        if scope.model.partial(statement) is None:
                            scope.model.block(statement)  # also checks protected/aliased destinations
                    originals=[scope.model.expression(root) for root in roots]
                    for root,original in zip(roots,originals):
                        if remaining['sites']<=0:raise ValueError('recurrence predicate limit')
                        remaining['sites']-=1
                        replacement,changes=scope.rewrite(original)
                        if changes and replacement!=original:
                            yield Candidate(root,root.address,original),replacement,changes
                scope.ordinary(statement)
            except BudgetExceeded:
                raise
            except (ValueError,ArithmeticError,RecursionError) as exc:
                report.notes.append(f'{statement.address:#x}: {exc}')
                scope.constants.clear();scope.summaries.clear()


def run(il,limits,deadline):
    report=Report();started,cpu=time.monotonic(),time.thread_time()
    adapter=HLILAdapter(il,limits.nodes,deadline);work=Work(limits,deadline)
    remaining={'loops':limits.loops,'sites':limits.sites};committing=False
    try:
        if len(il)>limits.instructions:raise ValueError('recurrence instruction limit')
        for _ in range(limits.rounds):
            if not any(n.operation in _LOOPS for n in structure(il.root,work.tick)):break
            planned=[]
            try:
                for plan in plans(il,limits,work,report,remaining):planned.append(plan)
            except BudgetExceeded:
                report.exhausted=True;report.notes.append('recurrence budget exhausted; completed proofs retained')
            if not planned:break
            committing=True
            for candidate,replacement,_ in planned:adapter.replace(candidate,replacement)
            adapter.finish();committing=False
            report.changed+=sum(len(changes) for _,_,changes in planned)
            for candidate,_,changes in planned:
                states=sum(c[1] for c in changes);report.states+=states
                report.notes.append(f'{candidate.address:#x}: {len(changes)} recurrence predicates proved by complete preimages; {states} state transitions')
            if report.exhausted:break
    except (ValueError,RecursionError) as exc:
        if committing:
            adapter.restore();report.changed=report.states=0
            report.notes.append('recurrence rewrites rolled back')
        report.notes.append(str(exc));report.exhausted|=work.left<0 or deadline.expired()
    except Exception:
        adapter.restore();raise
    finally:
        report.seconds=time.monotonic()-started;report.cpu_seconds=time.thread_time()-cpu
    return report
