"""Full-context round trips, bounded requests and actual browser continuation."""
import asyncio
import copy
import json
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from eventarena.online_v2_engine import (actor_payload,decode_actor_payload,decision_messages,
                                       lossless_trajectory,normalize_action)
from eventarena_official.engine import AutonomousEpisode,prepare_checkpoint,run_checkpoint_episode
from eventarena_official.transport import prepare_messages,decode_payload,short_css_path,expand_css_path,POLICY
from eventarena_official.corpus import normalize,task_case


def public_case():
    return task_case(normalize([{'task_id':'fixture','confirmed_task':'Find Alpha specifications.',
                                'website':'https://example.org/','reference_length':1,'level':'easy'}],'fixture')[0])


def source_payload(tmp_path):
    engine=AutonomousEpisode(public_case(),None,tmp_path)
    current={'url':'https://example.org/alpha','title':'Alpha','text':'完整正文 Alpha\n'*1500,
             'candidates':[{'node_id':str(i),'dom_path':'html>body>main>'+('section>'*35)+f'a[{i}]',
                            'tag':'a','text':f'Alpha control {i}','href':f'/alpha/{i}','id':'',
                            'value':'','literal':{'$ref':'SOURCE_LITERAL'},'disabled':False} for i in range(80)]}
    before=copy.deepcopy(current);before['text']=before['text'].replace('Alpha','Earlier Alpha',1)
    for i,c in enumerate(before['candidates']):
        c['node_id']=f'history-{i}';c['id']=f'historical-html-{i}'
    engine.episode['observations']=[{**before,'index':0,'action_index':0,'task_id':'A'},
                                     {**current,'index':1,'action_index':1,'task_id':'A'}]
    engine.episode['actions']=[{'index':0,'task_id':'A','action':{'op':'SCROLL','pixels':100},
                                'before':before,'after':current,'error':None}]
    return actor_payload(engine,{**current,'index':2,'action_index':1,'task_id':'A'})


