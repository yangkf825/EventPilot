"""Common preparation visibility and infrastructure deadlines, with no API calls."""
import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Existing runner tests use the same scripts import path.
import sys
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
preparation = importlib.import_module('prepare_online_v2')
runner = importlib.import_module('run_online_v2')


def _cases(tmp_path, cases):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'single_event.jsonl').write_text(''.join(json.dumps(c) + '\n' for c in cases))
    return data


def _args(data):
    return SimpleNamespace(data_dir=data, repeats=1, prep_model='common-preparer', judge_model=None,
                           retry_infra=False, headless=True, max_steps=10, pre_steps=10,
                           setup_steps=10, max_context_chars=10000, loop_limit=4, format_retries=1)


@pytest.fixture
def fake_browser_engine(monkeypatch):
    from eventarena import browser as browser_module, online_v2_engine as engine_module
    lifecycles = []

    class FakeBrowser:
        def __init__(self, folder, headless=False):
            self.case_id = Path(folder).parent.name

        async def __aenter__(self):
            lifecycles.append(('enter', self.case_id))
            return self

        async def __aexit__(self, *exc):
            lifecycles.append(('exit', self.case_id))

    class FakeEngine:
        def __init__(self, case, model, folder, opts):
            self.case, self.model, self.opts = case, model, opts
            self.folder = folder
            self.episode = {'status': 'started'}

        def persist(self):
            runner.write_json(self.folder / 'episode.json', self.episode)

    async def restore(engine, browser, checkpoint):
        with pytest.raises(AssertionError, match='must never call a tested model'):
            engine.model.call([])
        return {'matches': True, 'available': True}, {}

    monkeypatch.setattr(browser_module, 'Browser', FakeBrowser)
    monkeypatch.setattr(engine_module, 'AutonomousEpisode', FakeEngine)
    monkeypatch.setattr(engine_module, 'restore_checkpoint', restore)
    return lifecycles, engine_module, browser_module


async def _checkpoint(case, repeat, pool, root, model, opts, locks):
    opts['progress']({'stage': 'actor', 'state': 'calling', 'messages': 'private prompt',
                      'gold': {'decision': 'IGNORE'}, 'api_key': 'private credential'})
    opts['progress']({'stage': 'actor', 'state': 'returned', 'api_ok': True})
    return {'status': 'checkpoint_ready'}, {'state': {}, 'prepared': True}, case['case_id']


def test_preparation_start_api_reset_and_cached_progress_are_visible(tmp_path, fake_browser_engine, capsys):
    case = {'case_id': 'X', 'gold': {'decision': 'INTERRUPT'}}
    args = _args(_cases(tmp_path, [case]))
    root = tmp_path / 'run'
    selected = {'main': [case], 'ablation': [case]}
    gates = asyncio.run(preparation.prepare_pool(args, root, selected, {'common-preparer': object()}, {}, _checkpoint))
    assert gates[('X', 0)]['eligible'] is True
    state = runner.read_json(root / 'preparation_state.json')
    assert (state['planned'], state['completed'], state['eligible']) == (1, 1, 1)
    assert state['state'] == 'finished'
    assert state['selection_reads_tested_model_results'] is False
    records = [json.loads(line) for line in (root / 'preparation_progress.jsonl').read_text().splitlines()]
    assert {(r['stage'], r['state']) for r in records} >= {
        ('case', 'START'), ('actor', 'calling'), ('actor', 'returned'),
        ('reset_audit', 'calling'), ('reset_audit', 'returned'), ('case', 'DONE')}
    logs = (root / 'progress.jsonl').read_text()
    assert 'private prompt' not in logs and 'private credential' not in logs and 'gold' not in logs
    assert 'START' in capsys.readouterr().out

    async def never_prepare(*args):
        raise AssertionError('Cached eligibility must not call any model or browser')

    cached = asyncio.run(preparation.prepare_pool(args, root, selected, {}, {}, never_prepare))
    assert cached == gates
    assert 'CACHED' in capsys.readouterr().out
    assert fake_browser_engine[0] == [('enter', 'X'), ('exit', 'X')]


