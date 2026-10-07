"""Full behavior, invariant controls and transport regressions; no paid calls."""
import asyncio
import copy
import json

import pytest

from eventarena.online_v2_engine import (AutonomousEpisode, prepare_checkpoint, run_checkpoint_episode,
    match_trigger, lossless_trajectory, decode_lossless_trajectory)
from eventarena.online_v2_metrics import score_cases
from eventarena.online_eval import judge_messages, parse_judge
from eventarena.providers import ProviderText
from test_online_v2_engine import FakeBrowser, Model, case, finish, b_actions


def prepared(c, folder):
    return asyncio.run(prepare_checkpoint(c,Model([{'op':'TYPE','node_id':'0','value':'alpha'}]),folder,
                                         {'headless':True},FakeBrowser))['checkpoint']


@pytest.mark.parametrize('view',['event-only','goal','state','trajectory'])
@pytest.mark.parametrize('branch',['IGNORE','DEFER','HANDLE','REPLAN','TERMINATE'])
def test_all_information_views_continue_real_behavior_and_verify_outcomes(tmp_path,view,branch):
    c=case(branch)
    cp=prepared(c,tmp_path/'prep')
    gold=c['gold']
    responses=[{'decision':gold['decision'],'follow_up':gold.get('follow_up')}]
    if branch=='IGNORE': responses += finish()
    elif branch=='DEFER': responses += finish()+b_actions()
    elif branch=='HANDLE': responses += b_actions()+[{'op':'SWITCH_TASK','task_id':'A'},{'op':'CLICK','node_id':'1'}]+finish()
    elif branch=='REPLAN': responses += [{'op':'ACCEPT_EVENT','event_id':'E1'},{'op':'TYPE','node_id':'0','value':'gamma'}]+finish('gamma')
    else: responses += [{'op':'ACCEPT_EVENT','event_id':'E1'}]
    responses += [{'op':'ANSWER','answer':'done'}]
    model=Model(responses)
    ep=asyncio.run(run_checkpoint_episode(c,model,cp,view,tmp_path/'run',{'headless':True},FakeBrowser))
    assert ep['protocol']=='controlled_episode_v2'
    assert ep['status']=='agent_finished'
    assert ep['checkpoint_replay_audit']['matches'] is True
    assert ep['evaluation']['episode_success'] is True
    assert any(r['origin']=='actor' for r in ep['actions'])
    decision=json.loads(model.messages[0][1]['content'])
    assert ('original_user_task' in decision)==(view!='event-only')
    assert ('current_state' in decision)==(view in {'state','trajectory'})
    assert ('full_trajectory' in decision)==(view=='trajectory')
    post=json.loads(model.messages[1][1]['content'])
    memory=post['event_boundary_information']
    assert ('full_trajectory' in memory)==(view=='trajectory')
    assert ('current_state' in memory)==(view in {'state','trajectory'})
    history=decode_lossless_trajectory(post['full_trajectory'])
    assert all(a['index']>=ep['controlled_action_boundary'] for a in history['actions'])
    scores=score_cases([c],[{**ep,'view':view}],experiment='ablation',view=view)
    assert scores['MicroESR']==1.0
    key={'IGNORE':'I_SR','DEFER':'D_SR','HANDLE':'H_SR','REPLAN':'R_SR','TERMINATE':'T_SR'}[branch]
    assert scores[key]==1.0


def test_declared_decision_never_dispatches_or_completes_event(tmp_path):
    c=case('HANDLE');cp=prepared(c,tmp_path/'prep')
    model=Model([{'decision':'INTERRUPT','follow_up':'HANDLE'},{'op':'ANSWER','answer':'done'}])
    ep=asyncio.run(run_checkpoint_episode(c,model,cp,'trajectory',tmp_path/'run',{},FakeBrowser))
    assert ep['decisions']['E1']=='INTERRUPT'
    assert ep['events']['E1']['started_index'] is None
    assert ep['evaluation']['episode_success'] is False


