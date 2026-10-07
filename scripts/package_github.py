#!/usr/bin/env python3
"""Create a public source ZIP from an explicit file allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

from check_public_data import check

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ('run_online_v2.py', 'build_online_v2.py', 'validate_online_v2.py',
           'analyze_online_v2.py', 'preflight_online_v2.py', 'review_online_v2.py',
           'prepare_online_v2.py', 'paired_online_v2.py', 'recompute_online_v2.py',
           'probe_gateway.py', 'run_models.py', 'metrics.py', 'download_data.py',
           'prepare_dataset.py', 'check_public_data.py', 'package_github.py')
PUBLIC_DATA = ('single_event.jsonl', 'counterfactual.jsonl', 'multi_event.jsonl',
               'manifest.json', 'split_manifest.json', 'INPUT_IDENTIFIABILITY.json')
ROOT_FILES = ('README.md', 'THIRD_PARTY_NOTICES.md', '.gitignore', '.env.example',
              'models.example.json', 'requirements.txt', 'requirements-online-v2.lock.txt', 'pytest.ini')


def selected_files(root):
    root = Path(root)
    files = [root / name for name in ROOT_FILES]
    files += [root / 'scripts' / name for name in SCRIPTS]
    files += sorted((root / 'eventarena').glob('*.py'))
    files += sorted((root / 'tests').glob('test_online_v2*.py'))
    files += sorted((root / 'tests').glob('test_github_*.py'))
    files += [root / 'tests/conftest.py', root / 'config/online_v2_authoring.py',
              root / 'config/README.md', root / 'docs/DATA_SETUP.md', root / 'docs/PROTOCOL.md',
              root / '.github/workflows/tests.yml']
    files += [root / 'data/author_candidates' / name for name in PUBLIC_DATA]
    files += [root / 'data/source_metadata' / name for name in
              ('source_task_ids.json', 'mind2web_metadata.json')]
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError('Required public source file missing or symlink: ' + str(path.relative_to(root)))
    return sorted(set(files))


def audit_payloads(payloads):
    # Values, not environment variable names or credential-filtering code.
    checks = [(re.compile(r'sk-[A-Za-z0-9_-]{20,}'), 'possible actual API key'),
              (re.compile(r'(?<![A-Za-z0-9])[0-9a-f]{32}\.[A-Za-z0-9]{12,}'), 'possible actual provider key'),
              (re.compile(r'/(?:Volumes|Users)/'), 'machine-specific absolute path')]
    for name, raw in payloads.items():
        text = raw.decode('utf-8')
        for pattern, reason in checks:
            if pattern.search(text):
                raise ValueError(f'Publication audit rejected {name}: {reason}')
        if name == 'models.example.json':
            for model in json.loads(text)['models']:
                if model['endpoint'] != 'https://api.example.com/v1/chat/completions':
                    raise ValueError('Example model configuration must use placeholder endpoint')
                if model['model'] != 'REPLACE_WITH_EXACT_MODEL_ID':
                    raise ValueError('Example model configuration must use placeholder model ID')


def package(root, output):
    root, output = Path(root), Path(output)
    report = check(root)
    payloads = {path.relative_to(root).as_posix(): path.read_bytes() for path in selected_files(root)}
    audit_payloads(payloads)
    manifest = {'scope': 'github_public_source_and_author_candidates', 'version': 'online_v2.0.5',
                'contains_api_credentials': False, 'contains_original_source_texts': False,
                'contains_raw_html': False, 'contains_runtime_binaries': False,
                'human_reviewed': False, 'dataset_check': report,
                'local_setup': 'python scripts/prepare_dataset.py --download --extract',
                'files': {name: {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
                          for name, raw in payloads.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, raw in payloads.items():
            archive.writestr('EventArena-Mind2Web/' + name, raw)
        archive.writestr('EventArena-Mind2Web/PACKAGE_MANIFEST.json',
                         json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='utf-8')
    return {'archive': str(output), 'files': len(payloads) + 1, 'bytes': output.stat().st_size,
            'sha256': checksum, 'uncompressed_source_bytes': sum(len(raw) for raw in payloads.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'releases/EventArena_GitHub_v2.0.5.zip')
    args = parser.parse_args()
    print(json.dumps(package(ROOT, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
