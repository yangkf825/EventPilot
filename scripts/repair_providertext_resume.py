#!/usr/bin/env python3
"""Audit and register only the ProviderText copy repair for an existing pilot run.

Default mode is read-only. --apply backs up the original manifest and core,
records the exact correction and hashes of existing finished results, then
updates only code_sha256 so normal strict resume checks remain in force.
No API or browser calls are made. This is not a general version-check bypass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
BUG_ID = 'providertext_copy_20261008'
BEFORE_PROVIDER_SHA256 = '4ebf4b339b30cf62f74d4358284670caff2bd919144e6aa1f3ca1d92bbe7cce3'
AFTER_PROVIDER_SHA256 = '1330b248ca75571d3c1a56791c197bfa00e475a9b16d9c6253dd5f610e9106e1'
COPY_METHOD = '''    def __getnewargs__(self):
        # copy/pickle must supply both constructor arguments when rebuilding
        # this str subclass; deepcopy also isolates nested diagnostic values.
        return str(self), self.diagnostics

'''.encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def core_files(project):
    return [project / 'scripts' / name for name in
            ('run_online_v2.py', 'validate_online_v2.py', 'prepare_online_v2.py',
             'analyze_online_v2.py', 'paired_online_v2.py')] + sorted((project / 'eventarena').glob('*.py'))


def pre_repair_core(runner):
    project = runner.ROOT
    provider = project / 'eventarena/providers.py'
    after = provider.read_bytes()
    if digest(after) != AFTER_PROVIDER_SHA256 or after.count(COPY_METHOD) != 1:
        raise ValueError('Current provider is not the exact supported copy repair')
    before = after.replace(COPY_METHOD, b'', 1)
    if digest(before) != BEFORE_PROVIDER_SHA256:
        raise ValueError('Cannot reconstruct the exact original provider source')
    payloads = {str(p.relative_to(project)): (before if p == provider else p.read_bytes())
                for p in core_files(project) if p.exists()}
    old_fingerprint = runner.sha({name: digest(raw) for name, raw in payloads.items()})
    return old_fingerprint, payloads


def result_hashes(root, jobs, runner):
    states, finished = Counter(), {}
    for job in jobs:
        folder = runner.job_path(root, job)
        state = runner.read_json(folder / 'job.json', {}).get('state', 'pending')
        states[state] += 1
        if state == 'finished':
            for name in ('job.json', 'episode.json', 'metric_record.json'):
                path = folder / name
                if path.is_file():
                    finished[str(path.relative_to(root))] = digest(path.read_bytes())
    return dict(states), finished


def plan_repair(root, config, data_dir, runner):
    root = Path(root).resolve()
    manifest_path = root / 'manifest.json'
    original_bytes = manifest_path.read_bytes()
    saved = json.loads(original_bytes)
    old_code, payloads = pre_repair_core(runner)
    new_code = runner.code_fingerprint()
    if saved.get('code_sha256') not in {old_code, new_code}:
        raise ValueError('Other core code changes are present; this repair cannot migrate the run')
    args = runner.parser().parse_args(['run', '--run-root', str(root), '--config', str(config),
                                       '--data-dir', str(data_dir), '--resume'])
    for name, value in saved['arguments'].items():
        setattr(args, name, value)
    selected = runner.select_cases(args)
    configured = {m['name']: m for m in runner.read_json(config)['models']}
    jobs = runner.make_jobs(selected, saved['models'], args.repeats, args.seed)
    rebuilt = runner.make_manifest(args, selected, saved['models'], configured, jobs)
    updated = dict(saved, code_sha256=new_code)
    if rebuilt != updated:
        raise ValueError('Dataset, model configuration, runtime or registered parameters differ; repair refused')
    frozen = {name: runner.read_cases(root / 'registered_data' / (name + '.jsonl'))
              for name in saved['counts']}
    if runner.sha(frozen) != saved['dataset_sha256']:
        raise ValueError('Registered dataset was changed or corrupted; repair refused')
    states, finished = result_hashes(root, jobs, runner)
    report = {'bug_id': BUG_ID, 'run_root': str(root),
              'status': 'already_current' if saved['code_sha256'] == new_code else 'ready_to_apply',
              'old_code_sha256': saved['code_sha256'], 'new_code_sha256': new_code,
              'changed_core_files': ['eventarena/providers.py'],
              'old_provider_sha256': BEFORE_PROVIDER_SHA256, 'new_provider_sha256': AFTER_PROVIDER_SHA256,
              'dataset_sha256': saved['dataset_sha256'], 'job_states': states,
              'preserved_result_files': len(finished), 'no_api_call': True,
              'scope': 'constructor reconstruction for copied provider response text and diagnostics only',
              'prompts_labels_budgets_scoring_unchanged': True,
              'legacy_attempts_retained': True,
              'research_note': 'Continued pilot has explicitly recorded pre/post repair code provenance; use a fresh frozen run for a clean comparison.'}
    return report, original_bytes, updated, payloads, finished


def write_once(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('Existing repair backup differs; refusing overwrite')
        return
    with path.open('xb') as stream:
        stream.write(raw)


def apply_repair(root, plan, runner):
    report, original_bytes, updated, payloads, finished = plan
    if report['status'] == 'already_current':
        return report
    root = Path(root).resolve()
    if (root / 'manifest.json').read_bytes() != original_bytes:
        raise ValueError('Manifest changed while checking; stop the original process and retry')
    _, current_finished = result_hashes(root, updated['jobs'], runner)
    if current_finished != finished:
        raise ValueError('Results changed while checking; stop the original process and retry')
    folder = root / 'repairs' / BUG_ID
    updated_bytes = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode()
    write_once(folder / 'manifest.before.json', original_bytes)
    write_once(folder / 'manifest.after.json', updated_bytes)
    for name, raw in payloads.items():
        write_once(folder / 'core_before' / name, raw)
    proof = json.dumps(finished, ensure_ascii=False, sort_keys=True, indent=2).encode() + b'\n'
    write_once(folder / 'preserved_results_sha256.json', proof)
    record_path = folder / 'REPAIR.json'
    if not record_path.exists():
        record = dict(report, status='registered_code_repair',
                      recorded_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                      before_manifest_sha256=digest(original_bytes), after_manifest_sha256=digest(updated_bytes))
        write_once(record_path, json.dumps(record, ensure_ascii=False, indent=2).encode() + b'\n')
    else:
        record = json.loads(record_path.read_text())
        if record.get('before_manifest_sha256') != digest(original_bytes) or record.get('after_manifest_sha256') != digest(updated_bytes):
            raise ValueError('Existing repair record differs; refusing overwrite')
    temporary = root / 'manifest.providertext_fix.tmp'
    temporary.write_bytes(updated_bytes)
    temporary.replace(root / 'manifest.json')
    _, after_finished = result_hashes(root, updated['jobs'], runner)
    if after_finished != finished:
        raise RuntimeError('Results changed during repair; inspect preserved result hashes before resuming')
    return dict(report, status='applied', repair_record=str(record_path))


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--run-root', type=Path, required=True)
    cli.add_argument('--config', type=Path)
    cli.add_argument('--data-dir', type=Path, default=ROOT / 'data/online_v2')
    cli.add_argument('--apply', action='store_true', help='Register the audited copy-only repair after backing up original provenance')
    args = cli.parse_args(argv)
    import run_online_v2 as runner
    root = args.run_root.expanduser().resolve()
    saved_config = root / 'logs/model_config.json'
    config = (args.config or (saved_config if saved_config.is_file() else ROOT / 'models.gateway.json')).expanduser().resolve()
    try:
        plan = plan_repair(root, config, args.data_dir.expanduser().resolve(), runner)
        report = apply_repair(root, plan, runner) if args.apply else plan[0]
    except (OSError, ValueError, KeyError) as error:
        cli.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