def test_replay_mismatch_prevents_test_model_call(tmp_path):
    c=case();cp=prepared(c,tmp_path/'prep');cp['state']['summary']='different actual page'
    model=Model([])
    ep=asyncio.run(run_checkpoint_episode(c,model,cp,'trajectory',tmp_path/'run',{},FakeBrowser))
    assert not model.messages
    assert ep['status']=='checkpoint_replay_mismatch'
    assert ep['evaluation']['episode_success'] is None


def test_matched_initial_history_baseline_does_not_deliver_event(tmp_path):
    c=case();cp=prepared(c,tmp_path/'prep');base=copy.deepcopy(c);base['events']=[];base.pop('event');base['gold']={}
    model=Model(finish()+[{'op':'ANSWER','answer':'done'}])
    ep=asyncio.run(run_checkpoint_episode(base,model,cp,'trajectory',tmp_path/'run',{},FakeBrowser,suppress_events=True))
    assert ep['events']=={} and not ep['exposed']
    assert ep['evaluation']['episode_success'] is True
    assert not any(r['stage']=='decision' for r in ep['responses'])


def test_legitimate_actor_goto_is_progress_registered_entry_is_not():
    trigger={'all':[{'kind':'url_contains','value':'/target'},{'kind':'url_path_not_in','values':['/']}], 'min_actor_progress_actions':1}
    obs={'url':'https://example.test/target','text':'actual target','http_status':200}
    goto={'task_id':'A','action':{'op':'GOTO','url':obs['url']},'error':None,'origin':'actor'}
    assert match_trigger(trigger,obs,[goto])
    assert not match_trigger(trigger,obs,[{**goto,'origin':'registered_task_entry'}])
    assert not match_trigger(trigger,obs,[{**goto,'error':'TimeoutError'}])
    assert not match_trigger(trigger,{**obs,'url':'https://example.test/'},[goto])
    assert not match_trigger(trigger,{**obs,'http_status':404},[goto])


def test_judge_copies_explicit_citation_index_not_source_index():
    phase={'observations':[{'index':8,'url':'https://example.test/current','text':'now'},
                           {'index':3,'url':'https://example.test/forecast','text':'verified seven days forecast'}]}
    payload=json.loads(judge_messages('Find forecast',phase)[1]['content'])
    assert payload['actual_observations'][1]['index']==3
    assert payload['actual_observations'][1]['citation_index']==1
    raw={'success':True,'reason':'supported','evidence':[{'observation_index':1,'url':'https://example.test/forecast','quote':'seven days forecast'}]}
    assert parse_judge(raw,phase)['success'] is True
    raw['evidence'][0]['observation_index']=3
    assert parse_judge(raw,phase)['success'] is None


def test_truncated_output_is_scored_budget_failure_without_format_retry(tmp_path):
    c=case();model=Model([ProviderText('',{'finish_reason':'length','empty_content':True,'requested_output_tokens':16384})])
    engine=AutonomousEpisode(c,model,tmp_path,{'format_retries':1})
    async def go():
        async with FakeBrowser(tmp_path) as browser:
            return await engine.actor_step(browser)
    asyncio.run(go())
    assert engine.episode['status']=='output_truncated'
    assert engine.episode['format_retry_count']==0
    assert len(model.messages)==1


def test_setup_focus_cannot_escape_to_original_task(tmp_path):
    c=case();c['setup_tasks']=[{'id':'PRIOR_B','goal':'Find beta','url':'https://example.test/b','verification':{'method':'deterministic','checks':[{'type':'saved_output_contains','value':'beta'}]}}]
    model=Model([{'op':'SWITCH_TASK','task_id':'A'},{'op':'ANSWER','answer':'done'}])
    engine=AutonomousEpisode(c,model,tmp_path,{'setup_steps':3})
    async def go():
        async with FakeBrowser(tmp_path) as browser:
            return await engine.warmup(browser)
    assert asyncio.run(go()) is False
    first=json.loads(model.messages[0][1]['content'])
    assert first['original_user_task']=='Find beta'
    assert set(first['available_tasks'])=={'PRIOR_B'}
    assert engine.episode['actions'][1]['error']=='ValueError'
    assert engine.episode['status']=='checkpoint_setup_failed'
    assert engine.episode['preparation_failure_is_model'] is True


