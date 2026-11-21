"""Offline protocol tests, not measured language-model performance."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from eventpilot.methods.paper import PaperSession, MethodBudgetExhausted
from eventpilot.input_budget import bounded_messages,method_messages,MAX_INPUT_BYTES,resource_id,read_resource


class PublicSession:
    def __init__(self,roles=('decision',),view='event-only'):
        self.roles=list(roles);self.view=view;self.resources={};self.decision_resources={}
        self.episode={};self.accepted=[];self.n=0
    def next_request(self):
        if self.n==len(self.roles):return None
        role=self.roles[self.n]
        payload={'event':'Public user request','schema':'Return original report or command'}
        if role=='actor':payload['observed_goal']='ACTOR_ONLY_SECRET'
        return {'role':role,'messages':bounded_messages('Original protocol.',payload,
                               self.decision_resources if role=='decision' else self.resources,actor=role=='actor')}
    def accept(self,value,role):self.accepted.append((role,value));self.n+=1
    def snapshot(self):return copy.deepcopy(self.episode)
    def close(self,status=None,diagnostics=None):
        return {**self.episode,'status':status or 'agent_finished'}


def answer(env,value):
    req=env.next_request();env.accept(value,req['role']);return req


class MethodTests(unittest.TestCase):
    def test_cot_commits_only_final_and_repeated_request_is_stable(self):
        base=PublicSession();env=PaperSession(base,'cot')
        a=env.next_request();self.assertIs(a,env.next_request());self.assertEqual(env.requests,1)
        answer(env,{'reasoning':['Observed evidence only.']})
        self.assertEqual(base.accepted,[])
        final=env.next_request()
        self.assertEqual(final['method_stage'],'cot_final')
        self.assertIn('Observed evidence only.',final['messages'][-1]['content'])
        answer(env,{'decisions':{'E1':'DEFER'}})
        self.assertEqual(base.accepted,[('decision',{'decisions':{'E1':'DEFER'}})])
        self.assertIsNone(env.next_request())

    def test_plan_solution_never_executes_before_final(self):
        base=PublicSession(('actor',));env=PaperSession(base,'ps')
        answer(env,{'plan':['Read public page'],'solution':{'cmd':'cancel','tasks':['A']}})
        self.assertEqual(base.accepted,[])
        answer(env,{'cmd':'inspect','task':'A','query':'public evidence'})
        self.assertEqual(base.accepted[0][1]['cmd'],'inspect')

    def test_react_observations_only_after_real_commit(self):
        base=PublicSession(('actor','actor'));env=PaperSession(base,'react')
        answer(env,{'thought':['Need public evidence.'],'action':{'cmd':'inspect','task':'A','query':'x'}})
        self.assertEqual(base.accepted[0][1]['cmd'],'inspect')
        request=env.next_request()
        notes=json.loads(request['messages'][-1]['content'])['method_context']['own_recent_steps']
        self.assertEqual(notes[0]['action']['cmd'],'inspect')
        self.assertNotIn('success',notes[0])

    def test_reflexion_reject_reflect_retry_uses_last_not_hidden_best(self):
        base=PublicSession();env=PaperSession(base,'reflexion',reflexion_trials=2)
        answer(env,{'thought':['First proposal.'],'action':{'decisions':{'E1':'DEFER'}}})
        self.assertEqual(base.accepted,[])
        answer(env,{'pass':False,'feedback':['Check timing evidence.']})
        answer(env,{'reflection':['Read timing more carefully.']})
        request=env.next_request();self.assertEqual(request['method_trial'],2)
        self.assertIn('Read timing more carefully.',request['messages'][-1]['content'])
        answer(env,{'action':{'decisions':{'E1':'IGNORE'}}})
        answer(env,{'pass':False,'feedback':['Still uncertain.']})
        self.assertEqual(base.accepted,[('decision',{'decisions':{'E1':'IGNORE'}})])
        self.assertTrue(env.close()['method_summary']['no_hidden_feedback'])

    def test_reflexion_pass_commits_first_without_claiming_correctness(self):
        base=PublicSession();env=PaperSession(base,'reflexion')
        answer(env,{'action':{'decisions':{'E1':'WRONG'}}})
        answer(env,{'pass':True,'feedback':[]})
        self.assertEqual(base.accepted[0][1]['decisions']['E1'],'WRONG')

    def test_reflexion_read_goes_to_base_not_imaginary_evaluator(self):
        base=PublicSession();env=PaperSession(base,'reflexion')
        answer(env,{'action':{'cmd':'read_input','resource_id':'nonexistent'}})
        self.assertEqual(base.accepted[0][1]['cmd'],'read_input')
        self.assertEqual(env.requests,1)

    def test_role_and_case_memory_cannot_leak_actor_goal(self):
        base=PublicSession(('actor','decision'));env=PaperSession(base,'reflexion')
        answer(env,{'action':{'cmd':'finish'}})
        answer(env,{'pass':False,'feedback':['ACTOR_ONLY_SECRET']})
        answer(env,{'reflection':['ACTOR_ONLY_SECRET']})
        answer(env,{'action':{'cmd':'finish'}});answer(env,{'pass':True})
        self.assertNotIn('ACTOR_ONLY_SECRET',json.dumps(env.next_request()))
        other=PaperSession(PublicSession(),'reflexion')
        self.assertEqual(list(other.memory['actor']),[])

    def test_malformed_intermediates_and_budget_are_known(self):
        for method in ('cot','ps'):
            base=PublicSession();env=PaperSession(base,method,max_requests=1)
            answer(env,None)
            with self.assertRaises(MethodBudgetExhausted):env.next_request()
            self.assertEqual(base.accepted,[])
        base=PublicSession();env=PaperSession(base,'reflexion',reflexion_trials=1)
        answer(env,None);answer(env,None)
        self.assertEqual(base.accepted,[('decision',None)])

    def test_method_projection_preserves_scoped_public_originals(self):
        registry={};original={'text':'x'*6000,'form_controls':[]}
        history={'steps':[{'command':{'cmd':'inspect'},'tool_result':original} for _ in range(150)],
                 'observations_by_ref':{'one':original}}
        messages=bounded_messages('Public instructions.',{'current_state':original,'full_observed_trajectory':history},registry,actor=True)
        ref=resource_id(history,registry)
        revised=method_messages(messages,'Method only.',{'draft':'x'*500},registry,actor=True)
        self.assertLessEqual(sum(len(x['content'].encode()) for x in revised),MAX_INPUT_BYTES)
        self.assertEqual(registry[ref],history)
        self.assertTrue(read_resource(registry,{'resource_id':ref,'path':['steps',149]})['ok'])
        restricted={}
        self.assertFalse(read_resource(restricted,{'resource_id':ref})['ok'])


class RealEngineAdapterTests(unittest.TestCase):
    def test_all_methods_preserve_scoring_and_real_control_protocol(self):
        from eventpilot.engine import Session
        from eventpilot.corpus import Corpus
        from test_reviewed_engine import oracle_response
        cases=[json.loads(line) for line in (ROOT/'data/cases.jsonl').read_text().splitlines()]
        selected=[next(c for c in cases if c['experiment']=='main' and c['private']['gold']['branch']==branch)
                  for branch in ('IGNORE','DEFER','HANDLE','REPLAN','TERMINATE')]
        selected.extend(next(c for c in cases if c['experiment']=='multi' and c['subtype'].startswith(kind+'/'))
                        for kind in ('ORD','COND','RES','CAN'))
        selected.extend(next(c for c in cases if c.get('cf_type')==kind) for kind in ('goal','state','semantic'))
        corpus=Corpus(ROOT/'corpus')
        for case,method in ((c,m) for c in selected for m in ('cot','ps','react','reflexion')):
            base=Session(case,corpus,view='state',max_commands=160)
            env=PaperSession(base,method,max_requests=1024)
            for _ in range(1024):
                req=env.next_request()
                if req is None:break
                phase=req['method_stage'].split('_',1)[1]
                if phase=='reason':value={'reasoning':['Engineering fixture, not a model result.']}
                elif phase=='plan':value={'plan':['Engineering fixture only.']}
                elif phase=='evaluate':value={'pass':True}
                else:
                    value=oracle_response(base,req['role'])
                    if phase=='propose':value={'action':value}
                env.accept(value,req['role'])
            else:self.fail('Adapter did not terminate')
            episode=env.close()
            self.assertTrue(episode['evaluation']['episode_success'],(case['case_id'],method))
            self.assertEqual(episode['view'],'state')


if __name__=='__main__':unittest.main()
