"""Replay boundaries: owned protocol ACKs, observed events, and callback joins."""
import asyncio
import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'scripts')]

from eventarena.browser import BrowserCleanupError
from eventarena_official.browser import Browser
from eventarena_official.preparation_import import plan_import, materialize_import, KNOWN_SOURCE_CODES


class Request:
    method, url, post_data_buffer = 'GET', 'https://example.test/background', None
    def is_navigation_request(self):
        return False


class Route:
    def __init__(self, failure=False):
        self.request = Request()
        self.aborted = 0
        self.failure = failure
        self._handling_future = asyncio.get_running_loop().create_future()
        self._impl_obj = self
    def _report_handled(self, value):
        assert self._handling_future is not None
        self._handling_future.set_result(value)
        self._handling_future = None
    async def abort(self):
        if self.failure:
            raise RuntimeError('Chromium cancellation failed')
        self.aborted += 1
        self._report_handled(True)
    async def fallback(self):
        pytest.fail('Archive route ownership cannot be handed away')


def browser_fixture(tmp_path):
    browser = Browser(tmp_path)
    browser.network_mode = 'recorded_prefix'
    handlers = []
    async def unroute_all(*, behavior):
        assert behavior == 'wait'
        await asyncio.gather(*handlers)
    browser.context = SimpleNamespace(pages=[], unroute_all=unroute_all)
    async def native_handle(route):
        route._report_handled(True)
    browser._owned_har_router = SimpleNamespace(_handle=native_handle, dispose=lambda: None)
    return browser, handlers


def test_native_terminal_ack_does_not_require_requestfinished_event(tmp_path):
    async def run():
        b, _ = browser_fixture(tmp_path)
        route = Route()
        await b._track_replay_route(route)
        assert not b._replay_routes and route._handling_future is None
        await b.end_checkpoint_replay()
        assert b.replay_cleanup_audit['completed'] is True
        assert b.replay_cleanup_audit['native_har_terminal_acknowledgments'] == 1
        assert route.aborted == 0
    asyncio.run(run())


def test_real_terminal_event_resolves_incomplete_handler_bookkeeping(tmp_path):
    async def run():
        b, handlers = browser_fixture(tmp_path)
        route = Route()
        b._replay_unfinished_keys.add(('GET', route.request.url, b''))
        handler = asyncio.create_task(b._track_replay_route(route)); handlers.append(handler)
        await asyncio.sleep(0)
        b._finish_replay_request(route.request)
        # The old bug removed the route here and stranded the dispatch future.
        assert route.request in b._replay_routes and not handler.done()
        await asyncio.wait_for(b.end_checkpoint_replay(), 1)
        assert route._handling_future is None and handler.done()
        assert b.replay_cleanup_audit['observed_terminal_event_releases'] == 1
        assert route.aborted == 0
    asyncio.run(run())


def test_stalled_owned_request_requires_actual_abort_ack(tmp_path):
    async def run():
        b, handlers = browser_fixture(tmp_path)
        route = Route()
        b._replay_unfinished_keys.add(('GET', route.request.url, b''))
        handler = asyncio.create_task(b._track_replay_route(route)); handlers.append(handler)
        await asyncio.sleep(0)
        await asyncio.wait_for(b.end_checkpoint_replay(), 1)
        assert handler.done() and route.aborted == 1
        assert b.replay_cleanup_audit['owned_route_abort_acknowledgments'] == 1
        assert b.replay_cleanup_audit['observed_terminal_event_releases'] == 0
    asyncio.run(run())


def test_failed_abort_cannot_certify_live_boundary(tmp_path):
    async def run():
        b, handlers = browser_fixture(tmp_path)
        route = Route(failure=True)
        b._replay_unfinished_keys.add(('GET', route.request.url, b''))
        handler = asyncio.create_task(b._track_replay_route(route)); handlers.append(handler)
        await asyncio.sleep(0)
        try:
            with pytest.raises(BrowserCleanupError):
                await b.end_checkpoint_replay()
            assert b.network_mode == 'replay_cleanup_failed'
            assert b.replay_cleanup_audit['completed'] is False
            assert route.request in b._replay_routes
        finally:
            handler.cancel(); await asyncio.gather(handler, return_exceptions=True)
    asyncio.run(run())


