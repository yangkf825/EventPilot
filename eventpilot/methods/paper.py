"""Public-input-only adaptations of CoT, ReAct, Reflexion and Plan-and-Solve.

No dataset/evaluator imports. Reflexion uses local action/decision-selection
trials with self-evaluation and role-isolated verbal memory, NOT private rewards
or reset/replay of reference answers. Every proposal is charged and retained;
the last bounded trial is used, never the trial with the best hidden score.
"""
from __future__ import annotations

from collections import Counter, deque
import copy
import json

from ..input_budget import method_messages, small

METHODS=('direct','cot','react','reflexion','ps')
PROTOCOL='eventpilot.paper_methods.12'
READ_COMMANDS={'read_page','inspect','read_input'}

REASON='''
CoT adaptation: this is the intermediate factual-summary pass. Use ONLY the
permitted public input in this request. Return {"reasoning":["short factual point"]}
with at most six short points. Distinguish current work, message source, timing,
conditions and available evidence. Do not invent absent evidence or future steps.
This pass does NOT execute tools or commit the final benchmark answer.
'''
PLAN='''
Plan-and-Solve (PS, not PS+) adaptation: first understand the request and devise
a short ordered plan, then work through that plan to propose the answer/action.
Use only the permitted public inputs. Return
{"plan":["subtask"],"solution":{...original JSON report or command...}}.
Use at most six short plan items. Preserve the original protocol's distinction
between adding a request for later and interrupting current work. Never claim
execution or completion merely because it appears in the plan.
This pass does NOT execute the proposed command or commit the benchmark report.
'''
FINAL='''
This is the final extraction pass. Use the fallible intermediate draft in
method_context together with the SAME permitted public input. Return ONLY the
original benchmark JSON report/command required above, without a wrapper.
Do not add hidden facts, new observations or unexecuted-tool results.
'''
REACT='''
ReAct adaptation: use the current public observation and your own prior steps to
select exactly one next action. Use the permitted read tools to reduce uncertainty
when necessary. An observation is available only after a tool actually returns.
Return {"thought":["brief factual reason for the next choice"],"action":{...}}.
The action value must be EXACTLY the original benchmark JSON report/command
required above. This outer wrapper is removed before execution. Use at most four
short thought items. Do not invent a successful tool result or backend outcome.
Prior method notes are fallible reasoning, not verified facts or new user orders.
'''
EVALUATE='''
Reflexion self-evaluator: assess the proposed action/report and its consistency
with ONLY this request's permitted public input, prior own actions and actual
public tool feedback. No hidden Gold, reference target, scoring result or future
message is available. Do not try to infer the evaluator's hidden answer.
Return {"pass":true,"feedback":["short evidence-based observation"]} or pass=false.
Check source authority, task timing, current progress, cancellations, dependencies,
and whether declarations agree with the proposed next action. Distinguish unknown
from observed absence. A reference successor is not evidence of correct clicking.
This pass does NOT execute tools and does not grade website or benchmark success.
'''
REFLECT='''
Reflexion verbal-memory pass: use the self-evaluation feedback and your proposal
to write at most three specific lessons for the NEXT LOCAL trial. Return
{"reflection":["short actionable lesson"]}. These are hypotheses, not verified
facts. Do not invent new observations, future events, task success or hidden
reference actions. The public environment has NOT advanced during these trials.
'''


class MethodBudgetExhausted(RuntimeError):
    """Known method termination, not infrastructure uncertainty."""


def options(method,max_requests=1024,reflexion_trials=2):
    if method not in METHODS:raise ValueError('Unknown method')
    common={'protocol':PROTOCOL,'max_model_requests_per_episode':max_requests,
            'memory_scope':'one case, isolated prefix/decision/actor channels',
            'hidden_feedback':False,'lgr_controller':False}
    if method=='direct':return {**common,'variant':'unmodified_base_requests'}
    if method=='cot':return {**common,'variant':'short_factual_summary_then_final','passes':2}
    if method=='ps':return {**common,'variant':'PS_plan_and_solve_then_extraction','passes':2}
    if method=='react':return {**common,'variant':'one_action_public_observation_loop','history_entries':4}
    return {**common,'variant':'local_selection_trials_with_public_self_evaluation',
            'actor':'react','max_local_trials':reflexion_trials,'memory_entries':3,
            'trial_reset':'no environment reset or reference-answer replay',
            'selection':'last trial or first self-evaluator pass; never best-of-Gold'}


def brief(value,count=6,length=300):
    if not isinstance(value,list):return []
    return [x[:length] for x in value[:count] if isinstance(x,str)]


def proposal(parsed):
    if not isinstance(parsed,dict):return None,[]
    if 'action' in parsed:
        return (copy.deepcopy(parsed['action']) if isinstance(parsed['action'],dict) else None,
                brief(parsed.get('thought'),4,300))
    # A model can omit the outer wrapper but still return the original protocol.
    # Apply this public, fixed compatibility rule without consulting correctness.
    if any(k in parsed for k in ('cmd','node_id','decisions')):return copy.deepcopy(parsed),[]
    return None,brief(parsed.get('thought'),4,300)


