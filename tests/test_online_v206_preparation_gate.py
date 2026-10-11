"""Coverage, recovery and cache integrity without external sites or API calls."""
import asyncio
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
import run_online_v2 as runner
import prepare_online_v2 as preparation
from eventarena.preparation_gate import coverage_report, enforce_coverage, PreparationCoverageError
from test_online_v2_engine import case


def test_gate_uses_case_coverage_in_each_experiment_not_total_job_count():
    singles = [{'case_id': f'S{i}'} for i in range(10)]
    controls = [{'case_id': 'C1'}, {'case_id': 'C2'}]
    selected = {'main': singles, 'ablation': singles, 'counterfactual': controls,
                'baseline': [dict(c, original_case_id=c['case_id'], case_id=c['case_id'] + '_AONLY') for c in singles]}
    gates = {(c['case_id'], 0): {'eligible': i != 9, 'reason': 'reset_verified' if i != 9 else 'website_unavailable'}
             for i, c in enumerate(singles)}
    gates.update({(c['case_id'], 0): {'eligible': False, 'reason': 'controlled_group_preparation_incomplete'} for c in controls})
    report = coverage_report(selected, gates, 1)
    assert report['experiments']['main']['coverage'] == 0.9
    assert report['experiments']['ablation']['registered_case_repeats'] == 10
    assert report['experiments']['baseline']['eligible'] == 9
    assert report['failed_experiments'] == ['counterfactual']
    with pytest.raises(PreparationCoverageError):
        enforce_coverage(report)


def test_gate_is_independent_of_gold_and_requires_at_least_one_eligible_case():
    a = {'main': [{'case_id': 'X', 'gold': {'decision': 'IGNORE'}}]}
    b = {'main': [{'case_id': 'X', 'gold': {'decision': 'INTERRUPT'}}]}
    assert coverage_report(a, {}, 1) == coverage_report(b, {}, 1)
    assert coverage_report(a, {}, 1, minimum=0)['passed'] is False
    assert coverage_report(a, {('X', 0): {'eligible': True}}, 1)['passed'] is True


def test_low_coverage_stops_before_tested_actors_without_scored_results(tmp_path, monkeypatch):
    from eventarena import providers
    cases = [dict(case(), case_id=f'X{i}') for i in range(10)]
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'single_event.jsonl').write_text(''.join(json.dumps(c) + '\n' for c in cases))
    root = tmp_path / 'run'
    args = runner.parser().parse_args(['run', '--run-root', str(root), '--data-dir', str(data), '--only', 'unit'])
    args.experiments = ['main']
    args.prep_model = 'unit'
    args.judge_model = None
    selected = {'main': cases}
    jobs = runner.make_jobs(selected, ['unit'], 1, 1)
    runner.write_json(root / 'manifest.json', {'jobs': jobs})
    runner.write_json(root / 'preparation_state.json', {'state': 'finished'})

    class UnusedModel:
        usage = []

        def __init__(self, *args):
            pass

        def call(self, *args):
            pytest.fail('A failed gate cannot sample a tested model')

    async def frozen_gates(*args):
        return {(c['case_id'], 0): {'eligible': i == 0, 'reason': 'reset_verified' if i == 0 else 'reset_audit_error'}
                for i, c in enumerate(cases)}

    monkeypatch.setattr(providers, 'Model', UnusedModel)
    monkeypatch.setattr(preparation, 'prepare_pool', frozen_gates)
    with pytest.raises(PreparationCoverageError):
        asyncio.run(runner.execute(args, root, selected, ['unit']))
    assert runner.read_json(root / 'preparation_gate.json')['passed'] is False
    assert runner.status(root)['phase'] == 'blocked_preparation'
    assert not list(root.glob('main/**/episode.json'))
    assert not list(root.glob('main/**/metric_record.json'))


def test_recovery_archives_ready_but_failed_reset_and_preserves_qualified_capture(tmp_path):
    root = tmp_path / 'run'
    for key in ('BAD', 'GOOD', 'PREPFAIL'):
        runner.write_json(root / 'checkpoints' / key / 'episode.json',
                          {'status': 'checkpoint_ready' if key != 'PREPFAIL' else 'browser_error'})
    for cid, key, good in [('X', 'BAD', False), ('Y', 'GOOD', True), ('Z', 'PREPFAIL', False)]:
        runner.write_json(root / 'preparation_eligibility/repeat_0' / cid / 'eligibility.json',
                          {'case_id': cid, 'repeat': 0, 'checkpoint_key': key, 'eligible': good})
    old = (root / 'checkpoints/GOOD/episode.json').read_bytes()
    rows = runner.recover_failed_checkpoints(root)
    assert {r['checkpoint_key'] for r in rows} == {'BAD', 'PREPFAIL'}
    assert (root / 'checkpoints/GOOD/episode.json').read_bytes() == old
    assert not (root / 'checkpoints/BAD').exists()
    assert len(list((root / 'checkpoints').glob('BAD_failed_*'))) == 1
    assert runner.recover_failed_checkpoints(root) == []


def test_cache_identity_and_content_mismatch_stop_before_model_calls(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    c = {'case_id': 'X'}
    (data / 'single_event.jsonl').write_text(json.dumps(c) + '\n')
    root = tmp_path / 'run'
    file = root / 'preparation_eligibility/repeat_0/X/eligibility.json'
    args = SimpleNamespace(data_dir=data, repeats=1, prep_model='unused', judge_model=None, retry_infra=False)

    async def never(*args):
        pytest.fail('Corrupt caches must not trigger model calls')

    for cached, message in [({'case_id': 'OTHER', 'repeat': 0}, 'identity'),
                            ({'case_id': 'X', 'repeat': 0, 'case_sha256': 'wrong'}, 'contents')]:
        runner.write_json(file, cached)
        with pytest.raises(ValueError, match=message):
            asyncio.run(preparation.prepare_pool(args, root, {'main': [c]}, {}, {}, never))
