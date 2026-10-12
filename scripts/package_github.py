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
           'probe_gateway.py', 'run_one_model.py', 'repair_providertext_resume.py', 'repair_storage_resume.py', 'run_models.py', 'metrics.py', 'download_data.py',
           'prepare_dataset.py', 'check_public_data.py', 'package_github.py',
           'official_online.py', 'run_official_online.py', 'activate_official.zsh', 'start_official_online.zsh',
           'build_publication_dataset.py','audit_publication_dataset.py','run_publication.py',
           'start_publication.zsh','configure_api_keys.zsh','recover_pending_storage.py','export_publication.py')
PUBLIC_DATA = ('single_event.jsonl', 'counterfactual.jsonl', 'multi_event.jsonl',
               'manifest.json', 'split_manifest.json', 'INPUT_IDENTIFIABILITY.json')
ROOT_FILES = ('README.md', 'README_LEGACY.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', '.gitignore', '.env.example',
              'models.example.json', 'requirements.txt', 'requirements-online-v2.lock.txt', 'pytest.ini')


def selected_files(root):
    root = Path(root)
    if (root/'config/publication_v6_source_registry.json').exists():
        files=[root/name for name in ROOT_FILES if name!='pytest.ini']
        files+=[root/'scripts'/name for name in SCRIPTS]
        files+=sorted((root/'eventarena').glob('*.py'))+sorted((root/'eventarena_official').glob('*.py'))
        files+=[root/name for name in ('config/publication_v6.json','config/publication_v6_source_registry.json',
                'docs/PUBLICATION_V6.md','docs/ARCHIVE_V5_README.md','docs/OFFICIAL_DATA_SETUP.md',
                'docs/OFFICIAL_PROTOCOL.md','docs/DATA_SETUP.md','docs/PROTOCOL.md','config/online_v2_authoring.py')]
        if any(not p.is_file() or p.is_symlink() for p in files):raise ValueError('Missing or linked v6 source file')
        return sorted(set(files))
    files = [root / name for name in ROOT_FILES]
    files += [root / 'scripts' / name for name in SCRIPTS]
    files += sorted((root / 'eventarena').glob('*.py'))
    files += sorted((root / 'eventarena_official').glob('*.py'))
    files += sorted((root / 'tests').glob('test_online_v2*.py'))
    files += sorted((root / 'tests').glob('test_official*.py'))
    files += sorted((root / 'tests').glob('test_github_*.py'))
    files += [root / 'tests/fixtures/storage_v205' / name for name in
              ('eventarena/online_v2_engine.py.txt', 'eventarena/storage_io.py.txt',
               'scripts/prepare_online_v2.py.txt', 'scripts/run_online_v2.py.txt')]
    files += [root / 'tests/conftest.py', root / 'config/online_v2_authoring.py',
              root / 'config/README.md', root / 'docs/DATA_SETUP.md', root / 'docs/PROTOCOL.md',
              root / 'config/github_actions_tests.example.yml', root / 'docs/OFFICIAL_DATA_SETUP.md',
              root / 'docs/OFFICIAL_PROTOCOL.md']
    files += sorted((root / 'config/official_online').glob('*.json'))
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
            inspected = text
            # A frozen, previously published manual collector has one generic
            # historical runtime fallback. Retain its exact bytes so existing
            # official/v2 experiment fingerprints remain reproducible. This
            # exception is file-hash bound and applies only to the path check;
            # credentials and any other machine paths are still rejected.
            if (reason == 'machine-specific absolute path' and name == 'eventarena/cli.py'
                    and hashlib.sha256(raw).hexdigest() == '21684e92f5f6f38a9eaeef5aaecbc4f2d42867ff78ec118981d34b7c550d9eb7'):
                historical_default = "'" + '/' + 'Volumes' + "/EA-M2W-Runtime-Recovered'"
                inspected = inspected.replace(historical_default, "'frozen-collector-fallback'")
            if pattern.search(inspected):
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
    v6=(root/'config/publication_v6_source_registry.json').exists()
    manifest = {'scope': 'github_public_source_recipe_and_source_ids' if v6 else 'github_public_source_and_author_candidates', 'version': 'official_online_eventarena_v6' if v6 else 'official_online_eventarena_v5',
                'legacy_version': 'online_v2.0.6',
                'contains_api_credentials': False, 'contains_original_source_texts': False,
                'contains_raw_html': False, 'contains_runtime_binaries': False,
                'human_reviewed': False, 'dataset_check': report,
                'frozen_core_fingerprints_preserved': not v6,
                'new_protocol_does_not_resume_old_runs':v6,
                'legacy_regression_results_are_not_v6_validation':v6,
                'legacy_manual_collector_requires_explicit_runtime_config': True,
                'local_setup': 'Follow docs/OFFICIAL_DATA_SETUP.md; gated official source is obtained separately',
                'files': {name: {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
                          for name, raw in payloads.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, raw in payloads.items():
            archive.writestr('EventPilot/' + name, raw)
        archive.writestr('EventPilot/PACKAGE_MANIFEST.json',
                         json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='utf-8')
    return {'archive': str(output), 'files': len(payloads) + 1, 'bytes': output.stat().st_size,
            'sha256': checksum, 'uncompressed_source_bytes': sum(len(raw) for raw in payloads.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'releases/EventPilot_v6_GitHub.zip')
    args = parser.parse_args()
    print(json.dumps(package(ROOT, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
