"""Scoped history access, growing trajectories, budgets and real task execution."""
import asyncio
import copy
import json
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from eventarena_official.context_access import (ContextAccess,LIMITS,indexed_value,
                                              reconstruct_index,read_request)
from eventarena_official.engine import AutonomousEpisode,run_episode
from eventarena.online_v2_engine import decision_messages,normalize_action,lossless_trajectory
from test_official_transport import public_case,source_payload


def long_public_input():
    # Forty different pages: compression cannot erase genuine new evidence.
    observations=[{'index':i,'action_index':i,'task_id':'A','url':f'https://example.org/{i}',
                   'title':f'Page {i}','text':f'Page {i} original passage.\n'*5000,
                   'candidates':[{'node_id':str(j),'text':f'Control {i}/{j}','href':f'/{i}/{j}'} for j in range(150)]}
                  for i in range(40)]
    actions=[{'index':i,'task_id':'A','action':{'op':'GOTO','url':o['url']},
              'before':observations[max(i-1,0)],'after':o,'error':None}
             for i,o in enumerate(observations)]
    return {'original_user_task':'Research','observation':observations[-1],
            'full_trajectory':{'observations':observations,'actions':actions},
            'literal':{'$context':{'path':['FAKE_SOURCE_LITERAL']}}}


def test_index_preserves_every_record_and_exact_literal_source_values():
    source=long_public_input();view=indexed_value(source,[],1)
    assert len(view['full_trajectory']['observations'])==40
    assert len(view['full_trajectory']['actions'])==40
    assert reconstruct_index(view,source)==source
    assert len(json.dumps(view))<450000


def test_forty_unique_pages_fit_without_forgetting_any_history():
    source=long_public_input()
    access=ContextAccess([{'role':'system','content':'fixture'},
                          {'role':'user','content':json.dumps(source)}])
    messages,audit=access.messages(450000,[],12)
    assert audit['mode']=='indexed' and audit['sent_chars']<450000
    view=json.loads(messages[1]['content'])['public_input']
    assert reconstruct_index(view,access.sources[1])==source
    for i in range(40):
        result=access.read({'path':['full_trajectory','observations',i,'text'],'offset':0,'limit':100})
        assert result['value']==source['full_trajectory']['observations'][i]['text'][:100]
        assert result['has_more'] is True


def test_string_paging_is_exact_and_caps_escaped_json_size():
    text=('中文\n\t"\\' * 8000)+'Tail'
    access=ContextAccess([{'role':'user','content':json.dumps({'text':text})}])
    offset=0;parts=[]
    while offset<len(text):
        result=access.read({'path':['text'],'offset':offset,'limit':16000})
        assert len(json.dumps(result,ensure_ascii=False,separators=(',',':')))<=LIMITS['max_read_chars']
        assert result['next_offset']>offset
        parts.append(result['value']);offset=result['next_offset']
    assert ''.join(parts)==text


def test_control_search_returns_actual_indices_and_fresh_ids():
    controls=[{'node_id':'old','text':'Series'}, {'node_id':'44','text':'Ultra compare','href':'/ultra'}]
    access=ContextAccess([{'role':'user','content':json.dumps({'observation':{'candidates':controls}})}])
    result=access.read({'path':['observation','candidates'],'query':'ULTRA','limit':20})
    assert result['items']==[{'index':1,'value':controls[1]}]
    assert result['has_more'] is False


@pytest.mark.parametrize('view',['event-only','goal','state','trajectory'])
def test_reads_cannot_cross_ablation_boundaries_or_reveal_gold(view):
    cp={'goal':'VISIBLE_ONLY_WITH_GOAL','rules':[],
        'state':{'summary':'STATE_DETAILS','candidates':[]},
        'trajectory':lossless_trajectory({'observations':[{'text':'PRE_EVENT_HISTORY','index':0}],'actions':[]}),
        'event':{'id':'E1','source':'User','text':'Event','gold':'HIDDEN_GOLD'}}
    access=ContextAccess(decision_messages(cp,view))
    assert ('error' not in access.read({'path':['original_user_task']}))==(view!='event-only')
    assert ('error' not in access.read({'path':['current_state','summary']}))==(view in {'state','trajectory'})
    assert ('error' not in access.read({'path':['full_trajectory','observations',0,'text']}))==(view=='trajectory')
    for path in (['incoming_event','gold'],['gold'],['/etc/passwd'],['..']):
        assert access.read({'path':path})['error']=='invalid_or_unavailable_public_path'


