"""Official import fidelity, public-input isolation and executable registrations."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from eventarena_official.corpus import normalize,import_corpus,load_corpus,task_case
from eventarena_official.registration import compile_extension,validate_extension,pair_candidates
from eventarena_official.availability import classify,summary,targets,check_sites
from eventarena.online_v2_engine import AutonomousEpisode,actor_payload,decision_messages,match_trigger
from run_online_v2 import make_jobs,baseline_case,code_fingerprint


@pytest.fixture
def corpus(tmp_path):
    raw=[{'task_id':'t1','confirmed_task':'Find the Alpha specification.','website':'https://example.org/',
          'reference_length':73,'level':'easy'},
         {'task_id':'t2','confirmed_task':'Find the Beta specification.','website':'https://example.org/',
          'reference_length':11,'level':'medium'}]
    path=tmp_path/'official.json';path.write_text(json.dumps(raw))
    directory=tmp_path/'corpus';import_corpus(path,directory,'abc123')
    return directory,raw


def spec():
    return {'case_id':'OMW_S_TEST','task_a_id':'t1','rules':['Reuse verified results only.'],
            'trigger':{'min_actor_progress_actions':1,'all':[{'type':'url_contains','value':'/alpha'},
                                                        {'type':'text_contains','value':'Alpha'}]},
            'events':[{'id':'E1','source':'User','text':'Please also retrieve Beta.','kind':'research_task',
                       'task_b_id':'t2','gold':{'decision':'DEFER','follow_up':None},
                       'label_reason':'Additional task has no stated urgency.'}]}


def compile_specs(tmp_path,corpus,specs):
    p=tmp_path/'specs.json';p.write_text(json.dumps(specs))
    out=tmp_path/'extension';compile_extension(corpus,p,out)
    return out


def test_import_retains_every_raw_field_and_exact_goal(corpus):
    directory,raw=corpus
    tasks,manifest=load_corpus(directory)
    assert manifest['task_count']==2 and manifest['unique_entry_urls']==1
    assert [t['official_record'] for t in tasks]==raw
    assert [t['goal'] for t in tasks]==[r['confirmed_task'] for r in raw]
    assert task_case(tasks[0])['goal']==raw[0]['confirmed_task']
    p=directory/'tasks.jsonl';p.write_text(p.read_text().replace('Alpha','Gamma'))
    with pytest.raises(ValueError,match='altered'):load_corpus(directory)


@pytest.mark.parametrize('mutation',[lambda r:r.append(copy.deepcopy(r[0])),
                                    lambda r:r[0].update(website='https://user:secret@example.org/'),
                                    lambda r:r[0].update(confirmed_task='')])
def test_reject_malformed_official_input(corpus,mutation):
    _,raw=corpus;mutation(raw)
    with pytest.raises(ValueError):normalize(raw,'sha')


def test_labels_provenance_and_reference_length_never_enter_prompts(tmp_path,corpus):
    directory,_=corpus
    out=compile_specs(tmp_path,directory,[spec()])
    case=json.loads((out/'single_event.jsonl').read_text())
    engine=AutonomousEpisode(case,None,tmp_path/'episode')
    obs={'url':'https://example.org/alpha','text':'Alpha specification','candidates':[]}
    engine.episode['events']['E1']['delivered']=True
    actor=actor_payload(engine,obs)
    checkpoint={'goal':case['goal'],'rules':case['rules'],'state':obs,'events':case['events']}
    for text in (json.dumps(actor),json.dumps(decision_messages(checkpoint,'state'))):
        for forbidden in ('reference_length','official_record','raw_sha256','label_reason','gold','author_spec'):
            assert forbidden not in text
    assert 'Find the Alpha specification.' in json.dumps(actor)
    assert 'Find the Beta specification.' in json.dumps(actor)


def test_event_requires_actual_progress_and_task_content(tmp_path,corpus):
    directory,_=corpus;out=compile_specs(tmp_path,directory,[spec()])
    case=json.loads((out/'single_event.jsonl').read_text())
    obs={'url':'https://example.org/alpha','text':'Alpha specification','candidates':[]}
    assert not match_trigger(case['trigger'],obs,[])
    action={'task_id':'A','origin':'actor','action':{'op':'CLICK'},'error':None}
    assert match_trigger(case['trigger'],obs,[action])
    assert not match_trigger(case['trigger'],{**obs,'text':'Home'},[action])
    assert not match_trigger(case['trigger'],obs,[{**action,'origin':'feasibility_script'}])


def test_compiler_uses_official_b_and_rejects_hidden_task_rewrite(tmp_path,corpus):
    directory,raw=corpus;s=spec()
    s['events'][0]['goal']='INVENTED GOAL'
    out=compile_specs(tmp_path,directory,[s]);c=json.loads((out/'single_event.jsonl').read_text())
    assert c['goal']==raw[0]['confirmed_task']
    assert c['events'][0]['execution']['goal']==raw[1]['confirmed_task']
    assert c['events'][0]['provenance']['official_event'] is False
    with pytest.raises(ValueError,match='reviews'):validate_extension(out,strict_review=True)


def test_none_unsupported_labels_and_unknown_predicates_are_rejected(tmp_path,corpus):
    directory,_=corpus
    s=spec();s['events'][0]['kind']='none'
    with pytest.raises(ValueError,match='IGNORE'):compile_specs(tmp_path,directory,[s])
    s=spec();s['trigger']['all'].append({'type':'author_claims_done','value':True})
    with pytest.raises(ValueError,match='Unknown semantic'):compile_specs(tmp_path,directory,[s])


def test_no_access_result_can_claim_task_completion(corpus):
    directory,_=corpus;tasks,_=load_corpus(directory)
    rows=[{**targets(tasks)[0],'category':'entry_available','http_status':200}]
    r=summary(tasks,rows)
    assert r['entry_available_tasks']==2 and r['verified_task_successes']==0
    assert all(t['task_success_verified'] is False and t['task_status']=='not_executed' for t in r['tasks'])
    assert classify(200,{'title':'Trip.com-Verification','text':'To continue, please complete the verification test below.'})=='blocked_challenge'
    assert classify(200,{'title':'Kaggle','text':'Kaggle uses cookies from Google. Learn more OK, Got it.'})=='consent_or_script_only'
    assert classify(403,{'text':'Denied'})=='http_forbidden'


def test_pair_generation_never_infers_gold_or_dependency(corpus):
    directory,_=corpus;tasks,_=load_corpus(directory)
    p=pair_candidates(tasks,{'https://example.org/'})
    assert len(p)==1 and p[0]['gold'] is None and p[0]['dependency_verified'] is False


def test_shipped_pilot_has_all_experiments_and_official_fidelity():
    folder=ROOT/'data/official_online/events_pilot'
    if not folder.exists():pytest.skip('Local gated corpus not included in portable code')
    assert validate_extension(folder)['cases']==19
    from run_online_v2 import select_cases
    args=SimpleNamespace(data_dir=folder,experiments=['main','ablation','counterfactual','multi','baseline','final_intent'],
                         split='all',case_ids=None,multi_case_ids=None,cf_group_ids=None,
                         single_limit=10,cf_groups=1,multi_limit=2)
    selected=select_cases(args)
    assert {e:len(cs) for e,cs in selected.items()}=={'main':10,'ablation':10,'counterfactual':7,'multi':2,'baseline':10,'final_intent':2}
    assert len(make_jobs(selected,['gateway_ds'],1,1))==71
    assert selected['main']==selected['ablation']
    assert [c['goal'] for c in selected['main']]==[c['goal'] for c in selected['baseline']]
    for c in selected['main']+selected['counterfactual']+selected['multi']:
        assert c['goal']==c['source_task_a']['original_goal']
        for e in c['events']:
            if e['execution']['kind']=='research_task':
                assert e['execution']['goal']==e['source_task']['original_goal']


def test_legacy_resume_fingerprint_is_unchanged():
    assert code_fingerprint()=='e91b83c60259e9bf215bc769e7a6c56cf539086ebfa767172dd16a8fa43022ff'


def test_no_event_baseline_can_resume_entirely_cached_without_new_calls(tmp_path,monkeypatch):
    import asyncio
    from eventarena_official.baseline import execute
    from eventarena.providers import Model
    monkeypatch.setenv('TEST_OFFICIAL_KEY','local-test-placeholder')
    monkeypatch.setattr(Model,'call',lambda *a,**k:pytest.fail('Cached resume must not call the provider'))
    configuration=tmp_path/'models.json'
    configuration.write_text(json.dumps({'models':[{'name':'actor','backend':'chat-completions',
                                                    'model':'fixture','api_key_env':'TEST_OFFICIAL_KEY',
                                                    'endpoint':'https://example.org/no-call'}]}))
    tasks=normalize([{'task_id':'t1','confirmed_task':'Subscribe to Alpha updates.',
                      'website':'https://example.org/','reference_length':1,'level':'easy'}],'raw-sha')
    args=SimpleNamespace(config=configuration,only='actor',judge_model='actor',
                         source_manifest={'raw_sha256':'raw-sha','revision':'fixture'},project_root=ROOT,
                         max_steps=2,max_context_chars=10000,episode_timeout=10,headless=True,
                         dry_run=False,resume=False,retry_infra=False)
    root=tmp_path/'run'
    first=asyncio.run(execute(args,root,tasks))
    args.resume=True
    second=asyncio.run(execute(args,root,tasks))
    assert first['results']==second['results']
    assert second['completed']==1 and second['results'][0]['actor_called'] is False
    assert second['TaskCompletion']==first['TaskCompletion']
