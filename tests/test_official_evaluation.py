"""Exercise shipped REPLAN contracts, outcome semantics and trace-only recovery."""
import asyncio
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts')]
from eventarena_official.evaluation import normalize_constraints, normalized_case, evaluate_episode, POLICY, digest
from eventarena_official.evaluation_recovery import (
    descriptor, recover_evaluation, save_evaluation_failure, migrate_constraint_schema, KNOWN_SOURCE_CODE)
from eventarena.online_v2_eval import evaluate_episode as legacy_evaluate
from run_online_v2 import job_path


def facts():
    obs = {'url': 'https://example.test/a', 'text': 'alpha beta', 'candidates': []}
    return {'case_id': 'C', 'status': 'agent_finished', 'protocol': 'controlled_episode_v2',
            'checkpoint_replay_audit': {'matches': True, 'available': True},
            'checkpoint_replay_cleanup_verified': True, 'exposed': True,
            'responses': [{'stage': 'actor', 'raw': 'saved model response'}],
            'actions': [{'index': 3, 'task_id': 'A', 'action': {'op': 'SAVE_RESULT'}, 'after': obs}],
            'observations': [obs], 'task_states': {'A': {'completed': True, 'goal_version': 1, 'completion_index': 4}},
            'events': {'E1': {'delivered': True, 'delivery_index': 1, 'started_index': 1,
                              'accepted_index': 1, 'completion_index': 1, 'effect_applied': True}},
            'task_outputs': {'A': [{'text': 'alpha', 'goal_version': 1,
                                   'sources': [{'url': obs['url'], 'quote': 'alpha'}]}]},
            'decisions': {'E1': 'INTERRUPT'}, 'follow_ups': {'E1': 'REPLAN'}}


def registration():
    verifier = {'method': 'deterministic', 'goal': 'Find alpha and beta',
                'checks': [{'type': 'saved_output_contains', 'value': 'alpha'}],
                'effective_constraints': [
                    {'id': 'new', 'origin': 'new', 'text': 'Find alpha',
                     'verification': {'method': 'deterministic', 'checks': [{'type': 'saved_output_contains', 'value': 'alpha'}]}},
                    {'id': 'old', 'origin': 'retained', 'text': 'Also preserve beta',
                     'verification': {'method': 'deterministic', 'checks': [{'type': 'saved_output_contains', 'value': 'beta'}]}}]}
    return {'case_id': 'C', 'goal': 'Find beta',
            'events': [{'id': 'E1', 'text': 'Narrow request', 'execution': {
                'kind': 'update_goal', 'updated_goal': 'Find alpha and beta', 'updated_verification': verifier}}],
            'gold': {'decisions': {'E1': {'decision': 'INTERRUPT', 'follow_up': 'REPLAN'}}},
            'verification': {'CURRENT_TASK': {'method': 'llm_judge'}, 'CURRENT_TASK_UPDATED': copy.deepcopy(verifier)}}


def test_text_alias_matches_frozen_independent_constraint_semantics():
    case, episode = registration(), facts()
    original = copy.deepcopy((case, episode))
    canonical, _ = normalized_case(case)
    expected = asyncio.run(legacy_evaluate(canonical, episode))
    actual = asyncio.run(evaluate_episode(case, episode))
    assert actual == expected
    assert actual['updated_goal_success'] is True
    assert [r['success'] for r in actual['effective_constraint_results']] == [True, False]
    assert actual['episode_success'] is False
    assert (case, episode) == original


def test_alias_keeps_cached_negative_task_verdict_and_checks_constraints():
    case, episode = registration(), facts()
    verifier = case['events'][0]['execution']['updated_verification']
    episode['verified_tasks'] = {'A': {
        'output_hash': digest(episode['task_outputs']['A']), 'goal_version': 1,
        'goal': 'Find alpha and beta', 'verification_sha256': digest(verifier),
        'result': {'success': False, 'reason': 'Observed task failure'}}}
    async def never_judge(messages):
        pytest.fail('A known task verdict and deterministic constraints require no new Judge call')
    actual = asyncio.run(evaluate_episode(case, episode, never_judge))
    assert actual['task_success'] is False
    assert actual['phase_results']['CURRENT_TASK']['reused_independent_verification'] is True
    assert [r['success'] for r in actual['effective_constraint_results']] == [True, False]


@pytest.mark.parametrize('row', [
    {'id': 'x', 'origin': 'new'}, {'id': 'x', 'origin': 'new', 'text': ''},
    {'id': 'x', 'origin': 'new', 'text': 'a', 'requirement': 'b'},
    {'id': 'x', 'origin': 'wrong', 'text': 'a'}, {'origin': 'new', 'text': 'a'},
    {'id': 'x', 'origin': 'new', 'text': 'a', 'verification': 'invalid'}])
