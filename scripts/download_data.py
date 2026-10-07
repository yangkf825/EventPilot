#!/usr/bin/env python3
"""Download the pinned official corpus locally; never redistribute raw files.

Requires curl for resumable transfers. This command can download several GB;
it is separate from the benchmark's small public author-candidate files.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / 'data/source_metadata/mind2web_metadata.json'


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def selected_entries(info, scope):
    selected = []
    for entry in info['siblings']:
        name = entry['rfilename']
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name:
            raise ValueError('Unsafe registered download filename')
        wanted = name == 'README.md'
        wanted |= scope == 'pilot' and name == 'data/train/train_10.json'
        wanted |= scope in ('test', 'all') and name == 'test.zip'
        wanted |= scope == 'all' and name.startswith('data/train/') and name.endswith('.json')
        if wanted:
            selected.append(entry)
    return sorted(selected, key=lambda entry: entry.get('size', 0))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=('pilot', 'test', 'all'), default='all')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--raw-dir', type=Path, default=ROOT / 'data/raw/Mind2Web')
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error('--workers must be positive')
    if not METADATA.is_file():
        raise ValueError('Bundled pinned metadata is missing: data/source_metadata/mind2web_metadata.json')
    info = json.loads(METADATA.read_text(encoding='utf-8'))
    registration = json.loads((ROOT / 'data/source_metadata/source_task_ids.json').read_text(encoding='utf-8'))
    if info['sha'] != registration['revision']:
        raise ValueError('Official revision differs from the benchmark source registration')
    entries = selected_entries(info, args.scope)
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    print(f"Official revision: {info['sha']}; {len(entries)} files; "
          f"{sum(entry['size'] for entry in entries) / 1e9:.2f} GB", flush=True)

    def download(entry):
        name = entry['rfilename']
        target = args.raw_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = entry.get('lfs', {}).get('sha256')
        if target.is_file() and target.stat().st_size == entry['size']:
            actual = digest(target)
            if expected and actual != expected:
                raise ValueError(f'{name}: checksum mismatch; existing file retained for inspection')
            return {'file': name, 'size': target.stat().st_size, 'sha256': actual,
                    'revision': info['sha'], 'status': 'verified'}
        partial = target.with_name(target.name + '.partial')
        url = f"https://huggingface.co/datasets/osunlp/Mind2Web/resolve/{info['sha']}/{name}?download=true"
        print('Downloading ' + name, flush=True)
        subprocess.run(['curl', '-fL', '--retry', '5', '--retry-delay', '2',
                        '--connect-timeout', '30', '--speed-time', '120', '--speed-limit', '1024',
                        '--continue-at', '-', '--silent', '--show-error', url, '-o', str(partial)], check=True)
        if partial.stat().st_size != entry['size']:
            raise ValueError(f'{name}: size mismatch')
        actual = digest(partial)
        if expected and actual != expected:
            raise ValueError(f'{name}: LFS SHA256 mismatch')
        partial.replace(target)
        print(f'Verified {name} ({entry["size"] / 1e6:.1f} MB)', flush=True)
        return {'file': name, 'size': entry['size'], 'sha256': actual,
                'revision': info['sha'], 'status': 'verified'}

    results, failures = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(download, entry): entry for entry in entries}
        for future in concurrent.futures.as_completed(pending):
            try:
                results.append(future.result())
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                failures.append({'file': pending[future]['rfilename'], 'error': str(error)})
                print(str(error), flush=True)
            manifest = {'revision': info['sha'], 'metadata_sha256': digest(METADATA),
                        'verified': sorted(results, key=lambda row: row['file']), 'failures': failures}
            (args.raw_dir.parent / f'download_manifest_{args.scope}.json').write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if failures:
        return 1
    print('Official files verified. Run python scripts/prepare_dataset.py --extract next.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
