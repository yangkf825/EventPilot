"""Local reconstruction tests: no official download or model API is invoked."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('github_prepare_dataset', ROOT / 'scripts/prepare_dataset.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def source_fixture(tmp_path):
    """Synthetic metadata exercises exact selection, not production authorship."""
    raw = tmp_path / 'raw'
    train = raw / 'data/train'
    test = raw / 'test_domain'
    train.mkdir(parents=True)
    test.mkdir()
    records, registration, normalized = [], [], []
    for index in range(54):
        uid, goal = f'fixture-{index:02d}', f'Official fixture task {index} — source text'
        split = 'train' if index < 27 else 'test_domain'
        records.append({'annotation_id': uid, 'website': 'fixture.example', 'confirmed_task': goal,
                        'actions': [{'raw_html': '<p>HTML_NOT_IN_INDEX</p>', 'annotation_id': 'nested-id',
                                     'confirmed_task': 'Nested values must not replace actual goal'}]})
        normalized.append({'task_id': uid, 'goal': goal, 'website': 'fixture.example', 'split': split})
        registration.append({'task_id': uid, 'website': 'fixture.example', 'split': split,
                             'original_goal_sha256': hashlib.sha256(goal.encode()).hexdigest()})
    (train / 'train_0.json').write_text(json.dumps(records[:27]), encoding='utf-8')
    (test / 'test_0.json').write_text(json.dumps(records[27:]), encoding='utf-8')
    payload = ''.join(json.dumps(row, sort_keys=True, ensure_ascii=False) + '\n' for row in normalized).encode()
    registered = {'revision': 'fixture-revision', 'tasks': registration,
                  'expected_private_source_index_sha256': hashlib.sha256(payload).hexdigest()}
    return raw, registered, payload


def test_all_54_source_metadata_are_selected_and_exactly_verified(tmp_path):
    raw, registered, payload = source_fixture(tmp_path)
    output = tmp_path / 'local/source_task_index.jsonl'
    report = setup.extract_selected_metadata(raw, registered, output)
    assert report['tasks'] == 54
    assert output.read_bytes() == payload
    assert b'HTML_NOT_IN_INDEX' not in output.read_bytes()
    assert b'actions' not in output.read_bytes()


def test_actual_bundled_registration_contains_54_only_hashes_and_ids():
    registration = setup.load_registration(ROOT / 'data/source_metadata/source_task_ids.json')
    assert len(registration['tasks']) == 54
    assert all('goal' not in row and 'original_goal' not in row for row in registration['tasks'])


def test_wrong_original_goal_or_whole_index_hash_never_writes(tmp_path):
    raw, registered, _ = source_fixture(tmp_path)
    output = tmp_path / 'private.jsonl'
    registered['tasks'][0]['original_goal_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='Original goal SHA256'):
        setup.extract_selected_metadata(raw, registered, output)
    assert not output.exists()
    raw, registered, _ = source_fixture(tmp_path / 'second')
    registered['expected_private_source_index_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='frozen benchmark SHA256'):
        setup.extract_selected_metadata(raw, registered, output)
    assert not output.exists()


def test_missing_sources_explain_full_download_and_extract(tmp_path):
    _, registered, _ = source_fixture(tmp_path)
    with pytest.raises(ValueError, match='--download --extract'):
        setup.extract_selected_metadata(tmp_path / 'absent', registered, tmp_path / 'out.jsonl')


@pytest.mark.parametrize('name', ('../outside.json', '/absolute.json', 'C:/outside.json', '..\\outside.json'))
def test_zip_path_traversal_is_rejected_before_any_write(tmp_path, name):
    archive = tmp_path / 'test.zip'
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr('test_domain/safe.json', '[]')
        stream.writestr(name, '[]')
    destination = tmp_path / 'raw'
    with pytest.raises(ValueError, match='Unsafe ZIP member'):
        setup.safe_extract_test(archive, destination)
    assert not destination.exists()


def test_zip_symlink_and_preexisting_parent_symlink_are_rejected(tmp_path):
    archive = tmp_path / 'test.zip'
    with zipfile.ZipFile(archive, 'w') as stream:
        member = zipfile.ZipInfo('escape')
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        stream.writestr(member, '../outside')
    with pytest.raises(ValueError, match='symlink'):
        setup.safe_extract_test(archive, tmp_path / 'raw')
    outside, destination = tmp_path / 'outside', tmp_path / 'raw'
    outside.mkdir()
    destination.mkdir()
    (destination / 'test_domain').symlink_to(outside, target_is_directory=True)
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr('test_domain/test_0.json', '[]')
    with pytest.raises(ValueError, match='escapes destination'):
        setup.safe_extract_test(archive, destination)
    assert not list(outside.iterdir())


def test_safe_zip_is_extracted_locally(tmp_path):
    archive = tmp_path / 'test.zip'
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr('test_domain/test_0.json', '[]')
    setup.safe_extract_test(archive, tmp_path / 'raw')
    assert (tmp_path / 'raw/test_domain/test_0.json').read_text() == '[]'


def test_compilation_invokes_only_builder_and_validator_and_checks_projection(tmp_path, monkeypatch):
    candidates, output = tmp_path / 'public', tmp_path / 'runtime'
    candidates.mkdir()
    output.mkdir()
    files = {}
    for name in setup.CASE_FILES:
        public = {'case_id': name, 'source': {'task_id': 'source-id'},
                  'goal': 'Authored public research adaptation', 'events': [{'text': 'Incoming event'}]}
        raw = (json.dumps(public) + '\n').encode()
        (candidates / name).write_bytes(raw)
        files[name] = {'sha256': hashlib.sha256(raw).hexdigest()}
        private = {**public, 'source': {**public['source'], 'original_goal': 'Actual official text'},
                   'entry_readiness': {'entry_status': 'not_preflighted'}}
        (output / name).write_text(json.dumps(private) + '\n')
    (candidates / 'manifest.json').write_text(json.dumps({'files': files}))
    calls = []
    monkeypatch.setattr(setup.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    setup.compile_dataset(tmp_path / 'source-index.jsonl', output, candidates, root=tmp_path)
    assert len(calls) == 2
    assert calls[0][1].endswith('build_online_v2.py') and '--tasks' in calls[0]
    assert calls[1][1].endswith('validate_online_v2.py')
    row = json.loads((output / setup.CASE_FILES[0]).read_text())
    row['events'][0]['text'] = 'Changed event violates the frozen release'
    (output / setup.CASE_FILES[0]).write_text(json.dumps(row) + '\n')
    with pytest.raises(ValueError, match='published author candidates'):
        setup.verify_public_cases(output, candidates)


def test_downloader_creates_empty_raw_directory_without_network(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('github_download_data', ROOT / 'scripts/download_data.py')
    download = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(download)
    metadata = tmp_path / 'data/source_metadata'
    metadata.mkdir(parents=True)
    (metadata / 'mind2web_metadata.json').write_text(json.dumps({'sha': 'fixed', 'siblings': []}))
    (metadata / 'source_task_ids.json').write_text(json.dumps({'revision': 'fixed'}))
    monkeypatch.setattr(download, 'ROOT', tmp_path)
    monkeypatch.setattr(download, 'METADATA', metadata / 'mind2web_metadata.json')
    monkeypatch.setattr(download.subprocess, 'run', lambda *args, **kwargs: pytest.fail('No network should be used'))
    raw = tmp_path / 'data/raw/Mind2Web'
    assert download.main(['--raw-dir', str(raw)]) == 0
    assert raw.is_dir()
