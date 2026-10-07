import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v2_validator_test', ROOT/'scripts/validate_online_v2.py')
validator = importlib.util.module_from_spec(spec); spec.loader.exec_module(validator)


def test_registered_candidates_are_source_valid_but_not_human_reviewed():
    result = validator.validate(ROOT/'data/online_v2')
    assert result['schema_valid'] is True and result['human_reviewed'] is False
    with pytest.raises(ValueError, match='independent human'):
        validator.validate(ROOT/'data/online_v2', strict_review=True)
    with pytest.raises(ValueError, match='execution and trigger'):
        validator.validate(ROOT/'data/online_v2', strict_readiness=True)


def test_no_shared_original_source_id_crosses_proposed_split():
    profiles = json.loads((ROOT/'data/online_v2/source_profiles.json').read_text())
    split = json.loads((ROOT/'data/online_v2/split_manifest.json').read_text())
    source_splits = {}
    for index, profile in enumerate(profiles,1):
        group = split['profile_cluster'][f'P{index:02d}']
        for key in ('a','b','c'):
            source_splits.setdefault(profile[key],set()).add(split['family_assignment'][group])
    assert all(len(values) == 1 for values in source_splits.values())


def test_event_b_is_a_different_real_source_task_for_each_author_profile():
    profiles = json.loads((ROOT/'data/online_v2/source_profiles.json').read_text())
    assert all(p['a'] != p['b'] for p in profiles)
    rows = validator.read_cases(ROOT/'data/online_v2/single_event.jsonl')
    assert sum(e['execution'].get('terminates_original') is True for c in rows for e in c['events']) == 5
    for case in rows:
        for event in case['events']:
            if event['execution']['kind'] == 'update_goal':
                assert case['verification']['CURRENT_TASK']['goal'] == case['goal']
                assert {c['origin'] for c in event['execution']['updated_verification']['effective_constraints']} == {'new','retained'}


def test_actual_event_source_is_not_assumed_to_be_nominal_b():
    originals = {r['task_id']:r for r in validator.read_cases(ROOT/'data/online_v2/source_task_index.jsonl')}
    seen_c = False
    for filename in set(validator.FILES.values()):
        for case in validator.read_cases(ROOT/'data/online_v2'/filename):
            for event in case['events']:
                if event['execution']['kind'] != 'research_task':
                    assert event['source_task'] is None
                    continue
                src = event['source_task']
                assert src['original_goal'] == originals[src['task_id']]['goal']
                assert src['task_id'] == event['execution']['source_task_id']
                assert src['adapted_goal'] == event['execution']['goal']
                if case.get('scenario_type') == 'scope':
                    assert src['task_id'] == case['source_task_c']['task_id']
                    seen_c = True
    assert seen_c