def test_reset_timeout_is_unknown_and_next_case_runs_with_heartbeat(tmp_path, fake_browser_engine, monkeypatch, capsys):
    cases = [{'case_id': 'A'}, {'case_id': 'B'}]
    args = _args(_cases(tmp_path, cases))
    _, engine_module, _ = fake_browser_engine

    async def restore(engine, browser, cp):
        if engine.case['case_id'] == 'A':
            await asyncio.sleep(60)
        return {'matches': True, 'available': True}, {}

    monkeypatch.setattr(engine_module, 'restore_checkpoint', restore)
    monkeypatch.setattr(preparation, 'RESET_AUDIT_TIMEOUT_SECONDS', 0.04)
    monkeypatch.setattr(preparation, 'HEARTBEAT_SECONDS', 0.005)
    root = tmp_path / 'run'
    gates = asyncio.run(preparation.prepare_pool(args, root, {'main': cases}, {'common-preparer': object()}, {}, _checkpoint))
    row = gates[('A', 0)]
    assert row['reason'] == 'reset_audit_timeout'
    assert row['eligible'] is False and row['preparation_failure_is_model'] is False
    assert row['reset_audit']['matches'] is None and row['reset_audit']['available'] is False
    assert gates[('B', 0)]['eligible'] is True
    episode = runner.read_json(root / 'preparation_eligibility/repeat_0/A/reset_audit/episode.json')
    assert episode['status'] == 'browser_error' and episode['preparation_failure_is_model'] is False
    assert episode['checkpoint_replay_audit']['matches'] is None
    records = [json.loads(line) for line in (root / 'preparation_progress.jsonl').read_text().splitlines()]
    assert any(r['stage'] == 'heartbeat' and r['last_stage'] == 'reset_audit' for r in records)
    assert fake_browser_engine[0] == [('enter', 'A'), ('exit', 'A'), ('enter', 'B'), ('exit', 'B')]
    assert 'waiting reset_audit/calling' in capsys.readouterr().out


def test_reset_deadline_includes_browser_close(tmp_path, fake_browser_engine, monkeypatch):
    case = {'case_id': 'X'}
    args = _args(_cases(tmp_path, [case]))
    _, _, browser_module = fake_browser_engine

    class CleanupBlockedBrowser:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            await asyncio.sleep(60)

    monkeypatch.setattr(browser_module, 'Browser', CleanupBlockedBrowser)
    monkeypatch.setattr(preparation, 'RESET_AUDIT_TIMEOUT_SECONDS', 0.02)
    root = tmp_path / 'run'
    gates = asyncio.run(preparation.prepare_pool(args, root, {'main': [case]}, {'common-preparer': object()}, {}, _checkpoint))
    assert gates[('X', 0)]['reason'] == 'reset_audit_timeout'
    assert gates[('X', 0)]['eligible'] is False


def test_timeout_keeps_counterfactual_group_gate_and_reason_on_resume(tmp_path, fake_browser_engine, monkeypatch):
    cases = [{'case_id': 'A', 'group_id': 'G'}, {'case_id': 'B', 'group_id': 'G'}]
    args = _args(_cases(tmp_path, cases))
    _, engine_module, _ = fake_browser_engine

    async def restore(engine, browser, cp):
        if engine.case['case_id'] == 'A':
            await asyncio.sleep(60)
        return {'matches': True, 'available': True}, {}

    monkeypatch.setattr(engine_module, 'restore_checkpoint', restore)
    monkeypatch.setattr(preparation, 'RESET_AUDIT_TIMEOUT_SECONDS', 0.02)
    root = tmp_path / 'run'
    selected = {'counterfactual': cases}
    gates = asyncio.run(preparation.prepare_pool(args, root, selected, {'common-preparer': object()}, {}, _checkpoint))
    assert all(r['eligible'] is False and r['reason'] == 'controlled_group_preparation_incomplete' for r in gates.values())
    assert gates[('A', 0)]['individual_preparation_reason'] == 'reset_audit_timeout'
    assert gates[('B', 0)]['individual_preparation_reason'] == 'reset_verified'
    cached = asyncio.run(preparation.prepare_pool(args, root, selected, {}, {}, _checkpoint))
    assert cached == gates
    assert runner.read_json(root / 'preparation_state.json')['eligible'] == 0


