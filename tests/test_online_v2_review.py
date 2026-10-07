import csv
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_v2_test',ROOT/'scripts/review_online_v2.py')
review = importlib.util.module_from_spec(spec); spec.loader.exec_module(review)


def test_blinded_review_has_no_gold_or_model_prediction_columns(tmp_path):
    output = tmp_path/'review.csv'
    review.export_form(ROOT/'data/online_v2',output,'R1')
    rows = list(csv.DictReader(output.open(encoding='utf-8-sig')))
    assert len(rows) == 240
    assert all(row['decision'] == row['follow_up'] == '' for row in rows)
    assert not any('gold' in key.lower() or 'model_prediction' in key for key in rows[0])


def test_blank_forms_never_mark_human_review_complete(tmp_path):
    a, b = tmp_path/'a.csv', tmp_path/'b.csv'
    review.export_form(ROOT/'data/online_v2',a,'R1')
    review.export_form(ROOT/'data/online_v2',b,'R2')
    output = tmp_path/'reviewed'
    review.apply_reviews(ROOT/'data/online_v2',[a,b],output)
    assert json.loads((output/'manifest.json').read_text())['human_reviewed'] is False
    assert all(c['annotation']['human_reviewed'] is False for c in review.load_dataset(output)['single_event.jsonl'])


def test_same_reviewer_cannot_be_counted_twice(tmp_path):
    path = tmp_path/'review.csv'
    review.export_form(ROOT/'data/online_v2',path,'R1')
    with pytest.raises(ValueError,match='same reviewer'):
        review.apply_reviews(ROOT/'data/online_v2',[path,path],tmp_path/'reviewed')
