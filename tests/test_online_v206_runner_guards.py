"""Registered run guards and recovery preserve Actor sampling and source facts."""
import asyncio
import copy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
import run_online_v2 as runner
import prepare_online_v2 as preparation
from eventarena.run_lock import exclusive_run, RunAlreadyActiveError
from test_online_v2_engine import case


@pytest.mark.parametrize('command', ['run', 'prepare'])
def test_formal_commands_reject_on_demand_before_registration_or_provider_work(tmp_path, command):
    root = tmp_path / 'run'
    with pytest.raises(ValueError, match='before-actors'):
        runner.main([command, '--run-root', str(root), '--preparation-mode', 'on-demand'])
    assert not root.exists()


def test_evaluate_cannot_write_while_the_registered_run_is_active(tmp_path, monkeypatch):
    root = tmp_path / 'run'
    runner.write_json(root / 'manifest.json', {'code_sha256': runner.code_fingerprint(),
                                               'arguments': {}, 'counts': {}, 'models': []})
    monkeypatch.setattr(runner, 'summarize', lambda *_: pytest.fail('Active runs cannot be overwritten'))
    with exclusive_run(root):
        with pytest.raises(RunAlreadyActiveError):
            runner.main(['evaluate', '--run-root', str(root)])


def test_evaluate_holds_the_exclusive_lock_during_all_result_writes(tmp_path, monkeypatch):
    root = tmp_path / 'run'
    runner.write_json(root / 'manifest.json', {'code_sha256': runner.code_fingerprint(),
                                               'arguments': {}, 'counts': {}, 'models': []})
    writes = []
    def summarize(*_):
        with pytest.raises(RunAlreadyActiveError):
            with exclusive_run(root):
                pytest.fail('A result writer escaped the lock')
        runner.write_json(root / 'metrics.json', {'locked': True})
        writes.append(True)
    monkeypatch.setattr(runner, 'summarize', summarize)
    runner.main(['evaluate', '--run-root', str(root)])
    assert writes == [True]
    with exclusive_run(root):
        assert runner.read_json(root / 'metrics.json') == {'locked': True}


def intact_unknown_episode():
    page = {'url': 'https://example.test/a', 'text': 'Observed alpha research evidence',
            'candidates': [], 'task_id': 'A', 'index': 0}
    action = {'op': 'SAVE_RESULT', 'text': 'alpha',
              'sources': [{'url': page['url'], 'quote': 'alpha research evidence'}]}
    return {'case_id': 'V2', 'protocol': 'controlled_episode_v2', 'status': 'agent_finished',
            'checkpoint_replay_audit': {'matches': True, 'available': True},
            'actions': [{'index': 0, 'task_id': 'A', 'action': action,
                         'before': copy.deepcopy(page), 'after': copy.deepcopy(page), 'error': None}],
            'observations': [page], 'final_observation': copy.deepcopy(page),
            'responses': [{'stage': 'actor', 'api_ok': True, 'parsed': action, 'raw': json.dumps(action)}],
            'task_states': {'A': {'goal_version': 0, 'completed': True}},
            'task_outputs': {'A': [{'text': 'alpha', 'sources': action['sources'], 'goal_version': 0}]},
            'events': {'E1': {'delivered': True, 'started_index': None}}, 'exposed': True,
            'decisions': {'E1': 'IGNORE'}, 'follow_ups': {'E1': None}, 'actual_schedule': ['CURRENT_TASK'],
            'verified_tasks': {'A': {'result': {'success': None}, 'output_hash': 'original'},
                               'KNOWN_TRUE': {'result': {'success': True}},
                               'KNOWN_FALSE': {'result': {'success': False}}},
            'evaluation': {'episode_success': None, 'environment_valid': True,
                           'failure_category': 'judge_or_outcome_unverified'}}