def test_short_browser_deadline_is_not_reported_as_outer_120s_timeout(tmp_path, fake_browser_engine, monkeypatch):
    from eventarena.browser import BrowserOperationTimeout
    case = {'case_id': 'X'}
    args = _args(_cases(tmp_path, [case]))
    _, engine_module, _ = fake_browser_engine

    async def restore(*args):
        raise BrowserOperationTimeout('replay.cleanup exceeded 10s')

    monkeypatch.setattr(engine_module, 'restore_checkpoint', restore)
    root = tmp_path / 'run'
    gates = asyncio.run(preparation.prepare_pool(args, root, {'main': [case]},
                                                {'common-preparer': object()}, {}, _checkpoint))
    row = gates[('X', 0)]
    assert row['reason'] == 'reset_audit_error'
    assert row['error_type'] == 'BrowserOperationTimeout'
    assert row['preparation_failure_is_model'] is False
    assert row['reset_audit']['available'] is False
    assert 'timeout_s' not in row['reset_audit']


def test_preparation_exception_does_not_block_next_registered_case(tmp_path, fake_browser_engine):
    cases = [{'case_id': 'A'}, {'case_id': 'B'}]
    args = _args(_cases(tmp_path, cases))

    async def checkpoint(case, *other):
        if case['case_id'] == 'A':
            raise OSError('infrastructure unavailable')
        return await _checkpoint(case, *other)

    root = tmp_path / 'run'
    gates = asyncio.run(preparation.prepare_pool(args, root, {'main': cases}, {'common-preparer': object()}, {}, checkpoint))
    assert gates[('A', 0)]['reason'] == 'common_preparation_error'
    assert gates[('A', 0)]['preparation_failure_is_model'] is False
    assert gates[('B', 0)]['eligible'] is True


def test_status_shows_common_preparation_while_all_jobs_are_pending(tmp_path):
    jobs = runner.make_jobs({'main': [{'case_id': 'X'}]}, ['model'], 1, 2)
    runner.write_json(tmp_path / 'manifest.json', {'jobs': jobs})
    logger = preparation.PreparationProgress(tmp_path, 1, 'common-preparer', None)
    logger.begin('X', 0, 1)
    logger.progress({'stage': 'reset_audit', 'state': 'calling'})
    state = runner.status(tmp_path)
    assert state['states'] == {'pending': 1}
    assert state['phase'] == 'preparation'
    assert state['preparation']['current_case']['case_id'] == 'X'
    assert state['preparation']['last_progress']['stage'] == 'reset_audit'
    logger.finish([{'eligible': True}])
    assert runner.status(tmp_path)['phase'] == 'evaluation'
    runner.write_json(runner.job_path(tmp_path, jobs[0]) / 'job.json', {'state': 'finished'})
    assert runner.status(tmp_path)['phase'] == 'finished'


def test_user_cancellation_is_preserved_and_visible(tmp_path, fake_browser_engine, monkeypatch):
    case = {'case_id': 'X'}
    args = _args(_cases(tmp_path, [case]))
    started = asyncio.Event()

    async def checkpoint(*args):
        started.set()
        await asyncio.sleep(60)

    async def cancel_prepare():
        task = asyncio.create_task(preparation.prepare_pool(args, tmp_path / 'run', {'main': [case]},
                                                            {'common-preparer': object()}, {}, checkpoint))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_prepare())
    state = runner.read_json(tmp_path / 'run/preparation_state.json')
    assert state['state'] == 'interrupted' and state['completed'] == 0
    assert not (tmp_path / 'run/preparation_eligibility/repeat_0/X/eligibility.json').exists()


def test_unavailable_audit_is_not_reported_as_a_state_mismatch(tmp_path, fake_browser_engine, monkeypatch):
    case = {'case_id': 'X'}
    args = _args(_cases(tmp_path, [case]))
    _, engine_module, _ = fake_browser_engine

    async def restore(engine, browser, cp):
        return {'matches': None, 'available': False, 'reason': 'registered_network_capture_missing'}, {}

    monkeypatch.setattr(engine_module, 'restore_checkpoint', restore)
    root = tmp_path / 'run'
    gates = asyncio.run(preparation.prepare_pool(args, root, {'main': [case]}, {'common-preparer': object()}, {}, _checkpoint))
    row = gates[('X', 0)]
    assert row['eligible'] is False and row['reason'] == 'reset_audit_unavailable'
    assert row['reset_audit']['reason'] == 'registered_network_capture_missing'
    episode = runner.read_json(root / 'preparation_eligibility/repeat_0/X/reset_audit/episode.json')
    assert episode['status'] == 'checkpoint_projection_unavailable'