def test_counterfactual_reports_common_execution_metrics_per_type():
    cases=[];episodes=[]
    for id_,branch in [('A','HANDLE'),('B','DEFER')]:
        c=case(branch);c.update(case_id=id_,group_type='goal',group_id='G1');cases.append(c)
        episodes.append({'case_id':id_,'repeat':0,'view':'state','protocol':'controlled_episode_v2',
                         'status':'agent_finished','counterfactual_protocol_valid':True,'api_ok':True,
                         'decisions':{'E1':c['gold']['decision']},'follow_ups':{'E1':c['gold'].get('follow_up')},
                         'events':{'E1':{'delivered':True}},
                         'evaluation':{'episode_success':True,'branch_success':True,'task_success':True,
                                       'environment_valid':True,'event_results':{'E1':{'success':True,'timely':True}}}})
    result=score_cases(cases,episodes,experiment='counterfactual',view='state')
    assert result['CFA']==1.0 and result['MicroESR']==1.0
    assert result['counterfactual']['types']['goal']['common_metrics']['MicroESR']==1.0
    assert result['counterfactual']['types']['goal']['common_metrics']['H_SR']==1.0
    episodes[1]['counterfactual_protocol_valid']=False
    result=score_cases(cases,episodes,experiment='counterfactual',view='state')
    assert result['CFA'] is None and result['MicroESR'] is None


def test_gateway_response_diagnostics_do_not_promote_reasoning_to_actions(tmp_path,monkeypatch):
    import eventarena.providers as providers
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'models':[{'name':'ds','model':'test','backend':'chat-completions','endpoint':'https://example.test/v1/chat/completions','api_key_env':'TEST_ONLY_FAKE_KEY','max_tokens':16384}]}))
    monkeypatch.setenv('TEST_ONLY_FAKE_KEY','nonsecret-test-fixture')
    class Response:
        status_code=200;is_error=False
        def json(self):
            return {'model':'test','choices':[{'finish_reason':'length','message':{'content':'','reasoning_content':'private thought text'}}],'usage':{'completion_tokens':16384}}
    class Client:
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):return Response()
    monkeypatch.setattr(providers.httpx,'Client',Client)
    model=providers.Model('ds',config)
    value=model.call([{'role':'user','content':'fixture'}])
    assert value=='' and value.diagnostics['reasoning_chars']==len('private thought text')
    assert value.diagnostics['requested_output_tokens']==16384
    assert 'private thought text' not in json.dumps(model.usage)


