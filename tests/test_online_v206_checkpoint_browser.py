"""Incomplete HAR ownership and exact structural control identity; no APIs."""
import asyncio
import copy
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from eventarena.browser import Browser, BrowserOperationTimeout


def entry(url, status, body='', method='GET'):
    request = {'url':url, 'method':method}
    if body:
        request['postData'] = {'text':body}
    return {'request':request, 'response':{'status':status, 'content':{'text':'unchanged task response'}}}


def archive(tmp_path, entries):
    path = tmp_path/'recorded.har'
    value = {'log':{'entries':entries, 'creator':{'name':'fixture'}}}
    path.write_text(json.dumps(value), encoding='utf-8')
    return path, path.read_bytes(), hashlib.sha256(path.read_bytes()).hexdigest()


def test_complete_har_stays_byte_identical_without_unneeded_derived_file(tmp_path):
    path, before, digest = archive(tmp_path, [entry('https://example.test/task', 200)])
    browser = Browser(tmp_path)
    assert browser._prepare_replay_archive(path, digest) == path
    assert path.read_bytes() == before
    assert not (tmp_path/'checkpoint_replay.har').exists()
    assert not browser._replay_unfinished_keys


def test_incomplete_responses_removed_without_rewriting_any_complete_response(tmp_path):
    complete = entry('https://example.test/task', 200)
    incomplete = entry('https://example.test/pending', -1)
    path, before, digest = archive(tmp_path, [complete, incomplete])
    browser = Browser(tmp_path)
    derived = browser._prepare_replay_archive(path, digest)
    assert path.read_bytes() == before
    assert json.loads(derived.read_text())['log']['entries'] == [complete]
    assert browser._replay_unfinished_keys == {('GET', incomplete['request']['url'], b'')}
    audit = json.loads((tmp_path/'checkpoint_replay_archive_audit.json').read_text())
    assert audit['registered_sha256'] == digest
    assert audit['replay_sha256'] == hashlib.sha256(derived.read_bytes()).hexdigest()
    assert audit['unfinished_entry_count'] == 1


def test_completed_alternative_remains_available_and_post_bodies_are_distinct(tmp_path):
    url = 'https://example.test/api'
    path, before, digest = archive(tmp_path, [entry(url, 200, 'alpha', 'POST'),
        entry(url, -1, 'alpha', 'POST'), entry(url, -1, 'beta', 'POST')])
    browser = Browser(tmp_path)
    browser._prepare_replay_archive(path, digest)
    assert path.read_bytes() == before
    assert browser._replay_unfinished_keys == {('POST', url, b'beta')}


