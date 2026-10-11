"""A second writer must never overwrite a registered experiment."""
import pytest
from eventarena.run_lock import exclusive_run, RunAlreadyActiveError


def test_duplicate_writer_rejected_and_other_directory_allowed(tmp_path):
    with exclusive_run(tmp_path / 'run'):
        with pytest.raises(RunAlreadyActiveError, match='Another process'):
            with exclusive_run(tmp_path / 'run'):
                pytest.fail('second writer acquired the run')
        with exclusive_run(tmp_path / 'other'):
            pass
    with exclusive_run(tmp_path / 'run'):
        pass


def test_exception_releases_lock_without_changing_results(tmp_path):
    result = tmp_path / 'result.json'
    result.write_text('{"state":"finished"}')
    with pytest.raises(ValueError):
        with exclusive_run(tmp_path):
            raise ValueError('test interrupted writer')
    with exclusive_run(tmp_path):
        assert result.read_text() == '{"state":"finished"}'