class PaperSession:
    def __init__(self,session,method,max_requests=1024,reflexion_trials=2):
        if method not in METHODS or method=='direct':raise ValueError('Direct must bypass the adapter')
        self.session=session;self.method=method;self.max_requests=max_requests
        self.reflexion_trials=reflexion_trials;self.requests=0
        self.episode=session.episode
        self.episode.update(method=method,method_protocol=PROTOCOL,method_trace=[])
        self.history={r:deque(maxlen=4) for r in ('prefix','decision','actor')}
        self.memory={r:deque(maxlen=3) for r in ('prefix','decision','actor')}
        self.base=None;self.pending=None;self.phase=None;self.trial=1
        self.draft=None;self.candidate=None;self.thought=[];self.feedback=None

    def _trace(self,**row):self.episode['method_trace'].append(row)

    def _context(self,role):
        context={'method':self.method,'stage':self.phase,
                 'own_recent_steps':list(self.history[role]),
                 'own_reflections':list(self.memory[role])}
        if self.phase=='final':context['draft']=copy.deepcopy(self.draft)
        if self.phase in ('evaluate','reflect'):
            context.update(candidate=copy.deepcopy(self.candidate),thought=list(self.thought),
                           local_trial=self.trial)
        if self.phase=='reflect':context['self_evaluation']=copy.deepcopy(self.feedback)
        return small(context,strings=360,items=8,depth=7)

    def next_request(self):
        if self.pending is not None:return self.pending
        if self.base is None:
            self.base=self.session.next_request()
            if self.base is None:return None
            self.phase='reason' if self.method=='cot' else 'plan' if self.method=='ps' else 'propose'
            self.trial=1;self.draft=None;self.candidate=None;self.feedback=None;self.thought=[]
        if self.requests>=self.max_requests:raise MethodBudgetExhausted('max_model_requests_per_episode')
        role=self.base['role']
        registry=self.session.decision_resources if role=='decision' else self.session.resources
        actor=role!='decision' or self.session.view in ('event-state','state','trajectory')
        instructions={'reason':REASON,'plan':PLAN,'final':FINAL,'propose':REACT,
                      'evaluate':EVALUATE,'reflect':REFLECT}[self.phase]
        try:
            messages=method_messages(self.base['messages'],instructions,self._context(role),registry,actor)
        except ValueError as error:
            if 'exceeds 64000-byte budget' not in str(error):raise
            raise MethodBudgetExhausted('auxiliary_input_byte_budget') from error
        self.requests+=1
        self.pending={**self.base,'messages':messages,'method_stage':self.method+'_'+self.phase,
                      'method_trial':self.trial,'base_role':role}
        return self.pending

    def _commit(self,value,role):
        # Only this object reaches the unchanged evaluator/environment.
        self.history[role].append({'thought':brief(self.thought,3,180),
                                   'action':small(value,strings=240,items=8,depth=5)})
        self._trace(stage='commit',role=role,local_trial=self.trial,output=copy.deepcopy(value))
        self.session.accept(value,role)
        self.base=None;self.pending=None;self.phase=None

    def accept(self,parsed,role):
        if self.pending is None or role!=self.base['role']:raise ValueError('No matching method request')
        phase=self.phase;self.pending=None
        self._trace(stage=phase,role=role,local_trial=self.trial,output=copy.deepcopy(parsed))
        if phase=='reason':
            self.draft={'reasoning':brief((parsed or {}).get('reasoning') if isinstance(parsed,dict) else None)}
            self.phase='final';return
        if phase=='plan':
            raw=parsed if isinstance(parsed,dict) else {}
            solution=raw.get('solution')
            if not isinstance(solution,dict) or len(json.dumps(solution,ensure_ascii=False).encode())>4096:solution=None
            self.draft={'plan':brief(raw.get('plan')),'solution':copy.deepcopy(solution)}
            self.phase='final';return
        if phase=='final':self._commit(parsed,role);return
        if phase=='propose':
            self.candidate,self.thought=proposal(parsed)
            is_read=(role in ('decision','actor') and isinstance(self.candidate,dict)
                     and self.candidate.get('cmd') in READ_COMMANDS)
            if self.method=='react' or is_read:self._commit(self.candidate,role);return
            self.phase='evaluate';return
        if phase=='evaluate':
            raw=parsed if isinstance(parsed,dict) else {}
            passed=raw.get('pass') is True and isinstance(self.candidate,dict)
            self.feedback={'pass':passed,'feedback':brief(raw.get('feedback'),4,240)}
            if passed or self.trial>=self.reflexion_trials:
                self._trace(stage='local_trial_selected',role=role,local_trial=self.trial,
                            self_evaluator_pass=passed,selection='first_pass_or_last_trial')
                self._commit(self.candidate,role)
            else:self.phase='reflect'
            return
        if phase=='reflect':
            lessons=brief(parsed.get('reflection') if isinstance(parsed,dict) else None,3,240)
            self.memory[role].append({'lessons':lessons})
            self.trial+=1;self.candidate=None;self.thought=[];self.feedback=None;self.phase='propose'

    def snapshot(self):
        result=self.session.snapshot()
        result['method_state']={'phase':self.phase,'local_trial':self.trial,'model_requests':self.requests,
                                'role_isolated_memory':{r:list(v) for r,v in self.memory.items()}}
        return result

    def close(self,status=None,diagnostics=None):
        result=self.session.close(status,diagnostics)
        result['method']=self.method;result['method_protocol']=PROTOCOL
        stages=Counter(r['stage'] for r in self.episode['method_trace'])
        result['method_summary']={'model_requests':self.requests,'stage_counts':dict(stages),
                                  'max_model_requests':self.max_requests,
                                  'reflexion_max_local_trials':self.reflexion_trials if self.method=='reflexion' else None,
                                  'no_hidden_feedback':True,'role_isolated_memory':True,
                                  'no_case_memory_sharing':True,'no_reference_answer_replay':True}
        return result