def test_invalid_constraints_rejected_before_execution(row):
    with pytest.raises(ValueError): normalize_constraints([row])


def test_all_shipped_specs_and_registered_experiments_have_valid_evaluator_contract():
    from eventarena_official.registration import validate_extension
    from run_online_v2 import baseline_case
    directory = ROOT / 'data/official_online/events_pilot'
    validate_extension(directory)
    count = 0
    for filename in ('single_event.jsonl', 'counterfactual.jsonl', 'multi_event.jsonl'):
        for line in (directory / filename).read_text().splitlines():
            case = json.loads(line)
            normalized_case(case); count += 1
            if filename == 'single_event.jsonl': normalized_case(baseline_case(case))
    assert count == 19


def write_actor(tmp_path):
    folder = tmp_path / 'job'; attempt = folder / 'attempts/attempt_001'
    attempt.mkdir(parents=True)
    path = attempt / 'episode.json'; path.write_text(json.dumps(facts()))
    return folder, attempt, path


def test_evaluation_only_recovery_preserves_model_actions_and_original_trace(tmp_path):
    folder, attempt, path = write_actor(tmp_path)
    before = path.read_bytes()
    recovered = asyncio.run(recover_evaluation(registration(), {'evaluation_recovery': descriptor(path)}, folder, None))
    assert recovered['actions'] == facts()['actions'] and recovered['responses'] == facts()['responses']
    assert recovered['evaluation']['episode_success'] is False
    assert recovered['infrastructure_recovery'][-1]['actor_resampled'] is False
    assert path.read_bytes() == before


def test_failure_saves_full_episode_as_pending_and_partial_traces_do_not_qualify(tmp_path):
    folder, attempt, path = write_actor(tmp_path)
    state = {'state': 'running'}
    assert save_evaluation_failure(folder, attempt, state, KeyError('requirement'))
    assert json.loads((folder/'job.json').read_text())['state'] == 'evaluation_pending'
    assert json.loads((folder/'episode.json').read_text())['actions'] == facts()['actions']
    data = facts(); data['status'] = 'started'; path.write_text(json.dumps(data))
    assert not save_evaluation_failure(folder, attempt, state, KeyError('requirement'))


def test_corrupt_actor_trace_cannot_be_recovered(tmp_path):
    folder, _, path = write_actor(tmp_path)
    job = {'evaluation_recovery': descriptor(path)}
    path.write_text(json.dumps({**facts(), 'decisions': {'E1': 'IGNORE'}}))
    with pytest.raises(ValueError, match='changed'):
        asyncio.run(recover_evaluation(registration(), job, folder, None))


def manifests():
    job = {'id': 'ablation/M/goal/0/C', 'experiment': 'ablation', 'model': 'M',
           'view': 'goal', 'repeat': 0, 'case_id': 'C'}
    previous = {'code_sha256': KNOWN_SOURCE_CODE, 'protocol_version': 'official_online_eventarena_v5',
                'jobs': [job], 'arguments': {'repeats': 1}, 'dataset_sha256': 'data'}
    current = {**previous, 'code_sha256': 'new-verified-code', 'evaluation_schema_policy': POLICY}
    return previous, current, job


def test_migration_records_original_manifest_and_queues_only_failed_evaluation(tmp_path):
    previous, current, job = manifests()
    (tmp_path/'manifest.json').write_text(json.dumps(previous))
    folder = job_path(tmp_path, job); attempt = folder/'attempts/attempt_001'; attempt.mkdir(parents=True)
    (attempt/'episode.json').write_text(json.dumps(facts()))
    (attempt/'error_traceback.txt').write_text("requirement = constraint['requirement']")
    (folder/'job.json').write_text(json.dumps({**job, 'state': 'finished', 'attempt': 1}))
    (folder/'episode.json').write_text(json.dumps({'status': 'runner_error', 'error_type': 'KeyError'}))
    migrate_constraint_schema(tmp_path, previous, current)
    assert json.loads((folder/'job.json').read_text())['state'] == 'evaluation_pending'
    assert json.loads((tmp_path/'repairs/constraint_schema_v1/manifest_before.json').read_text()) == previous
    assert json.loads((tmp_path/'manifest.json').read_text()) == current


@pytest.mark.parametrize('change', [{'dataset_sha256': 'other'}, {'arguments': {'repeats': 3}},
                                  {'protocol_version': 'other'}])
def test_migration_rejects_changed_experiment_settings(tmp_path, change):
    previous, current, _ = manifests()
    current.update(change)
    with pytest.raises(ValueError): migrate_constraint_schema(tmp_path, previous, current)
