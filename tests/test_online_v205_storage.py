"""Fault injection for run persistence, without model APIs or external pages."""
import asyncio
import errno
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from eventarena import storage_io as storage
from eventarena import online_v2_engine as engine
import prepare_online_v2 as preparation
import run_online_v2 as runner
from test_online_v2_engine import case, FakeBrowser


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(storage.time, 'sleep', lambda _: None)


@pytest.mark.parametrize('number', [errno.EIO, errno.ESTALE])
def test_failed_append_open_is_retried_without_duplicate_record(tmp_path, monkeypatch, number):
    path = tmp_path / 'progress.jsonl'
    path.write_text('old\n')
    original = Path.open
    calls = []

    def unstable(p, *args, **kwargs):
        if p == path and (args[0] if args else kwargs.get('mode')) == 'a':
            calls.append(1)
            if len(calls) < 3:
                raise OSError(number, 'temporary disk failure')
        return original(p, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', unstable)
    with storage.open_append(path) as stream:
        stream.write('new\n')
    assert len(calls) == 3
    assert path.read_text() == 'old\nnew\n'


def test_persistent_open_failure_is_bounded_and_keeps_original_file(tmp_path, monkeypatch):
    path = tmp_path / 'progress.jsonl'
    path.write_text('old\n')
    original = Path.open
    calls = []
    delays = []
    monkeypatch.setattr(storage.time, 'sleep', delays.append)

    def broken(p, *args, **kwargs):
        if p == path and (args[0] if args else kwargs.get('mode')) == 'a':
            calls.append(1)
            raise OSError(errno.EIO, 'drive offline')
        return original(p, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', broken)
    with pytest.raises(storage.StorageIOError) as error:
        with storage.open_append(path):
            pytest.fail('A failed open must never enter the write block')
    assert error.value.errno == errno.EIO
    assert len(calls) == 4 and delays == list(storage.RETRY_DELAYS)
    assert path.read_text() == 'old\n'


@pytest.mark.parametrize('number', [errno.ENOSPC, errno.EACCES, errno.EROFS])
def test_non_transient_storage_errors_stop_immediately(number):
    calls = []

    def broken():
        calls.append(1)
        raise OSError(number, 'not transient')

    with pytest.raises(storage.StorageIOError) as error:
        storage.retry_io(broken, Path('state.json'))
    assert len(calls) == 1 and error.value.errno == number


def test_partial_append_is_never_replayed(monkeypatch, tmp_path):
    calls = []

    class PartialStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, text):
            calls.append(text)
            raise OSError(errno.EIO, 'write may be partial')

    monkeypatch.setattr(Path, 'open', lambda *a, **k: PartialStream())
    with pytest.raises(storage.StorageIOError, match='without replaying'):
        with storage.open_append(tmp_path / 'log.jsonl') as stream:
            stream.write('record\n')
    assert calls == ['record\n']


def test_atomic_commit_retries_replace_and_keeps_valid_json(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_text('{"old":true}')
    original = Path.replace
    calls = []

    def unstable(p, target):
        calls.append(1)
        assert json.loads(path.read_text()) == {'old': True}
        if len(calls) < 3:
            raise OSError(errno.EIO, 'rename failure')
        return original(p, target)

    monkeypatch.setattr(Path, 'replace', unstable)
    storage.atomic_write_json(path, {'new': '中文'})
    assert len(calls) == 3 and json.loads(path.read_text()) == {'new': '中文'}
    assert not path.with_suffix('.json.tmp').exists()


def test_failed_atomic_write_keeps_previous_state(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_text('{"old":true}')
    calls = []

    def broken(*args):
        calls.append(1)
        raise OSError(errno.EIO, 'fsync failed')

    monkeypatch.setattr(storage.os, 'fsync', broken)
    with pytest.raises(storage.StorageIOError):
        storage.atomic_write_json(path, {'new': True})
    assert len(calls) == 4 and json.loads(path.read_text()) == {'old': True}


def test_serialization_error_does_not_touch_existing_state(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{"old":true}')
    with pytest.raises(TypeError):
        storage.atomic_write_json(path, {'bad': object()})
    assert path.read_text() == '{"old":true}'


def test_cached_preparation_recovers_eio_at_case_seven(tmp_path, monkeypatch):
    cases = [{'case_id': f'X{i:02d}'} for i in range(10)]
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'single_event.jsonl').write_text(''.join(json.dumps(c) + '\n' for c in cases))
    root = tmp_path / 'run'
    for c in cases:
        runner.write_json(root / 'preparation_eligibility/repeat_0' / c['case_id'] / 'eligibility.json',
                          {'case_id': c['case_id'], 'repeat': 0, 'eligible': True, 'reason': 'reset_verified'})
    args = SimpleNamespace(data_dir=data, repeats=1, prep_model='unused', judge_model=None, retry_infra=False)
    original = Path.open
    opens = []

    def unstable(p, *a, **k):
        if p == root / 'preparation_progress.jsonl' and (a[0] if a else k.get('mode')) == 'a':
            opens.append(1)
            if len(opens) == 14:
                raise OSError(errno.EIO, 'original case-seven failure')
        return original(p, *a, **k)

    async def never_prepare(*args):
        raise AssertionError('Must not request model calls or open a browser')

    monkeypatch.setattr(Path, 'open', unstable)
    result = asyncio.run(preparation.prepare_pool(args, root, {'main': cases}, {}, {}, never_prepare))
    assert len(result) == 10
    assert runner.read_json(root / 'preparation_state.json')['state'] == 'finished'
    rows = [json.loads(line) for line in (root / 'preparation_progress.jsonl').read_text().splitlines()]
    assert len(rows) == 20
    assert len([r for r in rows if r['case_id'] == 'X06' and r['state'] == 'CACHED']) == 1


def test_model_response_log_failure_does_not_repeat_model_call_or_count_api_error(tmp_path, monkeypatch):
    calls = []

    class Model:
        usage = []

        def call(self, messages):
            calls.append(1)
            return '{"op":"ANSWER","answer":"done"}'

    model = runner.LimitedModel(Model(), tmp_path, 'fixture')
    e = engine.AutonomousEpisode(case(), model, tmp_path / 'episode', {})
    original = Path.open

    def broken(p, *a, **k):
        if p == tmp_path / 'model_call_usage.jsonl' and (a[0] if a else k.get('mode')) == 'a':
            raise OSError(errno.EIO, 'persistent disk outage')
        return original(p, *a, **k)

    monkeypatch.setattr(Path, 'open', broken)
    with pytest.raises(storage.StorageIOError):
        asyncio.run(e.call_record('actor', [], engine.normalize_action))
    assert len(calls) == 1
    assert e.episode['responses'] == []  # No false provider-error record.


def test_persistent_storage_failure_propagates_through_episode_without_scoring(tmp_path, monkeypatch):
    calls = []

    async def interrupted(*args):
        raise storage.StorageIOError(errno.EIO, 'disk offline')

    def judge(*args):
        calls.append(1)
        pytest.fail('Storage errors must not be evaluated')

    monkeypatch.setattr(engine.AutonomousEpisode, 'run', interrupted)
    with pytest.raises(storage.StorageIOError):
        asyncio.run(engine.run_episode(case(), object(), tmp_path, browser_factory=FakeBrowser, evaluator=judge))
    assert calls == []


def test_cached_preparation_stops_on_persistent_eio_without_changing_results(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'single_event.jsonl').write_text('{"case_id":"X"}\n')
    root = tmp_path / 'run'
    result = root / 'main/unit/episode.json'
    runner.write_json(result, {'saved_result': True})
    old = result.read_bytes()
    args = SimpleNamespace(data_dir=data, repeats=1, prep_model='unused', judge_model=None, retry_infra=False)
    original = Path.open

    def broken(p, *a, **k):
        if p == root / 'preparation_progress.jsonl' and (a[0] if a else k.get('mode')) == 'a':
            raise OSError(errno.EIO, 'persistent disk outage')
        return original(p, *a, **k)

    async def never_prepare(*args):
        pytest.fail('No model or browser should run after failed progress persistence')

    monkeypatch.setattr(Path, 'open', broken)
    with pytest.raises(storage.StorageIOError):
        asyncio.run(preparation.prepare_pool(args, root, {'main': [{'case_id': 'X'}]}, {}, {}, never_prepare))
    assert runner.read_json(root / 'preparation_state.json')['state'] == 'error'
    assert result.read_bytes() == old


def test_worker_storage_failure_leaves_job_resumable_and_unscored(tmp_path, monkeypatch):
    from eventarena import providers
    c = case()
    c['events'] = [c.pop('event')]
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'single_event.jsonl').write_text(json.dumps(c) + '\n')
    root = tmp_path / 'run'
    args = runner.parser().parse_args(['run', '--run-root', str(root), '--data-dir', str(data),
                                      '--only', 'unit', '--workers', '1', '--preparation-mode', 'on-demand'])
    args.prep_model = 'unit'
    args.judge_model = None
    selected = {'main': [c]}
    jobs = runner.make_jobs(selected, ['unit'], 1, args.seed)
    runner.write_json(root / 'manifest.json', {'jobs': jobs})

    class UnusedModel:
        usage = []

        def __init__(self, *args):
            pass

        def call(self, *args):
            pytest.fail('This fixture makes no model calls')

    async def checkpoint(*args):
        return {'status': 'checkpoint_ready'}, {'case_id': c['case_id']}, 'fixture'

    async def broken(*args, **kwargs):
        raise storage.StorageIOError(errno.EIO, 'persistent disk outage')

    monkeypatch.setattr(providers, 'Model', UnusedModel)
    monkeypatch.setattr(runner, 'obtain_checkpoint', checkpoint)
    monkeypatch.setattr(engine, 'run_checkpoint_episode', broken)
    with pytest.raises(storage.StorageIOError):
        asyncio.run(runner.execute(args, root, selected, ['unit']))
    folder = runner.job_path(root, jobs[0])
    assert runner.read_json(folder / 'job.json')['state'] == 'interrupted'
    assert not (folder / 'episode.json').exists()
    assert not (folder / 'metric_record.json').exists()