@pytest.mark.parametrize('status', ['invalid_output', 'context_overflow', 'output_truncated',
                                   'loop_guard_exhausted', 'step_budget_exhausted', 'checkpoint_setup_failed'])
def test_known_actor_failures_are_never_infrastructure_retried(status):
    episode = intact_unknown_episode()
    episode.update(status=status)
    episode['evaluation'].update(episode_success=False, failure_category=status)
    assert runner.retryable_infrastructure_episode(episode) is False
    assert runner.rejudgeable_episode(episode) is False


@pytest.mark.parametrize('outcome', [True, False])
def test_known_outcomes_are_not_retried_even_with_stale_unknown_category(outcome):
    episode = intact_unknown_episode()
    episode['evaluation']['episode_success'] = outcome
    assert runner.retryable_infrastructure_episode(episode) is False


@pytest.mark.parametrize('damage', ['environment', 'trace', 'checkpoint', 'unfinished'])
def test_only_intact_completed_environment_valid_episodes_are_rejudged(damage):
    episode = intact_unknown_episode()
    assert runner.retryable_infrastructure_episode(episode)
    assert runner.rejudgeable_episode(episode)
    if damage == 'environment':
        episode['evaluation']['environment_valid'] = False
    elif damage == 'trace':
        episode['actions'][0].pop('after')
    elif damage == 'checkpoint':
        episode['checkpoint_replay_audit']['matches'] = False
    else:
        episode['status'] = 'interrupted'
    assert runner.rejudgeable_episode(episode) is False


def test_rejudge_invalidates_only_unknown_cache_and_preserves_all_actor_facts(monkeypatch):
    from eventarena import online_v2_eval
    original = intact_unknown_episode()
    saved = copy.deepcopy(original)
    async def evaluate(c, episode, judge):
        assert 'A' not in episode['verified_tasks']
        assert episode['verified_tasks']['KNOWN_TRUE']['result']['success'] is True
        assert episode['verified_tasks']['KNOWN_FALSE']['result']['success'] is False
        return {'episode_success': False, 'environment_valid': True, 'actual_schedule': ['CURRENT_TASK']}
    monkeypatch.setattr(online_v2_eval, 'evaluate_episode', evaluate)
    result = asyncio.run(runner.rejudge_saved_episode(case(), original, None))
    assert original == saved
    assert {k: result[k] for k in runner.ACTOR_FACT_FIELDS if k in result} == {
        k: original[k] for k in runner.ACTOR_FACT_FIELDS if k in original}
    audit = result['infrastructure_recovery'][-1]
    assert audit['actor_resampled'] is False and audit['mode'] == 'judge_only'
    assert audit['invalidated_unknown_verifications'] == ['A']


def _shared_fixture(tmp_path, monkeypatch, episode):
    from eventarena import providers, online_v2_engine
    c = case('IGNORE')
    c['events'] = [c.pop('event')]
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'single_event.jsonl').write_text(json.dumps(c) + '\n')
    root = tmp_path / 'run'
    selected = {'main': [c], 'ablation': [c]}
    jobs = [j for j in runner.make_jobs(selected, ['unit'], 1, 12) if j['view'] == 'trajectory']
    runner.write_json(root / 'manifest.json', {'jobs': jobs})
    shared = root / 'shared_episodes/unit/repeat_0/V2/episode.json'
    runner.write_json(shared, episode)
    for job in jobs:
        runner.write_json(runner.job_path(root, job) / 'episode.json', episode)
        runner.write_json(runner.job_path(root, job) / 'job.json', {**job, 'state': 'finished', 'attempt': 1})
    args = runner.parser().parse_args(['run', '--run-root', str(root), '--data-dir', str(data),
                                       '--only', 'unit', '--workers', '2', '--resume', '--retry-infra'])
    args.prep_model = args.judge_model = 'unit'
    provider_calls = []
    class JudgeOnlyModel:
        def __init__(self, *_):
            self.usage = []
        def call(self, messages):
            assert messages[0]['content'] == 'Independent judge of unchanged evidence'
            provider_calls.append(messages)
            return json.dumps({'success': True})
    async def gates(*_):
        return {('V2', 0): {'eligible': True, 'reason': 'reset_verified'}}
    async def checkpoint(*_):
        return {'status': 'checkpoint_ready'}, {'case_id': 'V2'}, 'V2_repeat_0'
    async def forbidden_actor(*_, **__):
        pytest.fail('A completed Actor cannot be resampled to fix Judge uncertainty')
    monkeypatch.setattr(providers, 'Model', JudgeOnlyModel)
    monkeypatch.setattr(preparation, 'prepare_pool', gates)
    monkeypatch.setattr(runner, 'obtain_checkpoint', checkpoint)
    monkeypatch.setattr(online_v2_engine, 'run_checkpoint_episode', forbidden_actor)
    monkeypatch.setattr(runner, 'summarize', lambda *_: None)
    return args, root, selected, jobs, shared, provider_calls


