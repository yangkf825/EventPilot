#!/usr/bin/env python3
"""Audit and register only the storage I/O repair for an existing pilot run.

Default mode is read-only. --apply backs up the original manifest and core,
records the exact correction and hashes of existing finished results, then
updates only code_sha256 so normal strict resume checks remain in force.
No API or browser calls are made. This is not a general version-check bypass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
BUG_ID = 'storage_io_20261008'
BEFORE_SHA256 = {'scripts/run_online_v2.py': 'ebab3d7b78aceeb783bf24d9e03021b06ee35f3caebeb94d530120b3b13d364a',
 'scripts/prepare_online_v2.py': '6c12544b11d3379ad5c2f42e7cdabfc8e7eb7bfcd8c07484aa432ba7a1dd44ea',
 'eventarena/online_v2_engine.py': '2e3ccb26802b721700fa76260c9f36d7d9165f67424453e3b7d0a6efa37c7823'}
AFTER_SHA256 = {'scripts/run_online_v2.py': '1990e6b5d8656cde6b7a61b129058d568a0ce686ac560e03db1c7da41edc860a',
 'scripts/prepare_online_v2.py': '62ed0be46e41ea37e8d3862653402527d519ff96a3014dcebf41d2231456cb57',
 'eventarena/online_v2_engine.py': 'd2364f9b10c63842ebaa5d4dc94880079ccd5fc2ee13a2054bda057bb6cc7973',
 'eventarena/storage_io.py': 'b624ba3ac11dd0b7de3ce051c4828aacf4fcce9c091846fe81c726795ff537f0'}
CORE_PATCHES = {'scripts/run_online_v2.py': [(22,
                               23,
                               '',
                               'from eventarena.storage_io import atomic_write_json, open_append, '
                               'StorageIOError\n'),
                              (40,
                               41,
                               '    path = Path(path)\n'
                               '    path.parent.mkdir(parents=True, exist_ok=True)\n'
                               "    temp = path.with_suffix(path.suffix + '.tmp')\n"
                               '    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), '
                               "encoding='utf-8')\n"
                               '    temp.replace(path)\n',
                               '    atomic_write_json(path, value)\n'),
                              (402,
                               403,
                               '                    with (self.run_root / '
                               "'model_call_usage.jsonl').open('a', encoding='utf-8') as stream:\n",
                               '                    with open_append(self.run_root / '
                               "'model_call_usage.jsonl') as stream:\n"),
                              (508,
                               509,
                               "                with (root / 'progress.jsonl').open('a', encoding='utf-8') "
                               'as stream:\n',
                               "                with open_append(root / 'progress.jsonl') as stream:\n"),
                              (599,
                               607,
                               '',
                               '                raise\n'
                               '            except StorageIOError:\n'
                               '                # A storage outage leaves the job resumable, never scored.\n'
                               "                state.update(state='interrupted', updated_at=now(), "
                               "error_type='StorageIOError')\n"
                               '                try:\n'
                               "                    write_json(folder / 'job.json', state)\n"
                               '                except OSError:\n'
                               '                    pass  # Retain the original error if this drive is still '
                               'unavailable.\n')],
 'scripts/prepare_online_v2.py': [(10,
                                   11,
                                   '',
                                   'from eventarena.storage_io import open_append, StorageIOError\n'),
                                  (61,
                                   62,
                                   "            with (self.root / name).open('a', encoding='utf-8') as "
                                   'stream:\n',
                                   '            with open_append(self.root / name) as stream:\n'),
                                  (80,
                                   81,
                                   "                with (self.root / name).open('a', encoding='utf-8') as "
                                   'stream:\n',
                                   '                with open_append(self.root / name) as stream:\n'),
                                  (184,
                                   186,
                                   '',
                                   '                    except StorageIOError:\n'
                                   '                        raise\n'),
                                  (212,
                                   213,
                                   '            except (asyncio.CancelledError, KeyboardInterrupt):\n',
                                   '            except (asyncio.CancelledError, KeyboardInterrupt, '
                                   'StorageIOError):\n'),
                                  (231,
                                   235,
                                   '        progress.persist()\n',
                                   '        try:\n'
                                   '            progress.persist()\n'
                                   '        except OSError:\n'
                                   '            pass  # Do not replace the original persistence error with '
                                   'another one.\n')],
 'eventarena/online_v2_engine.py': [(21,
                                     22,
                                     '',
                                     'from .storage_io import atomic_write_json, StorageIOError\n'),
                                    (84,
                                     85,
                                     '    path = Path(path)\n'
                                     '    path.parent.mkdir(parents=True, exist_ok=True)\n'
                                     '    temp = path.with_suffix(path.suffix + ".tmp")\n'
                                     '    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), '
                                     'encoding="utf-8")\n'
                                     '    temp.replace(path)\n',
                                     '    atomic_write_json(path, value)\n'),
                                    (738,
                                     740,
                                     '',
                                     '            except StorageIOError:\n                raise\n'),
                                    (881, 883, '', '        except StorageIOError:\n            raise\n'),
                                    (1316, 1318, '', '    except StorageIOError:\n        raise\n'),
                                    (1337, 1339, '', '    except StorageIOError:\n        raise\n'),
                                    (1457, 1459, '', '    except StorageIOError:\n        raise\n')]}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def core_files(project):
    return [project / 'scripts' / name for name in
            ('run_online_v2.py', 'validate_online_v2.py', 'prepare_online_v2.py',
             'analyze_online_v2.py', 'paired_online_v2.py')] + sorted((project / 'eventarena').glob('*.py'))


def pre_repair_core(runner):
    project = runner.ROOT
    before = {}
    for name, expected in AFTER_SHA256.items():
        raw = (project / name).read_bytes()
        if digest(raw) != expected:
            raise ValueError('Current source is not the exact supported storage repair: ' + name)
        if name in CORE_PATCHES:
            lines = raw.decode('utf-8').splitlines(True)
            for start, end, original, replacement in reversed(CORE_PATCHES[name]):
                if ''.join(lines[start:end]) != replacement:
                    raise ValueError('Storage patch reconstruction failed: ' + name)
                lines[start:end] = original.splitlines(True)
            original = ''.join(lines).encode('utf-8')
            if digest(original) != BEFORE_SHA256[name]:
                raise ValueError('Original storage source hash differs: ' + name)
            before[name] = original
    payloads = {str(p.relative_to(project)): before.get(str(p.relative_to(project)), p.read_bytes())
                for p in core_files(project) if p.exists() and p.name != 'storage_io.py'}
    return runner.sha({name: digest(raw) for name, raw in payloads.items()}), payloads


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
              'changed_core_files': list(AFTER_SHA256),
              'old_core_files_sha256': BEFORE_SHA256, 'new_core_files_sha256': AFTER_SHA256,
              'dataset_sha256': saved['dataset_sha256'], 'job_states': states,
              'preserved_result_files': len(finished), 'no_api_call': True,
              'scope': 'bounded retries for metadata I/O and propagation of persistent storage errors only',
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
        stream.flush()
        os.fsync(stream.fileno())


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
    temporary = root / 'manifest.storage_fix.tmp'
    with temporary.open('wb') as stream:
        stream.write(updated_bytes)
        stream.flush()
        os.fsync(stream.fileno())
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
    cli.add_argument('--apply', action='store_true', help='Register the audited storage-only repair after backing up original provenance')
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
