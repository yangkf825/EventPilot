"""Bounded replay cleanup and Chromium checks; no model/network API calls."""
import asyncio
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import eventarena.browser as browser_module
from eventarena.browser import Browser, BrowserCleanupError, BrowserOperationTimeout


async def never():
    await asyncio.Event().wait()


def test_replay_cleanup_timeout_never_reports_live(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        browser.network_mode = 'recorded_prefix'
        browser.replay_cleanup_timeout_s = .03
        async def unroute_all(*, behavior):
            assert behavior == 'ignoreErrors'
            await never()
        browser.context = SimpleNamespace(unroute_all=unroute_all, pages=[])
        with pytest.raises(BrowserOperationTimeout, match='replay.cleanup'):
            await browser.end_checkpoint_replay()
        assert browser.network_mode == 'replay_cleanup_failed'
        assert browser.replay_cleanup_audit['completed'] is False
    asyncio.run(run())


def test_pending_replay_route_is_aborted_before_live(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        browser.network_mode = 'recorded_prefix'
        request = object()
        stages = []
        async def unroute_all(*, behavior):
            stages.append(behavior)
        async def abort():
            stages.append('abort')
            browser._finish_replay_request(request)
        browser._replay_routes[request] = SimpleNamespace(abort=abort)
        browser.context = SimpleNamespace(unroute_all=unroute_all, pages=[])
        await browser.end_checkpoint_replay()
        assert stages == ['abort', 'ignoreErrors']
        assert browser.network_mode == 'live_after_checkpoint'
        assert browser.replay_cleanup_audit == {
            'completed':True, 'pending_requests':1, 'abort_errors':0, 'remaining_requests':0}
    asyncio.run(run())


def test_route_abort_without_terminal_request_is_not_assumed_success(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        browser.network_mode = 'recorded_prefix'
        browser.replay_cleanup_timeout_s = .03
        async def unroute_all(**kwargs):
            pass
        async def abort():
            pass
        browser._replay_routes[object()] = SimpleNamespace(abort=abort)
        browser.context = SimpleNamespace(unroute_all=unroute_all, pages=[])
        with pytest.raises(BrowserOperationTimeout):
            await browser.end_checkpoint_replay()
        assert browser.network_mode == 'replay_cleanup_failed'
        assert browser.replay_cleanup_audit['remaining_requests'] == 1
    asyncio.run(run())


def test_page_timer_request_during_cleanup_cannot_start_another_har_stall(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        browser.network_mode = 'replay_cleanup'
        calls = []
        request = object()
        async def abort():
            calls.append('abort')
            browser._finish_replay_request(request)
        async def fallback():
            raise AssertionError('A cleanup request must not re-enter HAR')
        route = SimpleNamespace(request=request, abort=abort, fallback=fallback)
        await browser._track_replay_route(route)
        assert calls == ['abort']
        assert not browser._replay_routes
    asyncio.run(run())


@pytest.mark.parametrize('failed_stage', ['tracing.stop', 'context.close'])
def test_close_timeout_still_cleans_browser_and_driver(tmp_path, failed_stage):
    async def run():
        browser = Browser(tmp_path)
        browser.cleanup_timeout_s = .02
        browser.capture_flush_timeout_s = .02
        calls = []
        def operation(stage):
            async def call(**kwargs):
                calls.append(stage)
                if stage == failed_stage:
                    await never()
            return call
        browser.context = SimpleNamespace(
            unroute_all=operation('routes.remove'),
            tracing=SimpleNamespace(stop=operation('tracing.stop')),
            close=operation('context.close'))
        browser.browser = SimpleNamespace(close=operation('browser.close'))
        browser.playwright = SimpleNamespace(stop=operation('driver.stop'))
        with pytest.raises(BrowserCleanupError, match=failed_stage):
            await browser.__aexit__(None, None, None)
        assert calls == ['routes.remove', 'tracing.stop', 'context.close', 'browser.close', 'driver.stop']
        audit = json.loads((tmp_path/'browser_cleanup.json').read_text())
        assert audit['completed'] is False
        assert [s['stage'] for s in audit['steps'] if not s['ok']] == [failed_stage]
    asyncio.run(run())


def test_close_preserves_body_exception_and_records_failed_cleanup(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        async def close():
            raise RuntimeError('driver disconnected')
        browser.playwright = SimpleNamespace(stop=close)
        # When exiting an errored body, Python re-raises the body exception;
        # this method must not replace it with an unrelated cleanup error.
        await browser.__aexit__(ValueError, ValueError('original failure'), None)
        assert browser.cleanup_audit['completed'] is False
    asyncio.run(run())


def test_failed_driver_rpc_terminates_only_the_owned_child(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        calls = []
        async def stop():
            raise RuntimeError('driver RPC lost')
        def kill():
            calls.append('own_child.kill')
        async def wait():
            calls.append('own_child.wait')
            return -9
        browser.playwright = SimpleNamespace(stop=stop)
        browser._driver_process = SimpleNamespace(returncode=None, kill=kill, wait=wait)
        with pytest.raises(BrowserCleanupError, match='driver.stop'):
            await browser.__aexit__(None, None, None)
        assert calls == ['own_child.kill', 'own_child.wait']
        assert browser.cleanup_audit['steps'][-1] == {'stage':'driver.force_stop', 'ok':True}
        # The original resource failure stays visible even when the fallback
        # succeeds; it must not make an incomplete trace/capture look valid.
        assert browser.cleanup_audit['completed'] is False
    asyncio.run(run())


def test_failed_browser_launch_still_stops_playwright(tmp_path, monkeypatch):
    async def run():
        calls = []
        async def launch(**kwargs):
            raise RuntimeError('launch failed')
        async def stop():
            calls.append('driver.stop')
        driver = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=stop)
        async def start():
            return driver
        monkeypatch.setattr(browser_module, 'async_playwright', lambda: SimpleNamespace(start=start))
        browser = Browser(tmp_path)
        with pytest.raises(RuntimeError, match='launch failed'):
            await browser.__aenter__()
        assert calls == ['driver.stop']
        assert browser.cleanup_audit['completed'] is True
    asyncio.run(run())


def test_observation_evaluate_has_outer_deadline(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        browser.operation_timeout_s = .02
        async def loaded(*args):
            pass
        async def evaluate(*args):
            await never()
        browser.page = SimpleNamespace(wait_for_load_state=loaded, evaluate=evaluate)
        with pytest.raises(BrowserOperationTimeout, match='observe'):
            await browser.observe()
    asyncio.run(run())


def test_cancellation_during_context_close_still_stops_driver(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        entered = asyncio.Event()
        calls = []
        async def routes(**kwargs):
            pass
        async def trace(**kwargs):
            pass
        async def context_close():
            entered.set()
            await never()
        async def browser_close():
            calls.append('browser.close')
        async def driver_stop():
            calls.append('driver.stop')
        browser.context = SimpleNamespace(unroute_all=routes, tracing=SimpleNamespace(stop=trace), close=context_close)
        browser.browser = SimpleNamespace(close=browser_close)
        browser.playwright = SimpleNamespace(stop=driver_stop)
        closing = asyncio.create_task(browser.__aexit__(None, None, None))
        await entered.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert calls == ['browser.close', 'driver.stop']
        assert browser.cleanup_audit['completed'] is False
    asyncio.run(run())


def test_action_has_outer_deadline_even_when_backend_ignores_navigation_timeout(tmp_path):
    async def run():
        browser = Browser(tmp_path)
        browser.operation_timeout_s = .01
        async def save(*args):
            pass
        async def goto(*args, **kwargs):
            await never()
        browser.save = save
        browser.page = SimpleNamespace(goto=goto)
        with pytest.raises(BrowserOperationTimeout, match='execute.GOTO'):
            await browser.execute({'op':'GOTO', 'url':'http://example.test/reference'})
    asyncio.run(run())


@pytest.mark.browser
def test_actual_unfinished_har_request_is_drained_before_live(tmp_path):
    """A fixture models Playwright's archived status=-1 unfinished request."""
    state = {'reply':'recorded reply'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/late':
                body = state['reply'].encode()
                content_type = 'text/plain'
            else:
                body = ("<html><body><main><h1>Reference page</h1>"
                        "<p id='result'>unchanged</p></main><script>"
                        "fetch('/late').then(r=>r.text()).then(t=>document.querySelector('#result').textContent=t)"
                        ".catch(()=>{});</script></body></html>").encode()
                content_type = 'text/html'
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
            await browser.page.wait_for_function("document.querySelector('#result').textContent === 'recorded reply'")
        # Only the synthetic fixture is altered, never a dataset checkpoint.
        fixture = json.loads((tmp_path/'capture/network.har').read_text())
        late = [e for e in fixture['log']['entries'] if e['request']['url'].endswith('/late')]
        assert len(late) == 1
        late[0]['response']['status'] = -1
        har = tmp_path/'unfinished-fixture.har'
        har.write_text(json.dumps(fixture))
        state['reply'] = 'new live reply'
        async with Browser(tmp_path/'replay', headless=True) as browser:
            await browser.begin_checkpoint_replay({'mode':'recorded_prefix_then_live', 'har_path':str(har),
                                                  'sha256':hashlib.sha256(har.read_bytes()).hexdigest()})
            await browser.page.goto(url, wait_until='domcontentloaded')
            # Confirm the incomplete archive request is actually in flight.
            for _ in range(100):
                if any(r.url.endswith('/late') for r in browser._replay_routes):
                    break
                await asyncio.sleep(.01)
            assert any(r.url.endswith('/late') for r in browser._replay_routes)
            assert await browser.page.locator('#result').inner_text() == 'unchanged'
            await asyncio.wait_for(browser.end_checkpoint_replay(), timeout=3)
            assert browser.network_mode == 'live_after_checkpoint'
            assert not browser._replay_routes
            assert browser.replay_cleanup_audit['completed'] is True
            await asyncio.sleep(.05)
            assert await browser.page.locator('#result').inner_text() == 'unchanged'
            reply = await browser.page.evaluate("fetch('/late').then(r=>r.text())")
            assert reply == 'new live reply'
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.browser
def test_actual_hanging_route_does_not_block_browser_shutdown(tmp_path):
    async def run():
        entered = asyncio.Event()
        async def stalled(route):
            entered.set()
            await never()
        async def with_browser():
            async with Browser(tmp_path, headless=True) as browser:
                await browser.context.route('**/pending', stalled)
                await browser.page.set_content('<body>Ready</body>')
                await browser.page.evaluate("fetch('http://example.test/pending').catch(()=>{}); void 0")
                await asyncio.wait_for(entered.wait(), timeout=2)
        await asyncio.wait_for(with_browser(), timeout=15)
        assert json.loads((tmp_path/'browser_cleanup.json').read_text())['completed'] is True
    asyncio.run(run())
