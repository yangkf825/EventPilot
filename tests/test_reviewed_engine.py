"""Private oracle engineering fixtures; these are NOT language-model results."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from contextlib import ExitStack

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from eventpilot.engine import Session, schedule_valid, registered_native_scopes
from eventpilot.corpus import Corpus

ROOT=Path(__file__).resolve().parents[1]


def legal_schedule(state):
    todo=set(state['required_tasks'])-set(state.get('completed_tasks',[]))-set(state.get('cancelled_tasks',[]))
    out=[]
    while todo:
        roots=sorted(r for r in todo if not any(y==r and x in todo for x,y in state.get('precedence_constraints',[])))
        if not roots:raise AssertionError('Registered dependency cycle')
        out.extend(roots);todo-=set(roots)
    return out


def oracle_response(env,role):
    if role=='prefix':
        task,index='A',env.prefix_index
    elif role=='decision':
        for index in env.group_indices:
            stage=env.stages[index]
            cond=stage.get('condition') or {}
            if cond.get('resolved')=='Unknown' and not env._verified.get(stage['event_id']):
                return {'cmd':'inspect','task':cond.get('predicate_workspace',stage['pre_state']['active_workspace']),
                        'query':cond['query'],'offset':0}
        decisions,followups={},{}
        for index in env.group_indices:
            stage=env.stages[index]
            branch=stage['allowed_branches'][0]
            if stage.get('post_verification_gold'):
                g=stage['post_verification_gold']
                branch=g.get('follow_up') or g['decision']
            decisions[stage['event_id']]=branch if branch in ('IGNORE','DEFER') else 'INTERRUPT'
            if branch not in ('IGNORE','DEFER'):followups[stage['event_id']]=branch
        expected=env._expected_state(env.stages[env.group_indices[-1]])
        schedule=legal_schedule(expected)
        return {'decisions':decisions,'follow_ups':followups,'schedule':schedule,
                'required_tasks':expected['required_tasks'],'cancelled_tasks':expected.get('cancelled_tasks',[]),
                'precedence':expected.get('precedence_constraints',[]),'next_task':schedule[0] if schedule else None}
    else:
        window=env.window
        state=window['expected']
        pending=set(state['required_tasks'])-set(state.get('completed_tasks',[]))
        if 'A' in state.get('cancelled_tasks',[]) and pending and env.model_adopted not in pending:
            target=next(iter(sorted(pending)))
            if any('REPLAN' in env.stages[i].get('allowed_branches',[]) for i in env.group_indices):
                return {'cmd':'adopt_goal','task':target}
        cancel=set(state.get('cancelled_tasks',[]))-env.model_cancelled
        if cancel:return {'cmd':'cancel','tasks':sorted(cancel)}
        restore=env.model_cancelled&pending
        if restore:return {'cmd':'restore','task':sorted(restore)[0]}
        remaining=window['required_pairs']-set(window['actions'])
        if not remaining:return {'cmd':'finish'}
        choices=env._ready(state)
        task=env.active if env.active in choices and (env.active,env.pointers[env.active]) in remaining else next(
            r for r in sorted(choices) if (r,env.pointers[r]) in remaining)
        if env.active!=task:return {'cmd':'switch','task':task}
        index=env.pointers[task]
    tid=env.case['tasks'][task]['source_task_id']
    step=env.corpus.tasks[tid]['steps'][index]
    page_ids={n['node_id'] for n in env.corpus.page(tid,index)['nodes']}
    node=next(x for x in step['positive_node_ids'] if x in page_ids)
    return {'cmd':'act','node_id':node,'op':step['operation']['op'],'value':step['operation'].get('value','')}


def run_oracle(case,corpus,delivery='sequential',render=False):
    env=Session(case,corpus,model='PRIVATE_ORACLE_NOT_LLM',delivery=delivery,max_commands=160)
    def stub(*a,**k):return []
    with ExitStack() as stack:
        # Non-rendered sweep still exercises Corpus.score_action and public reads;
        # ordinary prompt construction is tested separately on actual samples.
        if not render:
            stack.enter_context(patch('eventpilot.engine.actor_messages',side_effect=stub))
            stack.enter_context(patch('eventpilot.engine.prefix_messages',side_effect=lambda *a,**k:([],{'nodes':[]})))
            stack.enter_context(patch('eventpilot.engine.decision_messages',side_effect=stub))
        for _ in range(400):
            req=env.next_request()
            if req is None:break
            env.accept(oracle_response(env,req['role']),req['role'])
        else:raise AssertionError('Oracle fixture did not terminate')
    return env.close()


class ReviewedEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=[json.loads(x) for x in (ROOT/'data/cases.jsonl').read_text().splitlines()]
        cls.byid={x['case_id']:x for x in cls.cases}
        cls.corpus=Corpus(ROOT/'corpus')

    def test_terminate_cancels_all_work_without_b_execution(self):
        case=next(c for c in self.cases if c['experiment']=='main' and c['private']['gold']['branch']=='TERMINATE')
        e=run_oracle(case,self.corpus)
        self.assertTrue(e['evaluation']['episode_success'])
        self.assertFalse(e['evaluation']['ptar'])
        self.assertFalse(any(a['workspace']=='B' for a in e['native_actions']))
        self.assertEqual(set(e['decision_stages'][0]['expected_state']['cancelled_tasks']),{'A','B'})

    def test_handle_switches_b_and_resumes_a_without_automatic_handoff(self):
        case=next(c for c in self.cases if c['experiment']=='main' and c['private']['gold']['branch']=='HANDLE')
        e=run_oracle(case,self.corpus)
        self.assertTrue(e['evaluation']['episode_success'])
        switches=[x['command']['task'] for x in e['control_actions'] if x['command'].get('cmd')=='switch']
        self.assertEqual(switches[:2],['B','A'])

    def test_correct_decision_without_execution_is_failure(self):
        case=next(c for c in self.cases if c['experiment']=='main' and c['private']['gold']['branch']=='HANDLE')
        env=Session(case,self.corpus)
        while True:
            req=env.next_request()
            reply={'cmd':'finish'} if req['role']=='actor' else oracle_response(env,req['role'])
            env.accept(reply,req['role'])
            if req['role']=='actor':break
        e=env.close()
        self.assertTrue(e['evaluation']['complete_case_decision_correct'])
        self.assertFalse(e['evaluation']['episode_success'])
        b=next(s for s in e['native_scopes'] if s['scope']=='B')
        self.assertTrue(all(not x['attempted'] and x['step_correct'] is False for x in b['steps']))

    def test_multiple_legal_schedules_accepted(self):
        state={'required_tasks':['A','B','C'],'completed_tasks':[],'cancelled_tasks':[],
               'precedence_constraints':[['A','B'],['C','B']]}
        self.assertTrue(schedule_valid(['A','C','B'],state))
        self.assertTrue(schedule_valid(['C','A','B'],state))
        self.assertFalse(schedule_valid(['B','A','C'],state))

    def test_unknown_condition_checked_before_branch(self):
        c=self.byid['L3_RES_022']
        for delivery in ('sequential','batch','sequential_no_action'):
            e=run_oracle(c,self.corpus,delivery)
            self.assertTrue(e['evaluation']['episode_success'],(delivery,e['evaluation']))
            s=next(s for s in e['decision_stages'] if s['event_id']=='E2')
            self.assertTrue(s['verification_passed'])
            self.assertEqual(s['allowed_branches'],['DEFER'])

    def test_pending_native_denominators_do_not_depend_on_progress(self):
        c=self.byid['L3_CAN_001']
        env=Session(c,self.corpus)
        e=env.close('provider_error')
        scopes=registered_native_scopes(c)
        self.assertEqual([(x['scope'],x['registered_steps'],x['required']) for x in e['native_scopes']],
                         [(x['scope'],x['registered_steps'],x['required']) for x in scopes])
        self.assertIsNone(e['evaluation']['episode_success'])
        self.assertTrue(all(x['step_correct'] is None for s in e['native_scopes'] for x in s['steps']))

    def _decision_environment(self,case,view):
        env=Session(case,self.corpus,view=view)
        while True:
            req=env.next_request()
            if req['role']=='decision':return env,req
            env.accept(oracle_response(env,req['role']),req['role'])

    def test_ablation_information_is_nested_and_no_future_messages(self):
        case=self.byid['L3_ORD_016']
        payloads={}
        for view in ('event-only','goal','state','trajectory'):
            env,req=self._decision_environment(case,view)
            payloads[view]=json.loads(req['messages'][1]['content'])
            self.assertEqual(payloads[view]['new_message_ids'],['E1'])
            self.assertEqual([e['id'] for e in payloads[view]['delivered_events']],['E1'])
            self.assertNotIn('C',payloads[view].get('delivered_task_goals',{}))
            self.assertNotIn('D',payloads[view].get('delivered_task_goals',{}))
        self.assertNotIn('initial_user_request',payloads['event-only'])
        self.assertNotIn('current_state',payloads['goal'])
        self.assertIn('current_state',payloads['state'])
        self.assertNotIn('full_observed_trajectory',payloads['state'])
        self.assertIn('full_observed_trajectory',payloads['trajectory'])

    def test_goal_cf_hides_different_raw_dom_at_shared_s0(self):
        a,b=[c for c in self.cases if c.get('group_id')=='L2G01']
        _,ra=self._decision_environment(a,'event-only')
        _,rb=self._decision_environment(b,'event-only')
        self.assertEqual(ra['messages'],rb['messages'])
        _,ra=self._decision_environment(a,'trajectory')
        _,rb=self._decision_environment(b,'trajectory')
        pa,pb=[json.loads(r['messages'][1]['content']) for r in (ra,rb)]
        self.assertNotEqual(pa['initial_user_request'],pb['initial_user_request'])
        self.assertEqual(pa['current_state'],pb['current_state'])
        self.assertEqual(pa['full_observed_trajectory'],pb['full_observed_trajectory'])
        self.assertNotIn('snapshot_sha256',str(pa['current_state']))

    def test_event_state_hides_explicit_goals_but_retains_observed_state(self):
        c=self.byid['L3_ORD_016']
        env,req=self._decision_environment(c,'event-state')
        payload=json.loads(req['messages'][1]['content'])
        self.assertIn('current_state',payload)
        self.assertIn('workspace_states',payload)
        self.assertNotIn('initial_user_request',payload)
        self.assertNotIn('delivered_task_goals',payload)
        self.assertNotIn('full_observed_trajectory',payload)
        self.assertTrue(all('goal' not in state for state in payload['workspace_states'].values()))
        self.assertEqual(payload['new_message_ids'],['E1'])
        self.assertEqual([e['id'] for e in payload['delivered_events']],['E1'])
        self.assertNotIn('C',payload['workspace_states'])
        self.assertNotIn('D',payload['workspace_states'])
        # Later Actor information and tools remain identical across ablations.
        from eventpilot.prompts import actor_messages
        self.assertIn('initial_user_request',json.loads(actor_messages(env)[1]['content']))

    def test_event_state_goal_cf_stays_identical_but_state_cf_changes(self):
        goal=[c for c in self.cases if c.get('group_id')=='L2G01']
        requests=[self._decision_environment(c,'event-state')[1]['messages'] for c in goal]
        self.assertEqual(requests[0],requests[1])
        state=[c for c in self.cases if c.get('group_id')=='L2S01']
        requests=[self._decision_environment(c,'event-state')[1]['messages'] for c in state]
        self.assertNotEqual(requests[0],requests[1])

    def test_state_cf_only_changes_state_after_goal_view(self):
        a,b=[c for c in self.cases if c.get('group_id')=='L2S01']
        _,ra=self._decision_environment(a,'goal')
        _,rb=self._decision_environment(b,'goal')
        self.assertEqual(ra['messages'],rb['messages'])
        _,ra=self._decision_environment(a,'state')
        _,rb=self._decision_environment(b,'state')
        self.assertNotEqual(ra['messages'],rb['messages'])

    def test_visible_task_d_requires_task_reference_not_letter(self):
        c=copy.deepcopy(self.byid['L3_ORD_016'])
        c['events'][0]['text']+=' The license class D is mentioned in this page.'
        env,_=self._decision_environment(c,'goal')
        self.assertNotIn('D',env.visible_roles)

    def test_prompt_projection_recovers_public_large_inputs(self):
        env,_=self._decision_environment(self.byid['R001'],'trajectory')
        from eventpilot.prompts import actor_messages
        env.last_report={'decisions':{'E1':'IGNORE'},'schedule':['A']}
        env.public_history.extend([{'origin':'model_control','action':{'cmd':'inspect','query':'x'*3000}} for _ in range(90)])
        messages=actor_messages(env)
        self.assertLessEqual(sum(len(m['content'].encode('utf-8')) for m in messages),64000)
        self.assertTrue(env.resources)
        self.assertNotIn('allowed_branches',messages[1]['content'])

    def test_state_decision_cannot_read_actor_or_prefix_resources(self):
        env,_=self._decision_environment(self.byid['R001'],'state')
        env.resources['pub_secret_old_history']={'text':'Earlier actor-only history'}
        reply=env._read({'cmd':'read_input','resource_id':'pub_secret_old_history'},decision=True)
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['reason'],'unknown_public_resource')

    def test_read_input_current_page_evidence_verifies_condition(self):
        from eventpilot.input_budget import resource_id
        env=Session(self.byid['L3_RES_022'],self.corpus)
        while True:
            req=env.next_request()
            if req['role']=='decision' and env.pending_ids==['E2']:break
            env.accept(oracle_response(env,req['role']),req['role'])
        ref=resource_id(env.observation('B'),env.decision_resources)
        offset=0
        for _ in range(30):
            result=env._read({'cmd':'read_input','resource_id':ref,'path':['text'],'offset':offset},decision=True)
            if env._verified.get('E2') or not result['more']:break
            offset=result['next_offset']
        self.assertTrue(env._verified.get('E2'))

    def test_zero_action_cancellation_does_not_require_unoffered_tool(self):
        for cid,delivery in [('L3_CAN_001','sequential'),('L3_CAN_016','sequential_no_action'),
                             ('L3_COND_006','sequential_no_action')]:
            e=run_oracle(self.byid[cid],self.corpus,delivery)
            self.assertTrue(e['evaluation']['episode_success'],(cid,delivery))

    def test_packed_source_preserves_bytes_and_checks_snapshot_hash(self):
        c=self.byid['R001'];tid=c['tasks']['A']['source_task_id']
        source=self.corpus.tasks[tid];name=source['steps'][0]['cleaned_html']
        raw=self.corpus.snapshot_archive.read(name) if self.corpus.snapshot_archive else self.corpus.path(name).read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data/normalized').mkdir(parents=True)
            (root/'data/normalized/tasks.jsonl').write_text(json.dumps(source)+'\n')
            with zipfile.ZipFile(root/'snapshots.zip','w') as z:z.writestr(name,raw)
            packed=Corpus(root)
            actual=packed.page(tid,0)['snapshot_sha256']
            self.assertEqual(actual,self.corpus.page(tid,0)['snapshot_sha256'])
            bad=Corpus(root);bad.expected_snapshot_hashes={(tid,0):'0'*64}
            with self.assertRaises(OSError):bad.page(tid,0)


if __name__=='__main__':unittest.main()