def test_engine_read_then_final_action_uses_bounded_requests_and_keeps_audit(tmp_path):
    source=long_public_input();messages=[{'role':'system','content':'fixture'},
                                       {'role':'user','content':json.dumps(source)}]
    class Model:
        def __init__(self):self.messages=[]
        def call(self,messages):
            self.messages.append(messages)
            if len(self.messages)==1:return '{"op":"READ_CONTEXT","path":["full_trajectory","observations",39,"text"],"limit":1000}'
            return '{"op":"WAIT","seconds":0}'
    model=Model();engine=AutonomousEpisode(public_case(),model,tmp_path,{'max_context_chars':450000})
    row=asyncio.run(engine.call_record('actor',messages,normalize_action))
    assert row['parsed']=={'op':'WAIT','seconds':0}
    assert [r['stage'] for r in engine.episode['responses']]==['context_read','actor']
    assert len(engine.episode['context_reads'])==1 and engine.episode['actions']==[]
    assert all(sum(len(m['content']) for m in call)<=450000 for call in model.messages)
    source_file=next((tmp_path/'public_contexts').glob('*.json'))
    saved=json.loads(source_file.read_text())
    assert ContextAccess([{'role':'user','content':json.dumps(saved['encoded_public_payloads']['1'])}]).sources[0]==source


def test_repeated_memory_reads_stop_at_registered_budget(tmp_path):
    class Model:
        def __init__(self):self.calls=0
        def call(self,messages):self.calls+=1;return '{"op":"READ_CONTEXT","path":["x"]}'
    model=Model();engine=AutonomousEpisode(public_case(),model,tmp_path)
    row=asyncio.run(engine.call_record('decision',[{'role':'system','content':'fixture'},
        {'role':'user','content':'{"x":"data"}'}],lambda raw:None))
    assert row['error']=='step_budget_exhausted'
    assert len(engine.episode['context_reads'])==LIMITS['max_reads_per_call']
    assert model.calls==LIMITS['max_reads_per_call']+1
    assert engine.episode['status']=='step_budget_exhausted'
    assert not any(r['stage']=='decision' and r['parsed'] for r in engine.episode['responses'])


@pytest.mark.browser
def test_actual_browser_task_can_read_large_page_then_save_grounded_result(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from test_online_v203_controlled import case
    body='Research results alpha\n'+('Long public page evidence.\n'*6000)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            page=('<html><title>Research</title><body><pre>'+body+'</pre></body></html>').encode()
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(page)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    c=case('HANDLE');c['url']=url;c.pop('event',None);c.pop('trigger',None)
    actions=[{'op':'READ_CONTEXT','path':['observation','text'],'offset':0,'limit':1000},
             {'op':'SAVE_RESULT','text':'alpha','sources':[{'url':url+'/','quote':'Research results alpha'}]},
             {'op':'COMPLETE_TASK'},{'op':'ANSWER','answer':'done'}]
    class Model:
        def __init__(self):self.calls=[]
        def call(self,messages):self.calls.append(messages);return json.dumps(actions.pop(0))
    model=Model()
    try:
        episode=asyncio.run(run_episode(c,model,tmp_path/'episode',{'headless':True,'max_context_chars':12000}))
        assert episode['status']=='agent_finished'
        assert episode['evaluation']['phase_results']['CURRENT_TASK']['success'] is True
        assert len(episode['context_reads'])==1
        assert episode['context_reads'][0]['result']['value'].startswith('Research results alpha')
        assert all(sum(len(m['content']) for m in call)<=12000 for call in model.calls)
        assert any(r['input_transport']['mode']=='indexed' for r in episode['responses'])
    finally:server.shutdown();server.server_close();thread.join(timeout=3)
