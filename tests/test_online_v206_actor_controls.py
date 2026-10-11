"""Readable, complete actor observations and strict recorded control binding."""
import asyncio
import copy
import json

import pytest

from eventarena import online_v2_engine as engine_module
from eventarena.online_v2_engine import (
    ACTOR_REPRESENTATION_POLICY, AutonomousEpisode, _PayloadReferences,
    _replay_control, _resolve_payload_value, actor_payload, decode_actor_payload,
    decode_lossless_trajectory, lossless_trajectory, replay_checkpoint_audit,
    share_actor_payload,
)
from test_online_v2_engine import FakeBrowser, Model, case


def controls():
    return [
        {'node_id': '0', 'id': 'volatile-1', 'tag': 'a', 'text': 'Watch',
         'href': '/watch/', 'value': '', 'disabled': False},
        {'node_id': '1', 'id': 'volatile-2', 'tag': 'a', 'text': 'Other',
         'href': '/other/', 'value': '', 'disabled': False},
        {'node_id': '2', 'id': 'volatile-3', 'tag': 'a', 'text': 'Watch',
         'href': '/watch/', 'value': '', 'disabled': False},
    ]


def test_legacy_duplicates_bind_original_ordinal_only_for_exact_entire_sequence():
    old = controls()
    new = copy.deepcopy(old)
    for i, control in enumerate(new):
        control.update(node_id=str(80 + i), id=f'new-{i}', dom_path=f'body>a:{i}')
    assert _replay_control({'candidates': old}, {'candidates': new}, '2') == new[2]
    assert _replay_control({'candidates': old}, {'candidates': new}, '0') == new[0]


@pytest.mark.parametrize('mutation', ['order', 'text', 'value', 'disabled', 'extra'])
def test_changed_whole_sequence_never_guesses_a_duplicate(mutation):
    old, new = controls(), controls()
    if mutation == 'order':
        new[0], new[1] = new[1], new[0]
    elif mutation == 'extra':
        new.append({'node_id': '99', 'text': 'extra'})
    else:
        new[1][mutation] = True if mutation == 'disabled' else 'changed'
    with pytest.raises(ValueError, match='uniquely replayed'):
        _replay_control({'candidates': old}, {'candidates': new}, '2')


def test_recorded_dom_path_and_complete_semantics_must_both_identify_one_control():
    old, new = controls(), controls()
    for i, control in enumerate(old):
        control['dom_path'] = f'html>body>a:nth-of-type({i + 1})'
    for i, control in enumerate(new):
        control.update(dom_path=old[i]['dom_path'], node_id=str(100 + i))
    assert _replay_control({'candidates': old}, {'candidates': new}, '2')['node_id'] == '102'
    # Another identically labelled Watch cannot replace a missing recorded path.
    new[2]['dom_path'] = 'html>body>div>a:nth-of-type(3)'
    with pytest.raises(ValueError, match='DOM path'):
        _replay_control({'candidates': old}, {'candidates': new}, '2')
    new[2]['dom_path'] = old[2]['dom_path']
    new[2]['value'] = 'different control state'
    with pytest.raises(ValueError, match='DOM path'):
        _replay_control({'candidates': old}, {'candidates': new}, '2')


def test_duplicate_or_missing_recorded_node_id_never_chooses_first_match():
    old = controls()
    old[1]['node_id'] = '0'
    with pytest.raises(ValueError, match='ambiguous'):
        _replay_control({'candidates': old}, {'candidates': controls()}, '0')
    with pytest.raises(ValueError, match='unavailable'):
        _replay_control({'candidates': old}, {'candidates': controls()}, '404')


@pytest.mark.parametrize('field,value', [('text', 'different label'), ('value', 'new input'),
                                         ('disabled', True), ('href', '/different/')])
def test_locator_metadata_does_not_mask_content_or_control_state_changes(field, value):
    original = {'url': 'https://example.test/', 'text': 'Full evidence', 'candidates': controls()}
    replayed = copy.deepcopy(original)
    for i, control in enumerate(replayed['candidates']):
        control.update(dom_path=f'body>a:{i}', node_id=str(i + 5), id=f'new-{i}')
    assert replay_checkpoint_audit(original, replayed)['matches'] is True
    replayed['candidates'][0][field] = value
    assert replay_checkpoint_audit(original, replayed)['matches'] is False


def test_current_capture_is_plain_and_exact_with_only_redundant_audit_projection_shared():
    current = {'text': 'Complete 🙂 current evidence\n' * 500,
               'url': 'https://example.test/', 'title': 'Current', 'candidates': controls(),
               'index': 1, 'action_index': 1, 'task_id': 'A', 'provenance': 'live_browser_observation'}
    current['checkpoint_projection'] = {'available': True, 'content': [
        {'selector': 'main', 'text': current['text'], 'controls': controls()}]}
    history = {'observations': [copy.deepcopy(current)], 'actions': []}
    content = {'observation': current, 'full_trajectory': lossless_trajectory(history)}
    before = copy.deepcopy(content)
    payload = share_actor_payload(content, readable_current=True)
    assert ACTOR_REPRESENTATION_POLICY == 'readable_current_and_lossless_shared_history_v2'
    assert payload['payload_encoding'] == 'lossless_actor_shared_v2'
    assert payload['observation']['text'] == current['text']
    assert payload['observation']['candidates'] == current['candidates']
    assert all(isinstance(c['node_id'], str) for c in payload['observation']['candidates'])
    assert 'state_ref' not in payload['observation']
    recovered = decode_actor_payload(payload)
    assert recovered == {'observation': current, 'full_trajectory': history}
    assert decode_lossless_trajectory(payload['full_trajectory']) == history
    assert content == before


