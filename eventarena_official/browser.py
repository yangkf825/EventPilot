"""Owned HAR replay using Playwright's unchanged native archive matcher.

The pinned Playwright adapter avoids handing a Route away via fallback. Every
archive fulfill/redirect/abort is acknowledged before the live boundary; terminal
request events are supplementary, not the sole evidence for owned interceptions.
"""
import asyncio
import copy
import hashlib
from pathlib import Path

from eventarena.browser import Browser as BaseBrowser, BrowserCleanupError

REPLAY_POLICY = 'owned_native_har_replay_v1'


class Browser(BaseBrowser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._owned_har_router = None
        self._owned_replay_locks = {}
        self._owned_replay_errors = []
        self._owned_har_acknowledgments = 0
        self._owned_terminal_events = set()
        self._terminal_event_releases = 0

    def _finish_replay_request(self, request):
        # A network event can arrive while an owned route callback still awaits
        # its handling future. Retain that interception until the callback is
        # also resolved; dropping it here strands unroute_all(behavior='wait').
        if request in self._replay_routes:
            self._owned_terminal_events.add(request)

    def _release_owned_replay_request(self, request):
        super()._finish_replay_request(request)
        self._owned_terminal_events.discard(request)

    async def _abort_owned_locked(self, route):
        if route.request not in self._replay_routes:
            return
        if route.request in self._owned_terminal_events:
            # Chromium already emitted requestfinished/requestfailed for this
            # exact request. No response can arrive later. Resolve only the
            # Python routing bookkeeping which Playwright otherwise leaves
            # awaiting an action on a request that no longer exists.
            impl = route._impl_obj
            if impl._handling_future is not None:
                impl._report_handled(True)
            self._terminal_event_releases += 1
            self._release_owned_replay_request(route.request)
        else:
            await super()._abort_owned_replay_route(route)
            self._owned_terminal_events.discard(route.request)

    async def begin_checkpoint_replay(self, capture):
        if capture.get('mode') != 'recorded_prefix_then_live':
            raise ValueError('Unsupported checkpoint network reset mode')
        har = Path(capture.get('har_path', ''))
        if not har.is_file() or hashlib.sha256(har.read_bytes()).hexdigest() != capture.get('sha256'):
            raise ValueError('Registered checkpoint network archive missing or changed')
        # Use the same matcher as context.route_from_har: URL, method, headers,
        # request body, redirects and original response bytes remain unchanged.
        # This adapter is covered against the locked Playwright version in CI.
        from playwright._impl._har_router import HarRouter
        implementation = self.context._impl_obj
        try:
            replay = self._prepare_replay_archive(har, capture['sha256'])
            self._owned_har_router = await self._bounded(HarRouter.create(
                local_utils=implementation._connection.local_utils,
                file=str(replay), not_found_action='abort'), 'replay.archive.open')
            await self._bounded(self.context.route('**/*', self._track_replay_route),
                                'replay.owned_handler.install')
            self.network_mode = 'recorded_prefix'
        except BaseException:
            self.network_mode = 'replay_setup_failed'
            raise

    async def _abort_owned_replay_route(self, route):
        lock = self._owned_replay_locks.setdefault(route.request, asyncio.Lock())
        async with lock:
            # A concurrent owned fulfill or terminal event may have completed it.
            if route.request not in self._replay_routes:
                return
            await self._abort_owned_locked(route)

    async def _track_replay_route(self, route):
        self._remember_owned_route_callback_tasks()
        request = route.request
        self._replay_routes[request] = route
        lock = self._owned_replay_locks.setdefault(request, asyncio.Lock())
        try:
            if self.network_mode == 'replay_cleanup':
                await self._abort_owned_replay_route(route)
            elif self._request_replay_key(request) in self._replay_unfinished_keys:
                if request.is_navigation_request():
                    await self._abort_owned_replay_route(route)
                else:
                    # Preserve the recorded incomplete load until cleanup. Its
                    # bytes never become task content; no live fallback is used.
                    waiter = asyncio.Event()
                    self._replay_waiters[request] = waiter
                    await waiter.wait()
            else:
                async with lock:
                    if request not in self._replay_routes:
                        return
                    if self.network_mode == 'replay_cleanup':
                        await self._abort_owned_locked(route)
                    else:
                        # Unlike fallback, this handler retains the very Route
                        # that issues fulfill/redirect/abort. The native handler
                        # awaits its protocol ACK. Derived archives contain no
                        # status=-1 entries, so _handle cannot deliberately stall.
                        await self._owned_har_router._handle(route._impl_obj)
                        self._owned_har_acknowledgments += 1
                        self._release_owned_replay_request(request)
        except BaseException as error:
            self._owned_replay_errors.append({'url': request.url,
                                             'error_type': type(error).__name__,
                                             'message': str(error)})
            raise
        finally:
            if request not in self._replay_routes:
                self._owned_replay_locks.pop(request, None)

    async def _end_checkpoint_replay(self):
        self.replay_cleanup_audit = {'completed': False, 'policy': REPLAY_POLICY,
                                    'pending_requests': len(self._replay_routes), 'abort_errors': 0}
        for page in self.context.pages:
            if page.is_closed():
                continue
            session = await self.context.new_cdp_session(page)
            try:
                await session.send('Page.stopLoading')
            finally:
                await session.detach()
        pending = list(self._replay_routes.values())
        aborted = await asyncio.gather(*(self._abort_owned_replay_route(r) for r in pending),
                                       return_exceptions=True)
        errors = [r for r in aborted if isinstance(r, BaseException)]
        self.replay_cleanup_audit['abort_errors'] = len(errors)
        if errors:
            self.replay_cleanup_audit['errors'] = [{'error_type': type(e).__name__, 'message': str(e)}
                                                 for e in errors]
            raise BrowserCleanupError('Owned replay cancellation was not acknowledged')
        while self._replay_routes:
            await asyncio.sleep(.01)
        # Wait for every owned handler to return, including native HAR lookups.
        # No archived response callback can survive into the live phase.
        await self.context.unroute_all(behavior='wait')
        if self._owned_replay_errors:
            self.replay_cleanup_audit['errors'] = copy.deepcopy(self._owned_replay_errors)
            raise BrowserCleanupError('Owned replay handler failed')
        self._dispose_owned_har()
        self.replay_cleanup_audit.update(completed=True, remaining_requests=0,
            native_har_terminal_acknowledgments=self._owned_har_acknowledgments,
            owned_route_abort_acknowledgments=self._replay_abort_acknowledgments,
            observed_terminal_event_releases=self._terminal_event_releases,
            cancellation_evidence='owned_terminal_ack_or_observed_terminal_event_and_all_handlers_joined')

    def _dispose_owned_har(self):
        if self._owned_har_router is not None:
            self._owned_har_router.dispose()
            self._owned_har_router = None

    async def _close_resources(self):
        # Close the archive before stopping its driver; original cleanup still
        # shuts down all owned pages, contexts, callbacks and driver resources.
        self._dispose_owned_har()
        await super()._close_resources()