@pytest.mark.parametrize('new_outcome', [True, False, None])
def test_shared_unknown_is_rejudged_once_and_both_aliases_keep_identical_actor_metrics(
        tmp_path, monkeypatch, new_outcome):
    from eventarena import online_v2_eval
    original = intact_unknown_episode()
    args, root, selected, jobs, shared, calls = _shared_fixture(tmp_path, monkeypatch, original)
    evaluations = []
    async def evaluate(c, episode, judge):
        evaluations.append(True)
        await judge([{'role': 'system', 'content': 'Independent judge of unchanged evidence'}])
        return {'episode_success': new_outcome, 'environment_valid': True,
                'failure_category': 'judge_or_outcome_unverified' if new_outcome is None else None,
                'actual_schedule': ['CURRENT_TASK']}
    monkeypatch.setattr(online_v2_eval, 'evaluate_episode', evaluate)
    asyncio.run(runner.execute(args, root, selected, ['unit']))
    assert len(evaluations) == len(calls) == 1
    saved = [runner.read_json(runner.job_path(root, j) / 'episode.json') for j in jobs]
    assert saved[0]['evaluation'] == saved[1]['evaluation']
    assert saved[0]['decisions'] == saved[1]['decisions'] == original['decisions']
    for episode, job in zip(saved, jobs):
        for field in runner.ACTOR_FACT_FIELDS:
            if field in original:
                assert episode[field] == original[field]
        previous = runner.job_path(root, job) / 'previous_attempts/episode_1.json'
        assert runner.read_json(previous) == original
    archives = list(shared.parent.glob('episode_failed_*.json'))
    assert len(archives) == 1 and runner.read_json(archives[0]) == original
    assert runner.read_json(shared)['evaluation']['episode_success'] is new_outcome
    assert [runner.read_json(runner.job_path(root, j) / 'job.json')['attempt'] for j in jobs] == [2, 2]


def test_shared_known_actor_failure_is_cached_without_rejudging_or_new_attempt(tmp_path, monkeypatch):
    from eventarena import online_v2_eval
    original = intact_unknown_episode()
    original.update(status='loop_guard_exhausted')
    original['evaluation'].update(episode_success=False, failure_category='loop_guard_exhausted')
    args, root, selected, jobs, shared, calls = _shared_fixture(tmp_path, monkeypatch, original)
    async def forbidden_judge(*_, **__):
        pytest.fail('A known Actor failure cannot be rejudged')
    monkeypatch.setattr(online_v2_eval, 'evaluate_episode', forbidden_judge)
    asyncio.run(runner.execute(args, root, selected, ['unit']))
    assert not calls
    assert not list(shared.parent.glob('episode_failed_*.json'))
    for job in jobs:
        assert runner.read_json(runner.job_path(root, job) / 'episode.json') == original
        assert runner.read_json(runner.job_path(root, job) / 'job.json')['attempt'] == 1