def test_transport_keeps_exact_states_controls_literals_and_every_history_record(tmp_path):
    payload=source_payload(tmp_path)
    messages=[{'role':'system','content':'fixture'},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
    transmitted,audit=prepare_messages(messages)
    decoded=decode_payload(json.loads(transmitted[1]['content']))
    assert decoded==decode_actor_payload(payload)
    assert len(decoded['full_trajectory']['actions'])==1
    assert len(decoded['full_trajectory']['observations'])==2
    assert decoded['observation']['candidates'][0]['literal']=={'$ref':'SOURCE_LITERAL'}
    assert audit['sent_chars']<audit['original_chars']
    assert messages[1]['content']==json.dumps(payload,ensure_ascii=False)
    assert decoded['full_trajectory']['observations'][0]['candidates'][0]['node_id']=='history-0'
    assert decoded['observation']['candidates'][0]['node_id']=='0'


def test_css_path_encoding_preserves_exact_indices_and_rejects_other_selectors():
    original='html:nth-of-type(1)>body:nth-of-type(1)>custom-tag:nth-of-type(12)>a:nth-of-type(2)'
    short=short_css_path(original)
    assert short=='html>body>custom-tag[12]>a[2]'
    assert expand_css_path(short)==original
    for invalid in ('#main>a','div[aria-label="x"]','div:nth-of-type(0)','div:nth-of-type(01)','a > b'):
        assert short_css_path(invalid) is None
    with pytest.raises(ValueError):expand_css_path('div[0]')


def test_current_control_defaults_preserve_null_false_and_missing_fields(tmp_path):
    payload=source_payload(tmp_path)
    payload['observation']['candidates'][0]['disabled']=True
    payload['observation']['candidates'][1].pop('value')
    messages=[{'role':'system','content':'fixture'},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
    transmitted,audit=prepare_messages(messages);packed=json.loads(transmitted[1]['content'])
    assert decode_payload(packed)==decode_actor_payload(payload)
    defaults=packed['transport_encoding']['current_control_defaults']
    assert defaults['disabled'] is False and 'value' not in defaults
    assert packed['observation']['candidates'][0]['disabled'] is True
    assert packed['observation']['candidates'][0]['node_id']=='0'
    assert packed['observation']['candidates'][0]['text']=='Alpha control 0'
    assert prepare_messages(transmitted)[0]==transmitted
    assert audit['sent_chars']<audit['original_chars']


@pytest.mark.parametrize('view',['event-only','goal','state','trajectory'])
def test_transport_preserves_each_ablation_information_boundary(tmp_path,view):
    payload=source_payload(tmp_path);decoded=decode_actor_payload(payload)
    checkpoint={'goal':'PRIVATE_GOAL','rules':['COMMON_RULE'],'state':{
        'summary':decoded['observation']['text'],'candidates':decoded['observation']['candidates']},
        'trajectory':lossless_trajectory(decoded['full_trajectory']),
        'event':{'id':'E1','source':'User','text':'EVENT','gold':'PRIVATE_GOLD'}}
    original=decision_messages(checkpoint,view)
    transmitted,_=prepare_messages(original)
    a=json.loads(original[1]['content']);b=json.loads(transmitted[1]['content'])
    assert decode_payload(a)==decode_payload(b)
    assert 'PRIVATE_GOLD' not in transmitted[1]['content']
    assert ('original_user_task' in b)==(view!='event-only')
    assert ('current_state' in b)==(view in {'state','trajectory'})
    assert ('full_trajectory' in b)==(view=='trajectory')


def test_engine_checks_actual_transmitted_size_and_still_rejects_real_overflow(tmp_path):
    payload=source_payload(tmp_path/'source')
    messages=[{'role':'system','content':'fixture'},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
    _,audit=prepare_messages(messages)
    budget=(audit['original_chars']+audit['sent_chars'])//2
    assert audit['sent_chars']<budget<audit['original_chars']
    class Model:
        def __init__(self):self.calls=[]
        def call(self,messages):self.calls.append(messages);return '{"op":"WAIT","seconds":0}'
    model=Model();engine=AutonomousEpisode(public_case(),model,tmp_path/'allowed',{'max_context_chars':budget})
    row=asyncio.run(engine.call_record('actor',messages,normalize_action))
    assert row['error'] is None and row['request_sent'] is True and len(model.calls)==1
    assert row['messages']==model.calls[0] and row['input_transport']['sent_chars']<budget
    model=Model();engine=AutonomousEpisode(public_case(),model,tmp_path/'denied',{'max_context_chars':1})
    row=asyncio.run(engine.call_record('actor',messages,normalize_action))
    assert row['error']=='context_overflow' and row['request_sent'] is False and model.calls==[]


@pytest.mark.browser
def test_official_transport_runs_real_checkpoint_interrupt_handle_and_recovery(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from test_online_v203_controlled import case,Model
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            value='beta' if self.path=='/b' else 'alpha'
            body=f'<html><title>Actual research page</title><body><p>Research results {value}</p><button>Details</button></body></html>'.encode()
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    c=case('HANDLE');c['url']=url+'/'
    c['trigger']={'all':[{'kind':'url_contains','value':'/a'}],'min_actor_progress_actions':1}
    c['events']=[c.pop('event')];c['events'][0]['execution']['url']=url+'/b'
    try:
        cp=asyncio.run(prepare_checkpoint(c,Model([{'op':'GOTO','url':url+'/a'}]),tmp_path/'prep',{'headless':True}))['checkpoint']
        actions=[{'decision':'INTERRUPT','follow_up':'HANDLE'},
                 {'op':'SWITCH_TASK','task_id':'E1'},{'op':'GOTO','url':url+'/b'},
                 {'op':'SAVE_RESULT','text':'beta','sources':[{'url':url+'/b','quote':'beta'}]},
                 {'op':'COMPLETE_TASK'},{'op':'SWITCH_TASK','task_id':'A'},
                 {'op':'SAVE_RESULT','text':'alpha','sources':[{'url':url+'/a','quote':'alpha'}]},
                 {'op':'COMPLETE_TASK'},{'op':'ANSWER','answer':'done'}]
        episode=asyncio.run(run_checkpoint_episode(c,Model(actions),cp,'trajectory',tmp_path/'episode',{'headless':True}))
        assert episode['evaluation']['episode_success'] is True
        assert episode['checkpoint_replay_audit']['matches'] is True
        assert all('input_transport' in row for row in episode['responses'])
        assert episode['input_transport_policy']==POLICY
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
