"""Audited, copy-only resume registration; no APIs and no real websites."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import repair_providertext_resume as repair
import run_online_v2 as runner
from test_online_v2_engine import case


def registered_fixture(tmp_path):
    root = tmp_path / 'run'
    data = tmp_path / 'data'
    data.mkdir()
    cases = [dict(case(), case_id=name, task_family_id='F1') for name in ('C1', 'C2')]
    (data / 'single_event.jsonl').write_text(''.join(json.dumps(c) + '\n' for c in cases))
    model = {'name': 'unit', 'enabled': True, 'backend': 'chat-completions', 'model': 'fixture-model',
             'endpoint': 'https://example.invalid/v1/chat/completions', 'api_key_env': 'TEST_REPAIR_ENV'}
    config = tmp_path / 'models.json'
    config.write_text(json.dumps({'models': [model]}))
    args = runner.parser().parse_args(['run', '--config', str(config), '--run-root', str(root),
                                      '--data-dir', str(data), '--experiments', 'main', '--only', 'unit'])
    args.experiments = ['main']
    args.prep_model = args.judge_model = 'unit'
    selected = runner.select_cases(args)
    jobs = runner.make_jobs(selected, ['unit'], args.repeats, args.seed)
    manifest = runner.make_manifest(args, selected, ['unit'], {'unit': model}, jobs)
    manifest['code_sha256'] = repair.pre_repair_core(runner)[0]
    runner.write_json(root / 'manifest.json', manifest)
    runner.write_json(runner.job_path(root, jobs[0]) / 'job.json', {'state': 'finished', 'attempt': 1})
    runner.write_json(runner.job_path(root, jobs[0]) / 'episode.json', {'status': 'runner_error', 'error_type': 'TypeError'})
    runner.write_json(runner.job_path(root, jobs[0]) / 'metric_record.json', {'episode_success': None})
    runner.write_json(runner.job_path(root, jobs[1]) / 'job.json', {'state': 'interrupted', 'attempt': 1})
    frozen = root / 'registered_data/main.jsonl'
    frozen.parent.mkdir(parents=True)
    frozen.write_text((data / 'single_event.jsonl').read_text())
    return root, config, data, jobs


def test_repair_audit_is_read_only_and_preserves_completed_result_hashes(tmp_path):
    root, config, data, _ = registered_fixture(tmp_path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    plan = repair.plan_repair(root, config, data, runner)
    assert plan[0]['status'] == 'ready_to_apply'
    assert plan[0]['no_api_call'] and plan[0]['changed_core_files'] == ['eventarena/providers.py']
    assert plan[0]['job_states'] == {'finished': 1, 'interrupted': 1}
    assert plan[0]['preserved_result_files'] == 3
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_apply_keeps_all_old_results_and_records_exact_original_code_and_manifest(tmp_path):
    root, config, data, jobs = registered_fixture(tmp_path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    plan = repair.plan_repair(root, config, data, runner)
    report = repair.apply_repair(root, plan, runner)
    assert report['status'] == 'applied'
    for rel, raw in before.items():
        if str(rel) != 'manifest.json':
            assert (root / rel).read_bytes() == raw
    folder = root / 'repairs' / repair.BUG_ID
    assert (folder / 'manifest.before.json').read_bytes() == before[Path('manifest.json')]
    saved = runner.read_json(root / 'manifest.json')
    original = json.loads(before[Path('manifest.json')])
    assert saved == dict(original, code_sha256=runner.code_fingerprint())
    assert repair.digest((folder / 'core_before/eventarena/providers.py').read_bytes()) == repair.BEFORE_PROVIDER_SHA256
    snapshot_hash = runner.sha({str(p.relative_to(folder / 'core_before')): repair.digest(p.read_bytes())
                               for p in (folder / 'core_before').rglob('*.py')})
    assert snapshot_hash == original['code_sha256']
    proof = runner.read_json(folder / 'preserved_results_sha256.json')
    assert all(repair.digest((root / rel).read_bytes()) == sha for rel, sha in proof.items())
    assert runner.read_json(folder / 'REPAIR.json')['legacy_attempts_retained'] is True
    assert runner.read_json(runner.job_path(root, jobs[1]) / 'job.json')['state'] == 'interrupted'
    # Applying again makes no further mutations, including the immutable backup.
    all_after = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert repair.apply_repair(root, repair.plan_repair(root, config, data, runner), runner)['status'] == 'already_current'
    assert all_after == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('change', ['model', 'dataset', 'runtime', 'other_code', 'frozen_data'])
def test_repair_refuses_incompatible_runs_without_writing(change, tmp_path):
    root, config, data, _ = registered_fixture(tmp_path)
    if change == 'model':
        obj=json.loads(config.read_text()); obj['models'][0]['model']='other-model'; config.write_text(json.dumps(obj))
    elif change == 'dataset':
        rows=runner.read_cases(data / 'single_event.jsonl'); rows[0]['goal']='Changed goal'
        (data / 'single_event.jsonl').write_text(''.join(json.dumps(c)+'\n' for c in rows))
    elif change == 'frozen_data':
        (root / 'registered_data/main.jsonl').write_text('{}\n')
    else:
        obj=runner.read_json(root / 'manifest.json')
        if change == 'runtime': obj['runtime']['python']='unsupported-runtime'
        else: obj['code_sha256']='unsupported-code-change'
        runner.write_json(root / 'manifest.json',obj)
    original=(root / 'manifest.json').read_bytes()
    with pytest.raises(ValueError): repair.plan_repair(root,config,data,runner)
    assert (root / 'manifest.json').read_bytes()==original and not (root / 'repairs').exists()


def test_any_unregistered_provider_changes_are_refused(tmp_path,monkeypatch):
    root,config,data,_=registered_fixture(tmp_path)
    monkeypatch.setattr(repair,'AFTER_PROVIDER_SHA256','unsupported-patch')
    with pytest.raises(ValueError,match='exact supported copy repair'):
        repair.plan_repair(root,config,data,runner)


def test_results_changed_after_audit_block_registration(tmp_path):
    root,config,data,jobs=registered_fixture(tmp_path)
    plan=repair.plan_repair(root,config,data,runner)
    path=runner.job_path(root,jobs[0])/'episode.json'
    runner.write_json(path,{'changed_after_audit':True})
    original=(root/'manifest.json').read_bytes()
    with pytest.raises(ValueError,match='Results changed'):
        repair.apply_repair(root,plan,runner)
    assert (root/'manifest.json').read_bytes()==original and not (root/'repairs').exists()
