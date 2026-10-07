"""Reversibility and field-sharing tests, independent of model outcomes."""
import copy
import json

import pytest

from eventarena.online_v2_engine import decode_lossless_trajectory, lossless_trajectory


def history_fixture():
    controls = [{'node_id': '0', 'text': 'Full option text ' * 1000, 'value': '杭州',
                 'options': [{'label': '选择', 'value': '一'}], 'checked': False}]
    first = {'url': 'https://example.test/a', 'title': 'Research',
             'text': '完整正文\n' + 'Full evidence ' * 2000,
             'candidates': controls, 'http_status': 200, 'custom': None}
    second = {**copy.deepcopy(first), 'text': first['text'] + '\nFrom $499'}
    return {'observations': [
                {**copy.deepcopy(first), 'index': 0, 'action_index': 0, 'task_id': 'A',
                 'provenance': 'actual_browser_capture'},
                {**copy.deepcopy(first), 'index': 1, 'action_index': 0, 'task_id': 'A',
                 'provenance': 'actual_browser_capture'},
                {**copy.deepcopy(second), 'index': 2, 'action_index': 1, 'task_id': 'A',
                 'provenance': 'actual_browser_capture'}],
            'actions': [{'index': 0, 'task_id': 'A', 'action': {'op': 'SCROLL', 'pixels': 700},
                         'before': copy.deepcopy(first), 'after': copy.deepcopy(second),
                         'error': None, 'custom_action_fact': {'value': True}}]}


def test_field_sharing_roundtrip_preserves_every_step_value_and_metadata():
    episode = history_fixture()
    original = copy.deepcopy(episode)
    encoded = lossless_trajectory(episode)
    assert decode_lossless_trajectory(encoded) == original
    assert episode == original
    assert len(encoded['records']) == len(episode['observations']) + len(episode['actions'])
    assert len(encoded['states']) == 2
    refs = list(encoded['states'].values())
    assert refs[0]['candidates'] == refs[1]['candidates']
    assert refs[0]['text'] != refs[1]['text']
    assert len(json.dumps(encoded, ensure_ascii=False)) < len(json.dumps(original, ensure_ascii=False))


def test_decode_and_encoding_do_not_share_mutable_values_with_source():
    episode = history_fixture()
    encoded = lossless_trajectory(episode)
    decoded = decode_lossless_trajectory(encoded)
    decoded['observations'][0]['candidates'][0]['value'] = 'changed'
    assert episode['observations'][0]['candidates'][0]['value'] == '杭州'
    assert decode_lossless_trajectory(encoded)['observations'][0]['candidates'][0]['value'] == '杭州'


def test_optional_fields_reserved_names_and_array_order_roundtrip():
    state = {'text': 'observed', 'index': 99, 'task_id': 'nested-task', 'provenance': 'nested-proof'}
    episode = {'observations': [
                   {'text': 'later', 'index': 8, 'action_index': 3},
                   {'text': 'earlier', 'index': 2, 'action_index': 1}],
               'actions': [{'index': 5, 'kind': 'source-kind', 'action_index': 19,
                            'action_order': 77, 'before_state_ref': 'original-value',
                            'source_reserved_fields': {'source': 'original'}, 'before': state},
                           {'index': 0, 'action': {'op': 'WAIT'}, 'custom': [1, False, None]}]}
    assert decode_lossless_trajectory(lossless_trajectory(episode)) == episode


def test_missing_or_unsupported_reference_fails_instead_of_dropping_evidence():
    encoded = lossless_trajectory(history_fixture())
    encoded['fields'].pop(next(iter(encoded['fields'])))
    with pytest.raises(ValueError, match='Missing lossless trajectory reference'):
        decode_lossless_trajectory(encoded)
    with pytest.raises(ValueError, match='Unsupported lossless trajectory encoding'):
        decode_lossless_trajectory({'encoding': 'unknown'})


def test_empty_history_and_absent_observation_metadata_roundtrip():
    assert decode_lossless_trajectory(lossless_trajectory({})) == {'observations': [], 'actions': []}
    episode = {'observations': [{'text': '', 'http_status': 200}], 'actions': []}
    assert decode_lossless_trajectory(lossless_trajectory(episode)) == episode


def test_source_reference_named_fields_do_not_create_absent_before_after_states():
    episode = {'observations': [], 'actions': [{'index': 0, 'action': {'op': 'WAIT'},
                'before_state_ref': 'original source value', 'after_state_ref': 'another original value'}]}
    assert decode_lossless_trajectory(lossless_trajectory(episode)) == episode
