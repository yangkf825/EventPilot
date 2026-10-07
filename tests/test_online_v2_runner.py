import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('run_online_v2', ROOT / 'scripts/run_online_v2.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_all_backbones_four_views_repeats_are_registered():
    selected = {'main': [{'case_id': 'X'}], 'ablation': [{'case_id': 'X'}]}
    jobs = runner.make_jobs(selected, ['ds', 'glm'], 3, 12)
    assert len(jobs) == 30
    assert len({j['id'] for j in jobs}) == 30
    assert {(j['model'], j['view']) for j in jobs if j['experiment'] == 'ablation'} == {
        (model, view) for model in ('ds','glm') for view in runner.VIEWS}
    assert jobs == runner.make_jobs(selected, ['ds','glm'], 3, 12)


def test_no_event_baseline_retains_same_goal_and_removes_event_binding():
    case = {'case_id':'X','goal':'Read A','events':[{'id':'E1','execution':{'kind':'research_task'}}],
            'gold':{'decision':'INTERRUPT','follow_up':'HANDLE'},
            'verification': {'CURRENT_TASK': {'checks':[{'type':'required_event_completed','event_id':'E1'},
                                                       {'type':'saved_output_contains','value':'A'}]}}}
    baseline = runner.baseline_case(case)
    assert baseline['goal'] == case['goal']
    assert baseline['events'] == []
    assert baseline['gold'] == {}
    assert baseline['verification']['CURRENT_TASK']['checks'] == [{'type':'saved_output_contains','value':'A'}]
    assert case['events'] and len(case['verification']['CURRENT_TASK']['checks']) == 2


def test_final_intent_baseline_uses_new_goal_and_new_verifier():
    case = {'case_id':'X','goal':'Old','events':[{'id':'E1','execution':{'kind':'update_goal','updated_goal':'New',
                   'updated_verification':{'method':'hybrid','goal':'New','checks':[]}}}],
            'verification':{'CURRENT_TASK':{'goal':'Old','checks':[]}}}
    result = runner.baseline_case(case, True)
    assert result['goal'] == 'New'
    assert result['verification']['CURRENT_TASK']['goal'] == 'New'
    assert result['setup_tasks'] == []


def test_status_tracks_every_registered_pending_job(tmp_path):
    jobs = runner.make_jobs({'main':[{'case_id':'X'}]}, ['ds','glm'], 1, 2)
    runner.write_json(tmp_path / 'manifest.json', {'jobs':jobs})
    assert runner.status(tmp_path)['states'] == {'pending':2}
    runner.write_json(runner.job_path(tmp_path, jobs[0]) / 'job.json', {'state':'finished'})
    assert runner.status(tmp_path)['states'] == {'finished':1,'pending':1}


def test_credential_values_not_written_to_manifest_config():
    original = {'api_key':'never-publish','api_key_env':'GATEWAY_DS_API_KEY',
                'extra_body': {'token':'do-not-print','temperature':0}}
    value = runner.public_config(original)
    assert value['api_key'] == '[redacted]'
    assert value['api_key_env'] == 'GATEWAY_DS_API_KEY'
    assert value['extra_body']['token'] == '[redacted]'
    assert original['api_key'] == 'never-publish'


def test_metric_record_excludes_large_prompt_history_and_keeps_outcome():
    episode = {'case_id':'X','repeat':2,'protocol':'continuous_v2',
               'observations':[{'text':'large webpage'}], 'actions':[{'before':{'text':'large webpage'}}],
               'responses':[{'stage':'actor','messages':[{'content':'large webpage'}]},
                            {'stage':'decision','api_ok':True,'parsed':{'decision':'DEFER'},'messages':[]}],
               'evaluation':{'episode_success':False},'decisions':{'E1':'DEFER'}}
    result = runner.metric_record(episode)
    assert 'observations' not in result and 'actions' not in result
    assert result['evaluation']['episode_success'] is False
    assert len(result['responses']) == 1 and 'messages' not in result['responses'][0]


def test_same_shared_group_state_interventions_get_separate_actual_setup():
    shared = {'case_id':'S_A','checkpoint_group_id':'G','group_type':'state'}
    other = {**shared,'case_id':'S_B'}
    assert runner.checkpoint_key(shared, 0) != runner.checkpoint_key(other, 0)
    assert runner.checkpoint_key({**shared,'group_type':'goal'}, 0) == runner.checkpoint_key({**other,'group_type':'goal'}, 0)


def test_counterfactual_uses_same_state_information_for_all_interventions():
    assert runner.job_views('counterfactual') == ('state',)


def test_counterfactual_audit_rejects_hidden_extra_changes():
    cases = [{'case_id':'A','group_type':'goal','group_id':'G'},
             {'case_id':'B','group_type':'goal','group_id':'G'}]
    episodes = [{'case_id':c['case_id'],'counterfactual_input_audit':{
        'invariant_sha256':'same','intervention_sha256':c['case_id'],'intervention_visible':True}}
        for c in cases]
    assert runner.audit_counterfactual_groups(cases, episodes)[0]['valid'] is True
    episodes[1]['counterfactual_input_audit']['invariant_sha256'] = 'different_page'
    assert runner.audit_counterfactual_groups(cases, episodes)[0]['valid'] is False
    assert all(e['counterfactual_protocol_valid'] is False for e in episodes)


def test_missing_counterfactual_member_is_unverified():
    cases = [{'case_id':'A','group_type':'goal','group_id':'G'},
             {'case_id':'B','group_type':'goal','group_id':'G'}]
    episodes = [{'case_id':'A','counterfactual_input_audit':{
        'invariant_sha256':'same','intervention_sha256':'A','intervention_visible':True}},
        {'case_id':'B','status':'registered_pending'}]
    assert runner.audit_counterfactual_groups(cases, episodes)[0]['valid'] is None
