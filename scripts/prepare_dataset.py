#!/usr/bin/env python3
"""Reconstruct the private runtime dataset from locally obtained official data.

Only 54 registered tasks' metadata are retained. HTML/actions are not copied to
the prepared index. Download and test-archive extraction are explicit options.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import zipfile

import ijson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASE_FILES = ('single_event.jsonl', 'counterfactual.jsonl', 'multi_event.jsonl')


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def safe_extract_test(archive, destination):
    """Validate every ZIP member before writing, including symlink parents."""
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as source:
        members = []
        for member in source.infolist():
            name = member.filename
            portable = PurePosixPath(name.replace('\\', '/'))
            if portable.is_absolute() or '..' in portable.parts or re.match(r'^[A-Za-z]:', name):
                raise ValueError('Unsafe ZIP member path; nothing extracted')
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError('ZIP symlink is forbidden; nothing extracted')
            target = (destination / portable).resolve()
            if not target.is_relative_to(destination):
                raise ValueError('ZIP member escapes destination; nothing extracted')
            members.append((member, target))
        destination.mkdir(parents=True, exist_ok=True)
        for member, target in members:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member, pwd=b'mind2web') as incoming, target.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=8 * 1024 * 1024)


def load_registration(path):
    registration = json.loads(Path(path).read_text(encoding='utf-8'))
    entries = registration['tasks']
    if len(entries) != 54 or len({entry['task_id'] for entry in entries}) != 54:
        raise ValueError('Source registration must contain exactly 54 unique task IDs')
    for entry in entries:
        if not all(isinstance(entry.get(key), str) and entry[key] for key in
                   ('task_id', 'website', 'split', 'original_goal_sha256')):
            raise ValueError('Invalid registered source metadata')
        if not re.fullmatch('[0-9a-f]{64}', entry['original_goal_sha256']):
            raise ValueError('Invalid registered original-goal SHA256')
    if not re.fullmatch('[0-9a-f]{64}', registration.get('expected_private_source_index_sha256', '')):
        raise ValueError('Missing exact private source-index SHA256')
    return registration


def metadata_rows(path):
    """Stream only metadata scalars; do not build or retain HTML/action trees."""
    row = None
    fields = {'item.annotation_id': 'task_id', 'item.confirmed_task': 'goal', 'item.website': 'website'}
    with Path(path).open('rb') as stream:
        for prefix, event, value in ijson.parse(stream, use_float=True):
            if prefix == 'item' and event == 'start_map':
                row = {}
            elif prefix in fields and event == 'string' and row is not None:
                row[fields[prefix]] = value
            elif prefix == 'item' and event == 'end_map':
                yield row
                row = None


def extract_selected_metadata(raw_dir, registration, output):
    raw_dir, output = Path(raw_dir), Path(output)
    files = sorted(raw_dir.glob('**/train_*.json')) + sorted(raw_dir.glob('**/test_*/*.json'))
    if not files:
        raise ValueError('No official train/test JSON found. Run python scripts/prepare_dataset.py --download --extract, '
                         'or put official files in data/raw/Mind2Web and run --extract for test.zip.')
    expected = {entry['task_id']: entry for entry in registration['tasks']}
    selected = {}
    for path in files:
        split = path.parent.name
        for row in metadata_rows(path):
            uid = row.get('task_id')
            if uid not in expected:
                continue
            if uid in selected:
                raise ValueError('Duplicate registered task in official files: ' + uid)
            if set(row) != {'task_id', 'goal', 'website'}:
                raise ValueError('Official selected task is missing required metadata: ' + uid)
            spec = expected[uid]
            if row['website'] != spec['website'] or split != spec['split']:
                raise ValueError('Official website/split mismatch for ' + uid)
            if hashlib.sha256(row['goal'].encode('utf-8')).hexdigest() != spec['original_goal_sha256']:
                raise ValueError('Original goal SHA256 mismatch for ' + uid)
            selected[uid] = {**row, 'split': split}
        print(f'Inspected {path.name}; recovered {len(selected)}/54 registered tasks', flush=True)
    missing = sorted(set(expected) - set(selected))
    if missing:
        raise ValueError(f'Missing {len(missing)} registered tasks. The pilot shard is insufficient; obtain all train JSON '
                         'and extract test.zip using python scripts/prepare_dataset.py --download --extract.')
    payload = ''.join(json.dumps(selected[uid], ensure_ascii=False, sort_keys=True) + '\n'
                      for uid in sorted(selected)).encode('utf-8')
    actual = hashlib.sha256(payload).hexdigest()
    if actual != registration['expected_private_source_index_sha256']:
        raise ValueError('Recovered 54-task source index differs from the frozen benchmark SHA256')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    return {'tasks': len(selected), 'sha256': actual, 'source_files': len(files), 'output': str(output)}


def public_projection(value):
    if isinstance(value, dict):
        return {key: public_projection(item) for key, item in value.items()
                if key not in {'original_goal', 'entry_readiness'}}
    if isinstance(value, list):
        return [public_projection(item) for item in value]
    return value


def verify_public_cases(output_dir, candidate_dir):
    manifest = json.loads((Path(candidate_dir) / 'manifest.json').read_text(encoding='utf-8'))
    for name in CASE_FILES:
        authored = Path(candidate_dir) / name
        if digest(authored) != manifest['files'][name]['sha256']:
            raise ValueError('Public author-candidate hash mismatch: ' + name)
        public = [json.loads(line) for line in authored.read_text(encoding='utf-8').splitlines() if line.strip()]
        compiled = [json.loads(line) for line in (Path(output_dir) / name).read_text(encoding='utf-8').splitlines()
                    if line.strip()]
        if public_projection(compiled) != public_projection(public):
            raise ValueError('Compiled cases differ from the published author candidates: ' + name)


def compile_dataset(source_index, output_dir, candidate_dir=None, root=None):
    root = Path(root or ROOT)
    candidate_dir = Path(candidate_dir or root / 'data/author_candidates')
    subprocess.run([sys.executable, str(root / 'scripts/build_online_v2.py'),
                    '--tasks', str(source_index), '--output-dir', str(output_dir)], check=True)
    verify_public_cases(output_dir, candidate_dir)
    subprocess.run([sys.executable, str(root / 'scripts/validate_online_v2.py'),
                    '--data-dir', str(output_dir)], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true', help='Explicitly download pinned official train JSON and test.zip (several GB)')
    parser.add_argument('--extract', action='store_true', help='Verify and extract local official test.zip with its official password')
    parser.add_argument('--raw-dir', type=Path, default=ROOT / 'data/raw/Mind2Web')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'data/online_v2')
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error('--workers must be positive')
    registration = load_registration(ROOT / 'data/source_metadata/source_task_ids.json')
    if args.download:
        subprocess.run([sys.executable, str(ROOT / 'scripts/download_data.py'), '--scope', 'all',
                        '--workers', str(args.workers), '--raw-dir', str(args.raw_dir)], check=True)
    if args.extract:
        archive = args.raw_dir / 'test.zip'
        if not archive.is_file():
            parser.error('test.zip is missing. Use --download --extract or obtain the official archive locally.')
        info = json.loads((ROOT / 'data/source_metadata/mind2web_metadata.json').read_text(encoding='utf-8'))
        if info['sha'] != registration['revision']:
            raise ValueError('Official download revision differs from registered source IDs')
        expected = next(entry['lfs']['sha256'] for entry in info['siblings'] if entry['rfilename'] == 'test.zip')
        if digest(archive) != expected:
            raise ValueError('Official test.zip SHA256 mismatch; archive was not extracted')
        safe_extract_test(archive, args.raw_dir)
    index = ROOT / 'data/local/source_task_index.jsonl'
    report = extract_selected_metadata(args.raw_dir, registration, index)
    compile_dataset(index, args.output_dir)
    print(json.dumps({'prepared': str(args.output_dir), 'source_index': report,
                      'human_reviewed': False, 'all_cases_execution_verified': False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
