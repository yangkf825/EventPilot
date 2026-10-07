"""Regression checks for label shortcuts, scope and task-verification contracts."""
import copy
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from run_online_v2 import read_cases,baseline_case
from build_online_v2 import anonymize_multi_ids

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data/online_v2'


def test_original_and_event_outputs_have_separate_verifiers():
    c=next(c for c in read_cases(DATA/'single_event.jsonl') if c['gold']['follow_up']=='HANDLE')
    spec=c['verification']['CURRENT_TASK']
    assert spec['goal']==c['task_content_goal']
    assert c['requirements']['acceptance_checks'][0] not in spec['requirements']
    assert c['requirements']['acceptance_checks'][0] not in spec['goal']
    assert any(ch['type']=='required_event_completed' for ch in spec['checks'])
    b=baseline_case(c)
    assert b['verification']['CURRENT_TASK']['goal']==b['goal']
    assert c['requirements']['acceptance_checks'][0] in b['verification']['CURRENT_TASK']['requirements']
    assert not any(ch['type']=='required_event_completed' for ch in b['verification']['CURRENT_TASK']['checks'])


def test_wrong_request_counterfactual_has_explicit_reference_in_goal_only():
    cf=read_cases(DATA/'counterfactual.jsonl')
    a=next(c for c in cf if c['case_id']=='ON2_G_01_A')
    b=next(c for c in cf if c['case_id']=='ON2_G_01_B')
    assert 'REQ_01_A' in a['goal'] and 'REQ_01_C' in b['goal']
    assert a['events'][0]['text']==b['events'][0]['text']
    assert a['events'][0]['text'].startswith('Request reference REQ_01_A.')
    assert 'request_reference' not in a and 'request_reference' not in b


def test_revision_preserves_market_information_and_horizon_boundaries():
    cases=read_cases(DATA/'single_event.jsonl')+read_cases(DATA/'multi_event.jsonl')
    for c in cases:
        for e in c['events']:
            if e['execution']['kind']!='update_goal':continue
            checks=e['execution']['updated_verification']['effective_constraints']
            retained=' '.join(ch['requirement'] for ch in checks if ch['origin']=='retained')
            if c['author_profile_id']=='P11':
                assert 'market-trend' in retained
                assert 'market-trend' in e['execution']['updated_goal']
            if c['author_profile_id']=='P12':
                assert 'cannot substantiate' in retained


def test_public_multi_ids_and_order_cannot_perfectly_predict_labels():
    from collections import defaultdict
    cases=read_cases(DATA/'multi_event.jsonl')
    ids,positions=defaultdict(set),defaultdict(set)
    for c in cases:
        for i,e in enumerate(c['events']):
            ids[e['id']].add(e['gold']['decision'])
            positions[i].add(e['gold']['decision'])
        assert set(c['gold']['decisions'])=={e['id'] for e in c['events']}
        for e in c['events']:
            assert e['event_id']==e['id']
            if e['gold']['decision']!='IGNORE':
                assert c['verification'][e['id']]==e['execution']['verification']
    assert all(len(v)>1 for v in ids.values())
    assert all(len(v)>1 for v in positions.values())


def test_identifier_permutation_does_not_consult_labels_and_keeps_all_references():
    case={'case_id':'TEST_IDS','events':[{'id':'E1','event_id':'E1','gold':'a'},
                                      {'id':'E2','delivery_after':['E1'],'gold':'b'}],
          'verification':{'E1':{'phase':'E1'}},'setup_tasks':[{'alias_event_id':'E1'}],
          'gold':{'required_nodes':['E1','E2'],'precedence':[['E1','E2']]}}
    changed=copy.deepcopy(case)
    changed['events'][0]['gold']='different';changed['events'][1]['gold']='also different'
    x,y=anonymize_multi_ids(case),anonymize_multi_ids(changed)
    assert x['identifier_registration']==y['identifier_registration']
    mapping=x['identifier_registration']['author_to_public_id']
    assert x['verification'][mapping['E1']]['phase']==mapping['E1']
    assert x['setup_tasks'][0]['alias_event_id']==mapping['E1']
    assert next(e for e in x['events'] if e['id']==mapping['E2'])['delivery_after']==[mapping['E1']]
    assert x['gold']['precedence']==[[mapping['E1'],mapping['E2']]]


def test_identical_related_notifications_need_goal_or_verified_state():
    cases=[c for c in read_cases(DATA/'single_event.jsonl')
           if c['author_profile_id']=='P01' and c['scenario_type'] in {'handle','defer','redelivery'}]
    assert len(cases)==3
    assert len({c['events'][0]['text'] for c in cases})==1
    assert {c['gold']['decision'] for c in cases}=={'IGNORE','DEFER','INTERRUPT'}
    assert next(c for c in cases if c['gold']['decision']=='IGNORE')['setup_tasks'][0]['require_verified_success'] is True