def test_full_runner_shares_main_and_trajectory_and_scores_all_views(tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    import eventarena.providers as providers
    import eventarena.online_v2_engine as engine
    spec=importlib.util.spec_from_file_location('v203_runner_fixture',Path(__file__).resolve().parents[1]/'scripts/run_online_v2.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    c=case('IGNORE');c['events']=[c.pop('event')];c['task_family_id']='F1'
    dataset=tmp_path/'data';dataset.mkdir();(dataset/'single_event.jsonl').write_text(json.dumps(c)+'\n')
    baseline=runner.baseline_case(c)
    selected={'main':[c],'ablation':[c],'baseline':[baseline]}
    jobs=runner.make_jobs(selected,['ds'],1,4)
    root=tmp_path/'run';root.mkdir();runner.write_json(root/'manifest.json',{'jobs':jobs})
    class Planner:
        def __init__(self,*args,**kwargs):self.usage=[]
        def call(self,messages):
            payload=json.loads(messages[1]['content'])
            if 'incoming_event' in payload:return json.dumps({'decision':'IGNORE','follow_up':None})
            observation=payload['observation']
            if 'state_ref' in observation:
                history=decode_lossless_trajectory(payload['full_trajectory'])
                observation=next(o for o in history['observations'] if o.get('index')==observation['observation_index'])
            ws=payload['workspace']
            if 'alpha' not in observation['text']:return json.dumps({'op':'TYPE','node_id':'0','value':'alpha'})
            if not ws['task_outputs'].get('A'):return json.dumps({'op':'SAVE_RESULT','text':'alpha','sources':[{'url':observation['url'],'quote':'alpha'}]})
            if not ws['tasks']['A']['completed']:return json.dumps({'op':'COMPLETE_TASK'})
            return json.dumps({'op':'ANSWER','answer':'done'})
    monkeypatch.setattr(providers,'Model',Planner)
    original_prepare=engine.prepare_checkpoint
    original_run=engine.run_checkpoint_episode
    calls={'prepare':0,'continuation':0}
    async def prepare(*args,**kwargs):
        calls['prepare']+=1
        return await original_prepare(*args,**kwargs,browser_factory=FakeBrowser)
    async def continuation(*args,**kwargs):
        calls['continuation']+=1
        return await original_run(*args,**kwargs,browser_factory=FakeBrowser)
    monkeypatch.setattr(engine,'prepare_checkpoint',prepare)
    monkeypatch.setattr(engine,'run_checkpoint_episode',continuation)
    args=SimpleNamespace(config=tmp_path/'unused',data_dir=dataset,prep_model='ds',judge_model=None,
                         resume=False,retry_infra=False,workers=2,repeats=1,headless=True,max_steps=15,
                         pre_steps=5,setup_steps=5,max_context_chars=450000,loop_limit=4,format_retries=1)
    asyncio.run(runner.execute(args,root,selected,['ds']))
    assert len(jobs)==6 and calls=={'prepare':1,'continuation':5}
    main=runner.read_json(root/'main/ds/metrics.json')['trajectory']
    ablation=runner.read_json(root/'ablation/ds/metrics.json')
    assert main['MicroESR']==1.0
    for view,metric in ablation.items(): assert metric['MicroESR']==1.0
    for metric in runner.CORE_COLUMNS['main']:
        assert main[metric]==ablation['trajectory'][metric]
    assert runner.status(root)['states']=={'finished':6}


def test_shared_episode_infrastructure_recovery_happens_once_per_invocation(tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    import eventarena.providers as providers
    import eventarena.online_v2_engine as engine
    spec=importlib.util.spec_from_file_location('v203_recovery_fixture',Path(__file__).resolve().parents[1]/'scripts/run_online_v2.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    c=case('IGNORE');c['events']=[c.pop('event')];c['task_family_id']='F1'
    dataset=tmp_path/'data';dataset.mkdir();(dataset/'single_event.jsonl').write_text(json.dumps(c)+'\n')
    selected={'main':[c],'ablation':[c]}
    jobs=[j for j in runner.make_jobs(selected,['ds'],1,4) if j['view']=='trajectory']
    root=tmp_path/'run';root.mkdir();runner.write_json(root/'manifest.json',{'jobs':jobs})
    failed={'case_id':c['case_id'],'status':'checkpoint_replay_mismatch','responses':[],
            'decisions':{},'follow_ups':{},'evaluation':{'episode_success':None,'environment_valid':False,
                                                       'failure_category':'environment_invalid'}}
    shared=root/'shared_episodes/ds/repeat_0'/c['case_id']/'episode.json'
    runner.write_json(shared,failed)
    class UnusedModel:
        def __init__(self,*args,**kwargs):self.usage=[]
        def call(self,messages):raise AssertionError('No provider call in this fixture')
    calls=[]
    async def prepare(*args,**kwargs):return {'status':'checkpoint_ready','checkpoint':{'case_id':c['case_id']}}
    async def continuation(*args,**kwargs):
        calls.append(args[0]['case_id'])
        return copy.deepcopy(failed)
    monkeypatch.setattr(providers,'Model',UnusedModel)
    monkeypatch.setattr(engine,'prepare_checkpoint',prepare)
    monkeypatch.setattr(engine,'run_checkpoint_episode',continuation)
    args=SimpleNamespace(config=tmp_path/'unused',data_dir=dataset,prep_model='ds',judge_model=None,
                         resume=True,retry_infra=True,workers=2,repeats=1,headless=True,max_steps=15,
                         pre_steps=5,setup_steps=5,max_context_chars=450000,loop_limit=4,format_retries=1)
    asyncio.run(runner.execute(args,root,selected,['ds']))
    assert calls==[c['case_id']]
    assert runner.status(root)['states']=={'finished':2}
    assert len(list(shared.parent.glob('episode_failed_*.json')))==1
    for j in jobs:
        assert runner.read_json(runner.job_path(root,j)/'episode.json')['status']=='checkpoint_replay_mismatch'


@pytest.mark.browser
def test_full_controlled_handle_with_real_chromium(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            value='beta' if self.path=='/b' else 'alpha'
            page=f'<html><title>Actual research page</title><body><p>Research results {value}</p><button>Details</button></body></html>'.encode()
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(page)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    start=f'http://127.0.0.1:{server.server_port}'
    c=case('HANDLE');c['url']=start+'/'
    c['trigger']={'all':[{'kind':'url_contains','value':'/a'},{'kind':'url_path_not_in','values':['/']}],'min_actor_progress_actions':1}
    c['events']=[c.pop('event')];c['events'][0]['execution']['url']=start+'/b'
    try:
        cp=asyncio.run(prepare_checkpoint(c,Model([{'op':'GOTO','url':start+'/a'}]),tmp_path/'liveprep',{'headless':True}))['checkpoint']
        actions=[{'decision':'INTERRUPT','follow_up':'HANDLE'},
                 {'op':'SWITCH_TASK','task_id':'E1'},{'op':'GOTO','url':start+'/b'},
                 {'op':'SAVE_RESULT','text':'beta','sources':[{'url':start+'/b','quote':'beta'}]},
                 {'op':'COMPLETE_TASK'},{'op':'SWITCH_TASK','task_id':'A'},
                 {'op':'SAVE_RESULT','text':'alpha','sources':[{'url':start+'/a','quote':'alpha'}]},
                 {'op':'COMPLETE_TASK'},{'op':'ANSWER','answer':'done'}]
        ep=asyncio.run(run_checkpoint_episode(c,Model(actions),cp,'state',tmp_path/'liveepisode',{'headless':True}))
        assert ep['evaluation']['episode_success'] is True
        assert ep['checkpoint_replay_audit']['matches'] is True
        assert (tmp_path/'liveepisode/trace.zip').exists()
        assert len(list((tmp_path/'liveepisode/trajectory').glob('*.html')))>=4
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


@pytest.mark.browser
def test_popup_checkpoint_replay_keeps_actual_active_page(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from eventarena.browser import Browser
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body='<p>Research results alpha</p><button>Details</button>' if self.path=='/a' else '<a target="_blank" href="/a">Open results</a>'
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(('<html><title>Popup research</title><body>'+body+'</body></html>').encode())
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    c=case();c['url']=url+'/';c['trigger']={'kind':'url_contains','value':'/a','min_actor_progress_actions':1};c['events']=[c.pop('event')]
    try:
        cp=asyncio.run(prepare_checkpoint(c,Model([{'op':'CLICK','node_id':'0'}]),tmp_path/'prep',{'headless':True}))['checkpoint']
        assert cp['state']['url']==url+'/a'
        model=Model([{'decision':'IGNORE'}, {'op':'SAVE_RESULT','text':'alpha','sources':[{'url':url+'/a','quote':'alpha'}]}, {'op':'COMPLETE_TASK'}, {'op':'ANSWER'}])
        ep=asyncio.run(run_checkpoint_episode(c,model,cp,'trajectory',tmp_path/'replay',{'headless':True}))
        assert ep['checkpoint_replay_audit']['matches'] is True
        assert ep['evaluation']['episode_success'] is True
        async def new_setup_page():
            async with Browser(tmp_path/'newsetup',headless=True) as browser:
                browser.page=await browser.context.new_page()
                await browser.execute({'op':'GOTO','url':url+'/'})
                await browser.execute({'op':'CLICK','node_id':'0'})
                assert browser.page.url==url+'/a'
                assert browser.pending_popups==[]
        asyncio.run(new_setup_page())
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