def test_multi_edit_strings_are_exact_unicode_and_references_have_no_patch_chains():
    first = ''.join(f'Row {i}: Complete 🙂 evidence and value {i}\n' for i in range(1000))
    second = first.replace('value 2\n', 'value 9\n').replace('value 700\n', 'value 703\n')
    third = second.replace('value 900\n', 'value 907\n')
    pool = _PayloadReferences()
    for text in [first, second, third]:
        ref = pool.nested(text)
        assert _resolve_payload_value({'values': pool.values}, ref) == text
    patches = [v for v in pool.values.values() if v[0] == 'text_patch']
    assert patches
    for patch in patches:
        assert pool.values[patch[1]['$ref']][0] == 'string'
    assert len(json.dumps(pool.values, ensure_ascii=False)) < len(first + second + third) / 2


@pytest.mark.parametrize('edit', [[-1, 1, 'x'], [0, True, 'x'], [0, 99, 'x'], [0, 1]])
def test_invalid_exact_text_patch_never_fabricates_evidence(edit):
    pool = {'values': {'base': ['string', '🙂完整正文'], 'patch': ['text_patch', {'$ref': 'base'}, [edit]]}}
    with pytest.raises(ValueError, match='text patch'):
        _resolve_payload_value(pool, {'$ref': 'patch'})


def test_schema_rows_preserve_nulls_empty_lists_and_source_literal_references():
    original = {'tag': 'a', 'text': 'Watch', 'role': None, 'name': '',
                'disabled': False, 'options': [], 'source_literal': {'$ref': 'not-a-pool-key'}}
    pool = _PayloadReferences()
    reference = pool.nested(original)
    assert pool.values[reference['$ref']][0] == 'row'
    assert _resolve_payload_value({'values': pool.values}, reference) == original


def _engine(tmp_path):
    obj = AutonomousEpisode(case('HANDLE'), Model([]), tmp_path)
    browser = FakeBrowser(tmp_path)
    obj.pages['A'] = browser.page
    obj.tasks['E1'] = {'goal': 'Find beta', 'url': 'https://example.test/b', 'goal_version': 0,
                       'completed': False, 'stopped': False, 'completion_index': None}
    obj.episode['events']['E1']['delivered'] = True
    return obj, browser


def test_switch_feedback_is_factual_no_op_and_never_navigates_or_completes_task(tmp_path):
    obj, browser = _engine(tmp_path)
    first = asyncio.run(obj.execute(browser, {'op': 'SWITCH_TASK', 'task_id': 'E1'}))
    assert first['tool_result']['new_tab'] is True
    assert first['tool_result']['navigation_performed'] is False
    assert first['after']['url'] == 'about:blank'
    second = asyncio.run(obj.execute(browser, {'op': 'SWITCH_TASK', 'task_id': 'E1'}))
    assert second['tool_result']['code'] == 'already_active'
    assert second['after']['url'] == 'about:blank'
    assert obj.tasks['E1']['completed'] is False
    assert obj.episode['events']['E1']['completion_index'] is None
    payload = actor_payload(obj, second['after'])
    assert payload['tool_feedback']['result']['code'] == 'already_active'
    assert payload['observation']['url'] == 'about:blank'


def test_invalid_source_explains_error_but_is_not_saved_or_counted_as_completed(tmp_path):
    obj, browser = _engine(tmp_path)
    browser.page.url, browser.page.value = 'https://example.test/a', 'alpha beta'
    invalid = {'op': 'SAVE_RESULT', 'text': 'alpha', 'sources': [
        {'url': browser.page.url, 'quote': 'alpha ... beta'}]}
    row = asyncio.run(obj.execute(browser, invalid))
    assert row['error'] == 'ValueError'
    assert row['tool_result']['code'] == 'source_not_observed'
    assert 'No result was saved' in row['tool_result']['message']
    assert obj.episode['task_outputs'] == {}
    assert obj.tasks['A']['completed'] is False
    # Literal whitespace differences remain allowed; invented punctuation does not.
    valid = {**invalid, 'sources': [{'url': browser.page.url, 'quote': 'alpha\n beta'}]}
    saved = asyncio.run(obj.execute(browser, valid))
    assert saved['error'] is None and saved['tool_result']['code'] == 'result_saved'
    assert obj.tasks['A']['completed'] is False


def test_unknown_browser_exception_message_is_not_leaked_as_tool_feedback(tmp_path):
    obj, browser = _engine(tmp_path)
    async def fail(*_):
        raise ValueError('PRIVATE_TRANSPORT_DETAILS_AND_SECRET')
    browser.execute = fail
    row = asyncio.run(obj.execute(browser, {'op': 'CLICK', 'node_id': '1'}))
    assert row['error'] == 'ValueError'
    assert 'tool_result' not in row
    assert 'PRIVATE_TRANSPORT' not in json.dumps(obj.episode)
