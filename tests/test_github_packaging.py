"""Check that public packaging cannot pick up local datasets or credentials."""
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('github_packaging', ROOT / 'scripts/package_github.py')
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


def test_allowlist_excludes_private_data_and_local_config():
    paths = {path.relative_to(ROOT).as_posix() for path in pack.selected_files(ROOT)}
    assert 'README.md' in paths and 'models.example.json' in paths
    assert 'scripts/prepare_dataset.py' in paths
    assert not any(path.startswith(('data/raw/', 'data/online_v2/', 'data/local/', 'runs/', 'logs/'))
                   for path in paths)
    assert not paths.intersection({'models.gateway.json', 'models.local.json', '.env'})


def test_machine_paths_and_actual_credential_patterns_are_rejected():
    # Synthetic fixture values are composed only in memory, never published as keys.
    for value in ('sk-' + 'x' * 30, '/' + 'Users' + '/fixture/private', '/' + 'Volumes' + '/fixture'):
        with pytest.raises(ValueError, match='Publication audit rejected'):
            pack.audit_payloads({'example.txt': value.encode()})


def test_selected_source_passes_publication_audit():
    payloads = {path.relative_to(ROOT).as_posix(): path.read_bytes() for path in pack.selected_files(ROOT)}
    pack.audit_payloads(payloads)
