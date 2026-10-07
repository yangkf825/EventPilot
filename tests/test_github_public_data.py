"""Published source package checks that need neither source downloads nor APIs."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_data_checks', ROOT / 'scripts/check_public_data.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_public_candidate_integrity_and_source_registry():
    report = checker.check(ROOT)
    assert report['registered_source_tasks'] == 54
    assert report['datasets']['single_event.jsonl'] == {'cases': 100, 'events': 100}
    assert report['datasets']['counterfactual.jsonl'] == {'cases': 70, 'events': 70}
    assert report['datasets']['multi_event.jsonl'] == {'cases': 20, 'events': 70}


@pytest.mark.parametrize('field', sorted(checker.FORBIDDEN_FIELDS))
def test_private_fields_are_rejected_at_any_nested_location(field):
    with pytest.raises(ValueError, match='Private source/runtime field'):
        checker.check_projection({'events': [{'source': {field: 'must not be published'}}]})


def test_authored_text_and_hashed_source_ids_are_permitted():
    checker.check_projection({'goal': 'Authored adaptation', 'source_task_a': {'task_id': 'id'},
                              'events': [{'text': 'Authored event', 'gold': {'decision': 'IGNORE'}}]})