def test_unfinished_owned_route_is_released_only_by_terminal_request_event(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        browser.network_mode = 'recorded_prefix'
        browser._replay_unfinished_keys = {('GET', 'https://example.test/pending', b'')}
        request = SimpleNamespace(method='GET', url='https://example.test/pending', post_data_buffer=None,
                                  is_navigation_request=lambda:False)
        # Requests are dictionary keys in the real Playwright API.
        class Request:
            method, url, post_data_buffer = request.method, request.url, None
            def is_navigation_request(self):
                return False
        actual = Request()
        async def fallback():
            raise AssertionError('An incomplete response cannot be handed to the stalled HAR route')
        route = SimpleNamespace(request=actual, fallback=fallback)
        pending = asyncio.create_task(browser._track_replay_route(route))
        await asyncio.sleep(0)
        assert browser._replay_routes[actual] is route
        assert not pending.done()
        browser._finish_replay_request(actual)
        await asyncio.wait_for(pending, timeout=1)
        assert not browser._replay_routes
        assert not browser._replay_waiters
    asyncio.run(run())


def test_incomplete_navigation_aborts_instead_of_waiting_for_domcontentloaded(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        browser.network_mode = 'recorded_prefix'
        browser._replay_unfinished_keys = {('GET', 'https://example.test/task', b'')}
        class Request:
            method, url, post_data_buffer = 'GET', 'https://example.test/task', None
            def is_navigation_request(self):
                return True
        request = Request()
        called = []
        async def abort():
            called.append('abort')
            browser._finish_replay_request(request)
        async def fallback():
            raise AssertionError('Unavailable navigation cannot stall in HAR')
        await browser._track_replay_route(SimpleNamespace(request=request, abort=abort, fallback=fallback))
        assert called == ['abort']
        assert not browser._replay_routes
    asyncio.run(run())


def test_abort_ack_releases_handler_before_waiting_for_real_terminal_event(tmp_path):
    """Some Chromium requests emit requestfailed only after their route exits."""
    async def run():
        browser = Browser(tmp_path)
        browser.network_mode = 'recorded_prefix'
        browser._replay_unfinished_keys = {('GET', 'https://example.test/pending', b'')}
        browser.context = SimpleNamespace(pages=[], unroute_all=None)
        unroute_calls = []
        async def unroute_all(**kwargs):
            assert not browser._replay_routes
            unroute_calls.append(kwargs)
        browser.context.unroute_all = unroute_all
        class Request:
            method, url, post_data_buffer = 'GET', 'https://example.test/pending', None
            def is_navigation_request(self):
                return False
        request = Request()
        acknowledged = asyncio.Event()
        async def abort():
            acknowledged.set()
            # No terminal event yet: the route callback must return first.
        route = SimpleNamespace(request=request, abort=abort)
        handler = asyncio.create_task(browser._track_replay_route(route))
        await asyncio.sleep(0)
        async def terminal_after_handler():
            await handler
            assert acknowledged.is_set()
            browser._finish_replay_request(request)
        terminal = asyncio.create_task(terminal_after_handler())
        await asyncio.wait_for(browser.end_checkpoint_replay(), timeout=1)
        await terminal
        assert browser.network_mode == 'live_after_checkpoint'
        assert browser.replay_cleanup_audit['completed'] is True
        assert unroute_calls == [{'behavior':'ignoreErrors'}]
    asyncio.run(run())


def test_owned_abort_ack_cancels_request_without_assuming_requestfailed_exists(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        browser.network_mode = 'recorded_prefix'
        browser._replay_unfinished_keys = {('GET', 'https://example.test/pending', b'')}
        async def unroute_all(**kwargs):
            assert not browser._replay_routes
        browser.context = SimpleNamespace(pages=[], unroute_all=unroute_all)
        class Request:
            method, url, post_data_buffer = 'GET', 'https://example.test/pending', None
            def is_navigation_request(self):
                return False
        request = Request()
        async def abort():
            pass  # Successful protocol ACK; no terminal callback will follow.
        handler = asyncio.create_task(browser._track_replay_route(SimpleNamespace(request=request, abort=abort)))
        await asyncio.sleep(0)
        await asyncio.wait_for(browser.end_checkpoint_replay(), timeout=1)
        await handler
        assert not browser._replay_routes
        assert not browser._replay_waiters
        assert browser.replay_cleanup_audit['owned_route_abort_acknowledgments'] == 1
        assert browser.replay_cleanup_audit['cancellation_evidence'] == 'owned_interception_abort_ack_or_terminal_event'
        assert browser.network_mode == 'live_after_checkpoint'
    asyncio.run(run())


def test_failed_owned_abort_never_proves_cancellation_or_clears_pending_request(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        browser.network_mode = 'recorded_prefix'
        browser.replay_cleanup_timeout_s = .03
        browser._replay_unfinished_keys = {('GET', 'https://example.test/pending', b'')}
        async def unroute_all(**kwargs):
            raise AssertionError('Cannot enter live browsing with an unverified pending request')
        browser.context = SimpleNamespace(pages=[], unroute_all=unroute_all)
        class Request:
            method, url, post_data_buffer = 'GET', 'https://example.test/pending', None
            def is_navigation_request(self):
                return False
        request = Request()
        async def abort():
            raise RuntimeError('Protocol abort failed')
        handler = asyncio.create_task(browser._track_replay_route(SimpleNamespace(request=request, abort=abort)))
        await asyncio.sleep(0)
        try:
            with pytest.raises(BrowserOperationTimeout):
                await browser.end_checkpoint_replay()
            assert request in browser._replay_routes
            assert request in browser._replay_waiters
            assert browser._replay_abort_acknowledgments == 0
            assert browser.replay_cleanup_audit['completed'] is False
            assert browser.replay_cleanup_audit['abort_errors'] == 1
            assert browser.network_mode == 'replay_cleanup_failed'
        finally:
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
    asyncio.run(run())


def test_shutdown_reaps_only_callbacks_of_this_context_and_leaves_other_tasks_running(tmp_path):
    async def run():
        class Implementation:
            async def _on_route(self):
                await asyncio.Event().wait()
        own, other = Implementation(), Implementation()
        owner_task = asyncio.create_task(own._on_route())
        other_task = asyncio.create_task(other._on_route())
        unrelated = asyncio.create_task(asyncio.Event().wait())
        browser = Browser(tmp_path)
        closed = []
        async def operation(*args, **kwargs):
            closed.append('operation')
        browser.context = SimpleNamespace(_impl_obj=own, unroute_all=operation,
            tracing=SimpleNamespace(stop=operation), close=operation)
        browser.playwright = SimpleNamespace(stop=operation)
        await asyncio.sleep(0)
        try:
            browser._remember_owned_route_callback_tasks()
            assert browser._owned_route_callback_tasks == {owner_task}
            await browser._close_resources()
            assert owner_task.cancelled()
            assert not other_task.done()
            assert not unrelated.done()
            assert len(closed) == 4  # All resources closed before callback reaping.
            assert browser.cleanup_audit['completed'] is True
            reaped = [s for s in browser.cleanup_audit['steps'] if s['stage']=='route_callbacks.cancel']
            assert reaped == [{'stage':'route_callbacks.cancel', 'ok':True, 'count':1}]
        finally:
            for task in (owner_task, other_task, unrelated):
                task.cancel()
            await asyncio.gather(owner_task, other_task, unrelated, return_exceptions=True)
    asyncio.run(run())


def test_unverified_driver_shutdown_cannot_cancel_owned_route_dispatcher(tmp_path):
    async def run():
        class Implementation:
            async def _on_route(self):
                await asyncio.Event().wait()
        own = Implementation()
        owner_task = asyncio.create_task(own._on_route())
        browser = Browser(tmp_path)
        async def okay(*args, **kwargs):
            pass
        async def failed(*args, **kwargs):
            raise RuntimeError('Driver/context shutdown unavailable')
        browser.context = SimpleNamespace(_impl_obj=own, unroute_all=okay,
            tracing=SimpleNamespace(stop=okay), close=failed)
        browser.playwright = SimpleNamespace(stop=failed)
        await asyncio.sleep(0)
        try:
            browser._remember_owned_route_callback_tasks()
            await browser._close_resources()
            assert not owner_task.done()
            assert browser.cleanup_audit['completed'] is False
            reaped = [s for s in browser.cleanup_audit['steps'] if s['stage']=='route_callbacks.cancel']
            assert reaped[0]['ok'] is False
        finally:
            owner_task.cancel()
            await asyncio.gather(owner_task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.browser
def test_duplicate_semantic_controls_have_distinct_stable_structural_paths(tmp_path):
    async def run():
        async with Browser(tmp_path, headless=True) as browser:
            await browser.page.set_content('<html><body><nav><a href="/watch">Watch</a></nav>'
                                           '<main>Task content</main><footer><a href="/watch">Watch</a></footer></body></html>')
            first = await browser.observe()
            links = [c for c in first['candidates'] if c['text']=='Watch']
            assert len(links) == 2
            assert links[0]['dom_path'] != links[1]['dom_path']
            assert [c['dom_path'] for c in links] == [
                'html:nth-of-type(1)>body:nth-of-type(1)>nav:nth-of-type(1)>a:nth-of-type(1)',
                'html:nth-of-type(1)>body:nth-of-type(1)>footer:nth-of-type(1)>a:nth-of-type(1)']
            before = copy.deepcopy(links)
            await browser.page.evaluate("document.querySelector('a').setAttribute('data-ea-node','999')")
            after = await browser.observe()
            assert [c['dom_path'] for c in after['candidates']] == [c['dom_path'] for c in before]
    asyncio.run(run())


@pytest.mark.browser
def test_multiple_incomplete_archive_fetches_are_cancelled_without_live_fallback(tmp_path):
    state = {'hits':0, 'reply':'recorded'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith('/pending'):
                state['hits'] += 1
                body = state['reply'].encode()
                content_type = 'text/plain'
            else:
                body = ("<body><p id='result'>waiting</p><script>window.finished=0;"
                        "for(let i=0;i<3;i++)fetch('/pending?i='+i).then(r=>r.text()).then(t=>{"
                        "window.finished++;document.querySelector('#result').textContent=t;}).catch(()=>{});"
                        "</script></body>").encode()
                content_type = 'text/html; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/reference'
    async def run():
        async with Browser(tmp_path/'capture', headless=True) as browser:
            await browser.page.goto(url, wait_until='domcontentloaded')
            await browser.page.wait_for_function('window.finished===3')
        assert state['hits'] == 3
        fixture = json.loads((tmp_path/'capture/network.har').read_text())
        unfinished = [e for e in fixture['log']['entries'] if '/pending?' in e['request']['url']]
        assert len(unfinished) == 3
        for entry in unfinished:
            entry['response']['status'] = -1
        har = tmp_path/'unfinished_fixture.har'
        har.write_text(json.dumps(fixture))
        state['reply'] = 'fresh live response'
        async with Browser(tmp_path/'replay', headless=True) as browser:
            await browser.begin_checkpoint_replay({'mode':'recorded_prefix_then_live',
                'har_path':str(har), 'sha256':hashlib.sha256(har.read_bytes()).hexdigest()})
            await browser.page.goto(url, wait_until='domcontentloaded')
            for _ in range(100):
                if len(browser._replay_waiters) == 3:
                    break
                await asyncio.sleep(.01)
            assert len(browser._replay_waiters) == 3
            assert await browser.page.locator('#result').inner_text() == 'waiting'
            # Verify that an explicit abort ACK is sufficient even if a
            # Chromium/driver terminal-event observer is absent. Successful
            # complete responses still require their normal finished events.
            browser.context.remove_listener('requestfailed', browser._finish_replay_request)
            await asyncio.wait_for(browser.end_checkpoint_replay(), timeout=3)
            assert browser.replay_cleanup_audit['completed'] is True
            assert not browser._replay_routes
            assert not browser._replay_waiters
            assert browser.replay_cleanup_audit['owned_route_abort_acknowledgments'] == 3
            assert state['hits'] == 3
            assert await browser.page.locator('#result').inner_text() == 'waiting'
            reply = await browser.page.evaluate("fetch('/pending?i=0').then(r=>r.text())")
            assert reply == 'fresh live response'
            assert state['hits'] == 4
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
