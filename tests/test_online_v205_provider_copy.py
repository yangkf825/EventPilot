"""Real provider response types in persisted State pairs; no paid API calls."""
import asyncio
import copy
import json
import pickle

import pytest

from eventarena.providers import ProviderText
from eventarena.online_v2_engine import prepare_state_pair
from test_online_v2_engine import FakeBrowser, Model, case, finish, verification


def test_deepcopy_response_preserves_text_and_isolates_nested_diagnostics():
    response = ProviderText('{"op":"ANSWER"}', {'finish_reason': 'stop', 'nested': {'items': [1]}})
    episode = {'responses': [{'raw': response, 'provider_diagnostics': response.diagnostics}]}
    cloned = copy.deepcopy(episode)
    raw = cloned['responses'][0]['raw']
    assert type(raw) is ProviderText and raw == response
    assert raw.diagnostics == response.diagnostics
    assert raw.diagnostics is cloned['responses'][0]['provider_diagnostics']
    raw.diagnostics['nested']['items'].append(2)
    assert response.diagnostics['nested']['items'] == [1]
    assert json.loads(json.dumps(cloned))['responses'][0]['raw'] == str(response)


def test_shallow_copy_preserves_response_metadata():
    response = ProviderText('IGNORE', {'finish_reason': 'stop'})
    cloned = copy.copy(response)
    assert type(cloned) is ProviderText and cloned == response
    assert cloned.diagnostics is response.diagnostics


@pytest.mark.parametrize('protocol', range(pickle.HIGHEST_PROTOCOL + 1))
def test_response_serialization_roundtrip_preserves_diagnostics(protocol):
    response = ProviderText('DEFER', {'finish_reason': 'length', 'nested': [3]})
    cloned = pickle.loads(pickle.dumps(response, protocol=protocol))
    assert type(cloned) is ProviderText and cloned == response
    assert cloned.diagnostics == response.diagnostics
    assert cloned.diagnostics is not response.diagnostics


def pair_cases():
    unfinished = case('HANDLE')
    unfinished.update(case_id='COPY_UNFINISHED', group_type='state')
    completed = copy.deepcopy(unfinished)
    completed.update(case_id='COPY_COMPLETED', gold={'decision': 'IGNORE', 'follow_up': None},
                     setup_tasks=[{'id': 'PRIOR_B', 'goal': 'Find beta', 'url': 'https://example.test/b',
                                   'verification': verification('beta'), 'alias_event_id': 'E1'}])
    return [completed, unfinished]


def response_model():
    class DiagnosticModel(Model):
        def call(self, messages):
            raw = super().call(messages)
            return ProviderText(raw, {'finish_reason': 'stop', 'content_chars': len(raw),
                                      'nested': {'fixture': True}})
    return DiagnosticModel([{'op': 'TYPE', 'node_id': '0', 'value': 'alpha'},
                            {'op': 'TYPE', 'node_id': '0', 'value': 'beta'},
                            *finish('beta', 'https://example.test/b')])


def test_successful_state_pair_copies_actual_response_type_and_persists(tmp_path):
    result = asyncio.run(prepare_state_pair(pair_cases(), response_model(), tmp_path,
                                           browser_factory=FakeBrowser))
    assert result['status'] == 'checkpoint_ready'
    assert set(result['checkpoints']) == {'COPY_COMPLETED', 'COPY_UNFINISHED'}
    assert result['checkpoints']['COPY_COMPLETED']['state']['prior_results']['E1']['verification']['success'] is True
    assert result['checkpoints']['COPY_UNFINISHED']['state']['prior_results'] == {}
    raw = result['preparation_episode']['responses'][0]['raw']
    assert type(raw) is ProviderText and raw.diagnostics['finish_reason'] == 'stop'
    saved = json.loads((tmp_path / 'state_pair.json').read_text())
    assert saved['status'] == 'checkpoint_ready'
    assert saved['preparation_episode']['responses'][0]['raw'] == str(raw)


def test_state_pair_cleanup_error_keeps_original_failure_and_persists(tmp_path):
    class FailingBrowser(FakeBrowser):
        async def __aexit__(self, *args):
            raise RuntimeError('local fixture cleanup failure')
    result = asyncio.run(prepare_state_pair(pair_cases(), response_model(), tmp_path,
                                           browser_factory=FailingBrowser))
    assert result['status'] == 'browser_error'
    assert result['error'] == {'category': 'RuntimeError', 'phase': 'browser_close'}
    assert result['checkpoints'] == {} and result['partial_checkpoints']
    saved = json.loads((tmp_path / 'state_pair.json').read_text())
    assert saved['error']['category'] == 'RuntimeError'
    assert saved['preparation_episode']['responses']
    assert not (tmp_path / 'checkpoint_case.json').exists()


def test_state_pair_cancellation_keeps_interrupted_record_instead_of_copy_error(tmp_path):
    class CancelledBrowser(FakeBrowser):
        async def __aexit__(self, *args):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(prepare_state_pair(pair_cases(), response_model(), tmp_path,
                                       browser_factory=CancelledBrowser))
    saved = json.loads((tmp_path / 'state_pair.json').read_text())
    assert saved['status'] == 'interrupted' and saved['checkpoints'] == {}
    assert saved['preparation_episode']['responses']
