"""Lossless actor inputs and factual checkpoint failure regressions; no API calls."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from eventarena.browser import checkpoint_policy_hash
from eventarena.online_v2_engine import (
    AutonomousEpisode, actor_payload, context_for_view, decode_actor_payload,
    decode_lossless_trajectory, lossless_trajectory, prepare_checkpoint,
    prepare_state_pair, replay_checkpoint_audit, run_checkpoint_episode,
    share_actor_payload,
)
from test_online_v2_engine import FakeBrowser, Model, case, finish, verification


def _history(text, start=0):
    state = {'url': 'https://example.test/a', 'title': 'Evidence', 'text': text,
             'candidates': [{'node_id': '1', 'id': 'dynamic', 'tag': 'input',
                             'name': 'query', 'value': 'alpha', 'options': []}],
             'literal_reference': {'$ref': 'original-source-literal'}}
    observation = {**copy.deepcopy(state), 'index': start, 'action_index': start,
                   'task_id': 'A', 'provenance': 'live_browser_observation'}
    action = {'index': start, 'task_id': 'A', 'action': {'op': 'SAVE_RESULT', 'text': text,
              'sources': [{'url': state['url'], 'quote': text}]}, 'before': state,
              'after': copy.deepcopy(state), 'error': None, 'source_reserved_fields': {'original': text}}
    return {'observations': [observation], 'actions': [action]}


def test_actor_components_share_complete_evidence_and_recover_exact_values():
    text = '全文证据 Full observation\n' * 5000
    before, after = _history(text), _history(text, 1)
    content = {'original_user_task': 'Find alpha',
               'observation': copy.deepcopy(after['observations'][0]),
               'workspace': {'saved_output': text, 'literal': {'$ref': 'literal-untrusted-value'}},
               'full_trajectory': lossless_trajectory(after),
               'event_boundary_information': {'current_state': {'summary': text, 'candidates': before['observations'][0]['candidates']},
                                               'full_trajectory': lossless_trajectory(before)}}
    original = copy.deepcopy(content)
    payload = share_actor_payload(content)
    decoded = decode_actor_payload(payload)
    expected = copy.deepcopy(content)
    expected['full_trajectory'] = after
    expected['event_boundary_information']['full_trajectory'] = before
    assert decoded == expected
    assert content == original
    assert decode_lossless_trajectory(payload['full_trajectory']) == after
    assert decode_lossless_trajectory(payload['event_boundary_information']['full_trajectory'], payload['full_trajectory']) == before
    wire = json.dumps(payload, ensure_ascii=False)
    assert wire.count(json.dumps(text, ensure_ascii=False)[1:-1]) == 1
    assert len(wire) < len(json.dumps(content, ensure_ascii=False)) / 4
    assert set(payload['observation']) == {'state_ref', 'observation_index', 'metadata'}
    decoded['workspace']['saved_output'] = 'mutated'
    assert decode_actor_payload(payload) == expected


@pytest.mark.parametrize('view', ['event-only', 'goal', 'state', 'trajectory'])
def test_shared_pool_obeys_pre_event_information_permissions(tmp_path, view):
    old = _history('SECRET_PRE_EVENT_ACTION_AND_TEXT' * 100, 0)
    new = _history('PUBLIC_CURRENT_OBSERVATION' * 100, 1)
    checkpoint = {'goal': 'Find alpha', 'rules': [], 'state': {'summary': 'STATE_ONLY', 'prior_results': {}},
                  'trajectory': lossless_trajectory(old)}
    engine = AutonomousEpisode(case(), Model([]), tmp_path,
                               {'execution_prefix': context_for_view(checkpoint, view)})
    engine.episode.update(actions=old['actions'] + new['actions'],
                          observations=old['observations'] + new['observations'], controlled_action_boundary=1)
    payload = actor_payload(engine, new['observations'][0])
    decoded = decode_actor_payload(payload)
    boundary = decoded['event_boundary_information']
    assert ('full_trajectory' in boundary) == (view == 'trajectory')
    assert ('current_state' in boundary) == (view in {'state', 'trajectory'})
    assert decoded['full_trajectory'] == new
    assert ('SECRET_PRE_EVENT_ACTION_AND_TEXT' in json.dumps(payload)) == (view == 'trajectory')
    assert decoded['observation'] == new['observations'][0]


def test_corrupt_payload_references_fail_explicitly():
    episode = _history('Long observation evidence ' * 300)
    payload = share_actor_payload({'observation': episode['observations'][0], 'full_trajectory': lossless_trajectory(episode)})
    payload['full_trajectory']['values'].clear()
    with pytest.raises((ValueError, KeyError), match='reference|V'):
        decode_actor_payload(payload)
    with pytest.raises(ValueError, match='Missing shared lossless trajectory pool'):
        decode_lossless_trajectory({'encoding': 'lossless_shared_references_v4', 'pool_path': 'full_trajectory'})


POLICY = {'mode': 'task_content_v1', 'id': 'official_main_content_v1',
          'content_selectors': ['main', 'article'], 'exclude_selectors': ['header', 'nav', '#cookie']}


def _projected(text='Full task content: 16 degrees', value='16'):
    projection = {'available': True, 'policy_id': POLICY['id'], 'policy_sha256': checkpoint_policy_hash(POLICY),
                  'url': 'https://example.test/a', 'title': 'Forecast',
                  'content': [{'selector': 'main', 'text': text,
                               'controls': [{'node_id': '0', 'id': 'volatile', 'tag': 'input', 'value': value, 'disabled': False}]}]}
    return {'url': projection['url'], 'text': 'irrelevant navigation and cookie banner', 'candidates': [],
            'checkpoint_projection': projection}


def test_registered_projection_ignores_ui_and_ids_but_keeps_all_task_values():
    expected = _projected()
    observed = copy.deepcopy(expected)
    observed['text'] = 'different header and cookie banner'
    observed['checkpoint_projection']['content'][0]['controls'][0].update(node_id='9', id='changed')
    assert replay_checkpoint_audit(expected, observed, policy=POLICY)['matches'] is True
    assert replay_checkpoint_audit(expected, observed)['matches'] is False
    observed['checkpoint_projection']['content'][0]['text'] = 'Full task content: 13 degrees'
    audit = replay_checkpoint_audit(expected, observed, policy=POLICY)
    assert audit['matches'] is False and audit['available'] is True
    observed = copy.deepcopy(expected)
    observed['checkpoint_projection']['content'][0]['controls'][0]['value'] = '13'
    assert replay_checkpoint_audit(expected, observed, policy=POLICY)['matches'] is False
    observed = copy.deepcopy(expected)
    observed['checkpoint_projection']['content'][0]['controls'][0]['disabled'] = True
    assert replay_checkpoint_audit(expected, observed, policy=POLICY)['matches'] is False


@pytest.mark.parametrize('mutation', ['missing', 'unavailable', 'hash'])
def test_registered_projection_unknown_never_passes(mutation):
    expected, observed = _projected(), _projected()
    if mutation == 'missing':
        observed.pop('checkpoint_projection')
    elif mutation == 'unavailable':
        observed['checkpoint_projection'].update(available=False, reason='registered_content_root_unavailable')
    else:
        observed['checkpoint_projection']['policy_sha256'] = 'other-policy'
    audit = replay_checkpoint_audit(expected, observed, policy=POLICY)
    assert audit['matches'] is False and audit['available'] is False
    # Presence-only anchors cannot declare 16 -> 13 equivalent.
    old = {'url': '/a', 'text': 'Forecast 16 degrees', 'candidates': []}
    new = {**old, 'text': 'Forecast 13 degrees'}
    assert not replay_checkpoint_audit(old, new, {'text_anchors': ['Forecast']})['matches']


def _prepared(tmp_path):
    return asyncio.run(prepare_checkpoint(case(), Model([{'op': 'TYPE', 'node_id': '0', 'value': 'alpha'}]),
                                           tmp_path, browser_factory=FakeBrowser))['checkpoint']


def test_execution_exception_after_successful_replay_is_not_a_replay_mismatch(tmp_path, monkeypatch):
    checkpoint = _prepared(tmp_path / 'prepare')
    async def explode(self, browser):
        raise RuntimeError('fixture execution error')
    monkeypatch.setattr(AutonomousEpisode, 'continue_episode', explode)
    model = Model([{'decision': 'IGNORE'}])
    episode = asyncio.run(run_checkpoint_episode(case(), model, checkpoint, 'goal', tmp_path / 'run', browser_factory=FakeBrowser))
    assert episode['checkpoint_replay_audit']['matches'] is True
    assert episode['status'] == 'browser_error'
    assert episode['error'] == {'category': 'RuntimeError', 'phase': 'execution'}
    assert len(episode['responses']) == 1
    assert 'replay_error_type' not in episode


def test_missing_network_capture_prevents_all_model_calls(tmp_path):
    checkpoint = _prepared(tmp_path / 'prepare')
    checkpoint['environment_capture'] = {'mode': 'recorded_prefix_then_live', 'har_path': str(tmp_path / 'missing.har'), 'sha256': 'missing'}
    class CapturedBrowser(FakeBrowser):
        async def begin_checkpoint_replay(self, capture):
            assert not Path(capture['har_path']).exists()
            raise ValueError('Missing factual capture')
    model = Model([])
    episode = asyncio.run(run_checkpoint_episode(case(), model, checkpoint, 'trajectory', tmp_path / 'run', browser_factory=CapturedBrowser))
    assert episode['status'] == 'checkpoint_capture_unavailable'
    assert episode['checkpoint_replay_audit']['available'] is False
    assert model.messages == []


def _pair():
    incomplete = case('HANDLE')
    incomplete['case_id'] = 'UNFINISHED_B'
    complete = copy.deepcopy(incomplete)
    complete.update(case_id='COMPLETED_B', setup_tasks=[{'id': 'PRIOR_B', 'goal': 'Find beta',
                    'url': 'https://example.test/b', 'verification': verification('beta'), 'alias_event_id': 'E1'}])
    return [complete, incomplete]


@pytest.mark.parametrize('return_value', [False, None, {'success': None}])
def test_state_pair_failed_or_unknown_warmup_never_restores_or_publishes_ready(tmp_path, monkeypatch, return_value):
    original = AutonomousEpisode.warmup
    async def guarded(self, browser):
        if not self.case.get('setup_tasks'):
            return await original(self, browser)
        self.episode.update(status='checkpoint_setup_failed', preparation_failure_status='output_truncated', preparation_failure_is_model=True)
        return return_value
    monkeypatch.setattr(AutonomousEpisode, 'warmup', guarded)
    result = asyncio.run(prepare_state_pair(_pair(), Model([{'op': 'TYPE', 'node_id': '0', 'value': 'alpha'}]),
                                            tmp_path, browser_factory=FakeBrowser))
    assert result['status'] == 'checkpoint_setup_failed'
    assert result['preparation_failure_status'] == 'output_truncated'
    assert result['checkpoints'] == {}
    assert set(result['partial_checkpoints']) == {'UNFINISHED_B'}
    assert 'restoration_audit' not in result
    assert not (tmp_path / 'checkpoint_case.json').exists()
    persisted = json.loads((tmp_path / 'episode.json').read_text())
    assert persisted['status'] != 'checkpoint_ready' and not persisted['state_pair_ready']
    assert persisted['prior_results'] == {}


def test_state_pair_true_warmup_without_verified_b_is_unknown(tmp_path, monkeypatch):
    original = AutonomousEpisode.warmup
    async def fake_true(self, browser):
        if not self.case.get('setup_tasks'):
            return await original(self, browser)
        return True
    monkeypatch.setattr(AutonomousEpisode, 'warmup', fake_true)
    result = asyncio.run(prepare_state_pair(_pair(), Model([{'op': 'TYPE', 'node_id': '0', 'value': 'alpha'}]),
                                            tmp_path, browser_factory=FakeBrowser))
    assert result['status'] == 'checkpoint_setup_unverified'
    assert result['checkpoints'] == {}
    assert result['preparation_episode']['prior_results'] == {}


def test_completed_state_pair_binds_same_archive_only_after_context_close(tmp_path):
    class ArchiveBrowser(FakeBrowser):
        def __init__(self, run_dir, headless=False):
            super().__init__(run_dir, headless)
            self.run_dir = Path(run_dir)
        async def __aexit__(self, *exc):
            (self.run_dir / 'network.har').write_bytes(b'factual-browser-fixture-archive')
    model = Model([{'op': 'TYPE', 'node_id': '0', 'value': 'alpha'},
                   {'op': 'TYPE', 'node_id': '0', 'value': 'beta'}, *finish('beta', 'https://example.test/b')])
    result = asyncio.run(prepare_state_pair(_pair(), model, tmp_path, browser_factory=ArchiveBrowser))
    assert result['status'] == 'checkpoint_ready'
    captures = [cp['environment_capture'] for cp in result['checkpoints'].values()]
    assert captures[0] == captures[1]
    assert captures[0]['sha256'] == hashlib.sha256((tmp_path / 'network.har').read_bytes()).hexdigest()
    complete = result['checkpoints']['COMPLETED_B']
    assert complete['state']['prior_results']['E1']['verification']['success'] is True
    assert complete['state']['prior_results']['E1']['outputs']


@pytest.mark.browser
def test_real_recorded_checkpoint_replays_factual_prefix_then_returns_to_live(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    state = {'temperature': '16'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = f'<html><title>Forecast</title><body><main>Forecast {state["temperature"]} degrees. '
            body += 'Complete task evidence and location. ' * 5 + '</main></body></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(body.encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    c = case('IGNORE')
    c['url'] = url + '/'
    c['trigger'] = {'kind': 'url_contains', 'value': '/a', 'min_actor_progress_actions': 1}
    c['checkpoint_audit_policy'] = POLICY
    try:
        cp = asyncio.run(prepare_checkpoint(c, Model([{'op': 'GOTO', 'url': url + '/a'}]), tmp_path / 'prepare', {'headless': True}))['checkpoint']
        assert cp['environment_capture']['sha256']
        assert cp['state']['checkpoint_projection']['available'] is True
        state['temperature'] = '13'
        model = Model([{'decision': 'IGNORE'}, {'op': 'GOTO', 'url': url + '/live'}, {'op': 'ANSWER', 'answer': 'done'}])
        episode = asyncio.run(run_checkpoint_episode(c, model, cp, 'state', tmp_path / 'run', {'headless': True}))
        assert episode['status'] == 'agent_finished'
        assert episode['checkpoint_replay_audit']['matches'] is True
        assert episode['checkpoint_replay_audit']['policy_sha256'] == checkpoint_policy_hash(POLICY)
        assert '13 degrees' in episode['final_observation']['text']
        assert episode['final_observation']['network_mode'] == 'live_after_checkpoint'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_completion_verifier_follows_acceptance_order_even_with_shuffled_events(tmp_path, monkeypatch):
    import eventarena.online_v2_eval as evaluation
    c = case('REPLAN')
    old = copy.deepcopy(c.pop('event'))
    old.update(id='OLD')
    old['execution'].update(updated_goal='Find older answer', updated_verification=verification('older'))
    new = copy.deepcopy(old)
    new.update(id='NEW')
    new['execution'].update(updated_goal='Find latest answer', updated_verification=verification('latest'))
    c['events'] = [new, old]
    engine = AutonomousEpisode(c, Model([]), tmp_path)
    engine.episode['events']['OLD'].update(delivered=True, effect_applied=True, accepted_index=4)
    engine.episode['events']['NEW'].update(delivered=True, effect_applied=True, accepted_index=8)
    engine.tasks['A'].update(goal='Find latest answer', goal_version=2)
    seen = []
    async def capture(goal, rules, *args, **kwargs):
        seen.append((goal, copy.deepcopy(rules)))
        return {'success': None}
    monkeypatch.setattr(evaluation, 'evaluate_task', capture)
    asyncio.run(engine.verify_completion('A', 9))
    assert seen == [('Find latest answer', verification('latest'))]


def test_content_verifier_uses_registered_goal_without_rechecking_independent_b(tmp_path, monkeypatch):
    import eventarena.online_v2_eval as evaluation
    c = case('HANDLE')
    c['goal'] = 'Find alpha; also finish the separately verified beta report'
    c['verification']['CURRENT_TASK']['goal'] = 'Find alpha'
    engine = AutonomousEpisode(c, Model([]), tmp_path)
    seen = []
    async def capture(goal, rules, *args, **kwargs):
        seen.append(goal)
        return {'success': None}
    monkeypatch.setattr(evaluation, 'evaluate_task', capture)
    asyncio.run(engine.verify_completion('A', 1))
    assert seen == ['Find alpha']
    assert engine.tasks['A']['goal'] == c['goal']
    assert engine.episode['verified_tasks']['A']['verification_goal'] == 'Find alpha'


def test_changed_long_text_uses_lossless_unicode_splices_without_omitting_values():
    original = '🙂完整正文 ' * 6000 + '\nTemperature 16 degrees\n' + '尾部正文' * 6000
    changed = original.replace('16 degrees', '13 degrees')
    before, after = _history(original), _history(changed, 1)
    source = {'observation': after['observations'][0], 'full_trajectory': lossless_trajectory(after),
              'event_boundary_information': {'full_trajectory': lossless_trajectory(before), 'current_state': {'summary': original}}}
    payload = share_actor_payload(source)
    expected = copy.deepcopy(source)
    expected['full_trajectory'] = after
    expected['event_boundary_information']['full_trajectory'] = before
    assert decode_actor_payload(payload) == expected
    assert any(v[0] == 'splice' for v in payload['full_trajectory']['values'].values())
    assert len(json.dumps(payload, ensure_ascii=False)) < len(json.dumps(source, ensure_ascii=False)) / 4
    # A corrupted splice cannot silently manufacture or shorten evidence.
    descriptor = next(v for v in payload['full_trajectory']['values'].values() if v[0] == 'splice')
    descriptor[2] = -1
    with pytest.raises(ValueError, match='Invalid lossless payload string splice'):
        decode_actor_payload(payload)


class MissingArchiveBrowser(FakeBrowser):
    environment_capture_required = True
    async def begin_checkpoint_replay(self, capture):
        raise AssertionError('Preparation must not replay')
    async def end_checkpoint_replay(self):
        pass


def test_prepare_checkpoint_missing_required_archive_cannot_publish_ready(tmp_path):
    episode = asyncio.run(prepare_checkpoint(case(), Model([{'op': 'TYPE', 'node_id': '0', 'value': 'alpha'}]),
                                             tmp_path, browser_factory=MissingArchiveBrowser))
    assert episode['status'] == 'checkpoint_capture_unavailable'
    assert episode['environment_capture_required'] is True
    assert 'checkpoint' not in episode and 'partial_checkpoint' in episode
    assert not (tmp_path / 'checkpoint_case.json').exists()
    assert json.loads((tmp_path / 'episode.json').read_text())['status'] == 'checkpoint_capture_unavailable'


def test_prepare_state_pair_missing_required_archive_invalidates_both_members(tmp_path):
    model = Model([{'op': 'TYPE', 'node_id': '0', 'value': 'alpha'},
                   {'op': 'TYPE', 'node_id': '0', 'value': 'beta'}, *finish('beta', 'https://example.test/b')])
    result = asyncio.run(prepare_state_pair(_pair(), model, tmp_path, browser_factory=MissingArchiveBrowser))
    assert result['status'] == 'checkpoint_capture_unavailable'
    assert result['checkpoints'] == {}
    assert set(result['partial_checkpoints']) == {'UNFINISHED_B', 'COMPLETED_B'}
    assert not result['preparation_episode']['state_pair_ready']
    assert not (tmp_path / 'checkpoint_case.json').exists()


def test_restore_missing_required_capture_prevents_live_fallback(tmp_path):
    checkpoint = _prepared(tmp_path / 'prepare')
    model = Model([])
    episode = asyncio.run(run_checkpoint_episode(case(), model, checkpoint, 'trajectory', tmp_path / 'run',
                                                 browser_factory=MissingArchiveBrowser))
    assert episode['status'] == 'checkpoint_capture_unavailable'
    assert episode['checkpoint_replay_audit']['reason'] == 'registered_network_capture_missing'
    assert model.messages == []
