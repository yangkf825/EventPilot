from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

def pytest_collection_modifyitems(items):
    ready = all((ROOT/'data/online_v2'/name).is_file() for name in
                ('single_event.jsonl','counterfactual.jsonl','multi_event.jsonl','source_task_index.jsonl'))
    if ready:
        return
    for item in items:
        name = Path(str(item.fspath)).name
        needs_source = name in {'test_online_v2_data.py','test_online_v2_review.py'}
        needs_source |= name == 'test_online_v204_data.py' and item.originalname != 'test_identifier_permutation_does_not_consult_labels_and_keeps_all_references'
        if needs_source:
            item.add_marker(pytest.mark.skip(reason='Official source data is not bundled; run python scripts/prepare_dataset.py --download --extract locally.'))