def test_cleanup_waits_for_inflight_native_handle_without_double_abort(tmp_path):
    async def run():
        b, handlers = browser_fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        async def native_handle(route):
            entered.set(); await release.wait(); route._report_handled(True)
        b._owned_har_router._handle = native_handle
        route = Route()
        handler = asyncio.create_task(b._track_replay_route(route)); handlers.append(handler)
        await entered.wait()
        ending = asyncio.create_task(b.end_checkpoint_replay())
        await asyncio.sleep(0)
        assert not ending.done()
        release.set(); await asyncio.wait_for(ending, 1)
        assert handler.done() and route.aborted == 0
        assert b.network_mode == 'live_after_checkpoint'
    asyncio.run(run())


def import_fixture(tmp_path):
    source = tmp_path / 'source'; folder = source / 'checkpoints/case'; folder.mkdir(parents=True)
    archive = folder / 'network.har'; archive.write_text('immutable factual archive')
    capture = {'mode': 'recorded_prefix_then_live', 'har_path': str(archive),
               'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    (folder / 'episode.json').write_text(json.dumps({'status': 'checkpoint_ready',
                                                    'checkpoint': {'environment_capture': capture}}))
    manifest = {'protocol_version': 'official_online_eventarena_v4',
        'code_sha256': next(iter(KNOWN_SOURCE_CODES)), 'dataset_sha256': 'cases',
        'official_source': {'revision': 'same'}, 'context_access_policy': 'same', 'context_read_limits': {},
        'arguments': {'prep_model': 'actor', 'judge_model': 'judge'},
        'model_configs': {'actor': {'model': 'fixture'}, 'judge': {'model': 'fixture'}}, 'jobs': []}
    (source / 'manifest.json').write_text(json.dumps(manifest))
    target = copy.deepcopy(manifest); target['protocol_version'] = 'official_online_eventarena_v5'
    return source, target


def test_import_reuses_only_immutable_prefixes_not_eligibility(tmp_path):
    source, target = import_fixture(tmp_path)
    plan = plan_import(source, target); root = tmp_path / 'new'
    materialize_import(root, plan); materialize_import(root, plan)
    assert (root / 'checkpoints/case/episode.json').read_bytes() == (source / 'checkpoints/case/episode.json').read_bytes()
    assert not (root / 'preparation_eligibility').exists()
    assert plan['all_reset_audits_required'] and plan['reuse_tested_actor_results'] is False
    assert plan['reuse_eligibility'] is False
    (source / 'checkpoints/case/network.har').write_text('changed')
    with pytest.raises(ValueError, match='altered'):
        plan_import(source, target)


@pytest.mark.parametrize('field', ['dataset_sha256', 'context_access_policy', 'prep_model', 'model'])
def test_import_rejects_changed_cases_information_or_fixed_models(tmp_path, field):
    source, target = import_fixture(tmp_path)
    if field == 'prep_model': target['arguments']['prep_model'] = 'another'
    elif field == 'model': target['model_configs']['actor']['model'] = 'another'
    else: target[field] = 'changed'
    with pytest.raises(ValueError, match='differs|identical'):
        plan_import(source, target)


def test_import_refuses_a_source_after_tested_actors_have_started(tmp_path):
    source, target = import_fixture(tmp_path)
    path = source / 'manifest.json'; manifest = json.loads(path.read_text())
    manifest['jobs'] = [{'experiment': 'main', 'model': 'actor', 'view': 'trajectory',
                         'repeat': 0, 'case_id': 'case'}]
    path.write_text(json.dumps(manifest))
    folder = source / 'main/actor/episodes/trajectory/repeat_0/case'; folder.mkdir(parents=True)
    (folder / 'job.json').write_text('{"state":"running"}')
    with pytest.raises(ValueError, match='no tested actors'):
        plan_import(source, target)
