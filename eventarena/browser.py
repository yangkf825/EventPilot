"""Actual Playwright interaction, factual observations, and checkpoint recording."""
import asyncio
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

OBSERVE_JS = r"""() => {
  const visible = el => !!(el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  const controls = [...document.querySelectorAll('a,button,input,select,textarea,[role=button],[role=link],[role=combobox],[role=option]')]
    .filter(visible).filter(el => !el.closest('[data-eventarena-panel]'));
  const candidates = controls.map((el, idx) => {
    el.setAttribute('data-ea-node', String(idx));
    return {node_id:String(idx), tag:el.tagName.toLowerCase(), text:el.innerText || el.getAttribute('aria-label') || '',
      role:el.getAttribute('role'), name:el.getAttribute('name'), type:el.getAttribute('type'),
      id:el.id, checked:typeof el.checked==='boolean'?el.checked:null,
      selected:Array.from(el.selectedOptions||[]).map(o=>o.value),
      placeholder:el.getAttribute('placeholder'), value:el.value || '', disabled:el.disabled || false,
      href:el.getAttribute('href'), options:el.tagName==='SELECT' ? [...el.options].map(o=>({label:o.label,value:o.value})):null};
  });
  // innerText on a detached clone falls back to textContent. That exposes
  // collapsed menus, hidden template variants and source whitespace rather
  // than the rendered document the browser agent can actually inspect.
  // Read the live rendered body, excluding our notification overlay without
  // removing it or leaving a changed inline style behind.
  const panels = [...document.querySelectorAll('[data-eventarena-panel]')]
    .map(el => ({el, style:el.getAttribute('style')}));
  let text = '';
  try {
    panels.forEach(({el, style}) => el.setAttribute('style', (style || '') + ';display:none!important;'));
    text = document.body ? document.body.innerText : '';
  } finally {
    panels.forEach(({el, style}) => {
      if (style === null) el.removeAttribute('style');
      else el.setAttribute('style', style);
    });
  }
  return {title:document.title, url:location.href, text, candidates};
}"""

# The policy is registered in the dataset, never inferred from model scores.
# Read the complete rendered task container while excluding only declared UI
# containers. Raw body/controls remain in the factual observation and trace.
PROJECT_JS = r"""({policy, candidates}) => {
  const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  let root=null, selector=null;
  for (const s of policy.content_selectors) {
    const nodes=[...document.querySelectorAll(s)].filter(visible);
    root=nodes.find(n => (n.innerText||'').trim().length>=120);
    if (root) {selector=s; break;}
  }
  if (!root) return {available:false,reason:'registered_content_root_unavailable'};
  const excluded=[...new Set(policy.exclude_selectors.flatMap(s => [...root.querySelectorAll(s)]))];
  const panels=[...root.querySelectorAll('[data-eventarena-panel]')];
  const hidden=[...new Set([...excluded,...panels])].map(el=>({el,style:el.getAttribute('style')}));
  let text='';
  try {
    hidden.forEach(({el,style})=>el.setAttribute('style',(style||'')+';display:none!important;'));
    text=root.innerText;
  } finally {
    hidden.forEach(({el,style})=>style===null ? el.removeAttribute('style') : el.setAttribute('style',style));
  }
  const controls=candidates.filter(c=>{
    const el=document.querySelector('[data-ea-node="'+c.node_id+'"]');
    return el && root.contains(el) && !excluded.some(n=>n.contains(el));
  });
  const anchors=policy.required_text_any||[];
  if (anchors.length && !anchors.some(a=>text.toLowerCase().includes(a.toLowerCase())))
    return {available:false,reason:'registered_task_content_anchor_unavailable'};
  return {available:true,url:location.href,title:document.title,content:[{selector,text,controls}]};
}"""


def checkpoint_policy_hash(policy):
    return hashlib.sha256(json.dumps(policy,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def validate_checkpoint_policy(policy):
    if not isinstance(policy,dict) or policy.get('mode') != 'task_content_v1' or not policy.get('id'):
        raise ValueError('Unsupported checkpoint projection policy')
    for key in ('content_selectors','exclude_selectors'):
        if not isinstance(policy.get(key),list) or not policy[key] or not all(isinstance(s,str) and s for s in policy[key]):
            raise ValueError('Checkpoint projection requires registered selectors')
    if not isinstance(policy.get('required_text_any',[]),list):
        raise ValueError('Invalid checkpoint task anchors')
    return policy


def permitted_url(url):
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and bool(parsed.hostname)


class BrowserOperationTimeout(TimeoutError):
    """A browser/driver operation exceeded its explicit outer deadline."""


class BrowserCleanupError(RuntimeError):
    """Cleanup was attempted, but the capture or replay boundary is unverified."""


class Browser:
    environment_capture_required = True
    operation_timeout_s = 30.0
    startup_timeout_s = 45.0
    cleanup_timeout_s = 15.0
    capture_flush_timeout_s = 30.0
    replay_cleanup_timeout_s = 10.0

    def __init__(self, run_dir, headless=False):
        self.run_dir = Path(run_dir)
        self.headless = headless
        self.history = []
        self.candidates = {}
        self.page_http_status = {}
        self.pending_popups = []
        self.checkpoint_audit_policy = None
        self.network_mode = 'live_capture'
        self.playwright = self.browser = self.context = self.page = None
        self._driver_process = None
        self._replay_routes = {}
        self.replay_cleanup_audit = None
        self.cleanup_audit = None

    async def _bounded(self, operation, stage, timeout=None):
        limit = self.operation_timeout_s if timeout is None else timeout
        try:
            return await asyncio.wait_for(operation, timeout=limit)
        except asyncio.TimeoutError as error:
            raise BrowserOperationTimeout(f'{stage} exceeded {limit:g}s') from error

    def _finish_replay_request(self, request):
        self._replay_routes.pop(request, None)

    async def _track_replay_route(self, route):
        # This public routing layer does not alter the archive or the request.
        # It lets us abort the HAR handler's deliberately stalled requests
        # (recorded response.status == -1) at the replay/live boundary.
        self._replay_routes[route.request] = route
        if self.network_mode == 'replay_cleanup':
            # Page timers can issue another request while cleanup is running.
            # Do not give that request back to a potentially stalled archive.
            await route.abort()
        else:
            await route.fallback()

    def _attach_page(self, page):
        def response_received(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                self.page_http_status[page] = response.status
        page.on('response', response_received)
        page.on('popup', lambda child: self.pending_popups.append(child))

    async def __aenter__(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / 'trajectory').mkdir(exist_ok=True)
        try:
            self.playwright = await self._bounded(async_playwright().start(), 'driver.start', self.startup_timeout_s)
            # Retain only our own spawned child for last-resort shutdown. No
            # PID discovery or process-name matching is used during cleanup.
            connection = getattr(getattr(self.playwright, '_impl_obj', None), '_connection', None)
            self._driver_process = getattr(getattr(connection, '_transport', None), '_proc', None)
            self.browser = await self._bounded(self.playwright.chromium.launch(headless=self.headless),
                                              'browser.launch', self.startup_timeout_s)
            self.context = await self._bounded(self.browser.new_context(viewport={'width':1280,'height':900},
                record_har_path=str(self.run_dir/'network.har'), record_har_content='embed',
                record_har_mode='full', service_workers='block'), 'context.create')
            self.context.set_default_timeout(20000)
            self.context.set_default_navigation_timeout(20000)
            self.context.on('page', self._attach_page)
            self.context.on('requestfinished', self._finish_replay_request)
            self.context.on('requestfailed', self._finish_replay_request)
            await self._bounded(self.context.tracing.start(screenshots=True, snapshots=True, sources=True), 'tracing.start')
            self.page = await self._bounded(self.context.new_page(), 'page.create')
            return self
        except BaseException:
            # __aexit__ is not called when __aenter__ fails.
            await self._close_resources()
            raise

    async def __aexit__(self, *exc):
        await self._close_resources()
        if not self.cleanup_audit['completed'] and not (exc and exc[0]):
            failures = ', '.join(s['stage'] for s in self.cleanup_audit['steps'] if not s['ok'])
            raise BrowserCleanupError(f'Browser cleanup incomplete: {failures}')

    async def _close_resources(self):
        # One slow trace/HAR flush must never skip browser/driver shutdown.
        # Retain every failure instead of publishing a successful capture.
        steps = []
        cancelled = None
        operations = []
        if self.context is not None:
            operations.extend([
                ('routes.remove', lambda: self.context.unroute_all(behavior='ignoreErrors')),
                ('tracing.stop', lambda: self.context.tracing.stop(path=self.run_dir / 'trace.zip')),
                ('context.close', self.context.close),
            ])
        if self.browser is not None:
            operations.append(('browser.close', self.browser.close))
        if self.playwright is not None:
            operations.append(('driver.stop', self.playwright.stop))
        for stage, operation in operations:
            try:
                limit = self.capture_flush_timeout_s if stage in {'tracing.stop', 'context.close'} else self.cleanup_timeout_s
                await self._bounded(operation(), stage, limit)
                steps.append({'stage':stage, 'ok':True})
            except asyncio.CancelledError as error:
                # The overall preparation deadline may expire while already
                # closing. Still attempt the remaining owned resources.
                cancelled = error
                steps.append({'stage':stage, 'ok':False, 'error_type':'CancelledError', 'message':'cleanup cancelled'})
            except Exception as error:
                steps.append({'stage':stage, 'ok':False, 'error_type':type(error).__name__, 'message':str(error)})
        if (self._driver_process is not None and self._driver_process.returncode is None
                and any(s['stage'] == 'driver.stop' and not s['ok'] for s in steps)):
            try:
                try:
                    self._driver_process.kill()
                except ProcessLookupError:
                    pass  # It exited between the returncode check and kill.
                await self._bounded(self._driver_process.wait(), 'driver.force_stop', 3.0)
                steps.append({'stage':'driver.force_stop', 'ok':True})
            except Exception as error:
                steps.append({'stage':'driver.force_stop', 'ok':False, 'error_type':type(error).__name__, 'message':str(error)})
        self.cleanup_audit = {'completed':all(s['ok'] for s in steps), 'steps':steps,
                              'network_mode':self.network_mode, 'replay_cleanup':self.replay_cleanup_audit}
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir/'browser_cleanup.json').write_text(json.dumps(self.cleanup_audit, ensure_ascii=False, indent=2), encoding='utf-8')
        if cancelled is not None:
            raise cancelled

    async def begin_checkpoint_replay(self, capture):
        if capture.get('mode') != 'recorded_prefix_then_live':
            raise ValueError('Unsupported checkpoint network reset mode')
        har=Path(capture.get('har_path',''))
        if not har.is_file() or hashlib.sha256(har.read_bytes()).hexdigest()!=capture.get('sha256'):
            raise ValueError('Registered checkpoint network archive missing or changed')
        try:
            await self._bounded(self.context.route_from_har(str(har),not_found='abort',update=False), 'replay.routes.install')
            await self._bounded(self.context.route('**/*', self._track_replay_route), 'replay.tracker.install')
            self.network_mode='recorded_prefix'
        except BaseException:
            self.network_mode='replay_setup_failed'
            raise

    async def end_checkpoint_replay(self):
        if self.network_mode=='recorded_prefix':
            self.network_mode='replay_cleanup'
            try:
                await self._bounded(self._end_checkpoint_replay(), 'replay.cleanup', self.replay_cleanup_timeout_s)
            except BaseException as error:
                self.network_mode='replay_cleanup_failed'
                self.replay_cleanup_audit = {**(self.replay_cleanup_audit or {}), 'completed':False,
                    'error_type':type(error).__name__, 'message':str(error), 'remaining_requests':len(self._replay_routes)}
                raise
            self.network_mode='live_after_checkpoint'

    async def _end_checkpoint_replay(self):
        # Waiting for HAR callbacks is unbounded when an unfinished archived
        # request is represented by status -1. Stop the old resource loads
        # BEFORE disabling interception: unroute first can silently continue a
        # stalled HAR request onto the live web and alter the checkpoint DOM.
        # CDP is supported by the Chromium browser selected by this class.
        self.replay_cleanup_audit = {'completed':False, 'pending_requests':len(self._replay_routes),
                                     'abort_errors':0}
        for page in self.context.pages:
            if page.is_closed():
                continue
            session = await self.context.new_cdp_session(page)
            try:
                await session.send('Page.stopLoading')
            finally:
                await session.detach()
        pending = list(self._replay_routes.values())
        aborted = await asyncio.gather(*(r.abort() for r in pending), return_exceptions=True)
        self.replay_cleanup_audit['abort_errors'] = sum(isinstance(r, BaseException) for r in aborted)
        # Require terminal request events, rather than assuming abort errors
        # mean the request finished. Inability to drain is a failed boundary.
        while self._replay_routes:
            await asyncio.sleep(.01)
        # ignoreErrors only suppresses exceptions from detached callbacks. It
        # does not complete or cancel them; terminal requests were required
        # above so there is no old archived fetch to cross into the live phase.
        await self.context.unroute_all(behavior='ignoreErrors')
        self.replay_cleanup_audit.update(completed=True, remaining_requests=0)

    async def observe(self):
        return await self._bounded(self._observe(), 'observe')

    async def _observe(self):
        if self.pending_popups:
            self.page = self.pending_popups.pop()
        await self.page.wait_for_load_state('domcontentloaded')
        if self.pending_popups:
            self.page = self.pending_popups.pop()
            await self.page.wait_for_load_state('domcontentloaded')
        result = await self.page.evaluate(OBSERVE_JS)
        if self.checkpoint_audit_policy:
            policy=validate_checkpoint_policy(self.checkpoint_audit_policy)
            previous=None
            projection=None
            # Bounded settling uses the registered content, not network-idle
            # (advertising requests can run forever). It performs no task action.
            for attempt in range(5):
                projection=await self.page.evaluate(PROJECT_JS,{'policy':policy,'candidates':result['candidates']})
                fingerprint=json.dumps(projection,sort_keys=True,ensure_ascii=False)
                if projection.get('available') and fingerprint==previous:
                    break
                previous=fingerprint
                if attempt<4:
                    await asyncio.sleep(.25)
                    result=await self.page.evaluate(OBSERVE_JS)
            else:
                if projection and projection.get('available'):
                    projection={'available':False,'reason':'registered_content_not_settled'}
            result['checkpoint_projection']={**(projection or {}),'policy_id':policy['id'],
                                             'policy_sha256':checkpoint_policy_hash(policy)}
        result['http_status'] = self.page_http_status.get(self.page, 200)
        result['network_mode'] = self.network_mode
        self.candidates = {c['node_id']: c for c in result['candidates']}
        return result

    async def save(self, action=None, phase='original_task', error=None):
        idx = len(self.history)
        observation = await self.observe()
        shot = self.run_dir / 'trajectory' / f'{idx:04d}.png'
        raw = self.run_dir / 'trajectory' / f'{idx:04d}.html'
        await self._bounded(self.page.screenshot(path=shot, full_page=False), 'observation.screenshot')
        raw.write_text(await self._bounded(self.page.content(), 'observation.html'), encoding='utf-8')
        row = {'step': idx, 'timestamp': time.time(), 'url': self.page.url,
               'observation': observation['text'], 'candidates': observation['candidates'],
               'action': action, 'phase': phase, 'error': error,
               'screenshot': str(shot.relative_to(self.run_dir)),
               'html': str(raw.relative_to(self.run_dir)), 'provenance': 'live_browser_observation'}
        self.history.append(row)
        with (self.run_dir / 'trajectory.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        return row

    async def execute(self, action, phase='original_task'):
        # Locator.count/mouse/CDP calls have no Playwright timeout, while one
        # action also includes recording and a fresh observation. Bound the
        # complete action independently of the preparation's outer deadline.
        kind = action.get('op', '').upper()
        await self._bounded(self._execute(action, phase), f'execute.{kind}', self.operation_timeout_s * 2)

    async def _execute(self, action, phase='original_task'):
        kind = action.get('op', '').upper()
        if kind == 'GOTO':
            if not permitted_url(action.get('url', '')):
                raise ValueError('GOTO requires an http(s) website URL')
            await self.save(action, phase)
            response = await self.page.goto(action['url'], wait_until='domcontentloaded')
            self.last_http_status = response.status if response else 200
            self.page_http_status[self.page] = self.last_http_status
        elif kind in ('CLICK', 'TYPE', 'SELECT'):
            node = str(action.get('node_id', ''))
            if node not in self.candidates:
                raise ValueError('Unknown node_id; obtain a fresh observation first')
            candidate = self.candidates[node]
            locator = self.page.locator(f'[data-ea-node="{node}"]')
            if await locator.count() != 1:
                raise ValueError('Node is missing or duplicated; page changed')
            # The exact observed element information is saved with each factual action.
            factual = {**action, 'observed_element': candidate}
            await self.save(factual, phase)
            if kind == 'CLICK':
                if await locator.get_attribute('target') == '_blank':
                    clicked = False
                    try:
                        async with self.page.expect_popup(timeout=1000) as popup:
                            await locator.click()
                            clicked = True
                        child = await popup.value
                        self.pending_popups = [p for p in self.pending_popups if p is not child]
                        self.page = child
                        await child.wait_for_load_state('domcontentloaded')
                    except PlaywrightTimeoutError:
                        if not clicked:
                            raise
                        # A site may intercept a target=_blank link and handle
                        # it in-page. The successful click is still factual.
                else:
                    await locator.click()
            elif kind == 'TYPE':
                await locator.fill(str(action.get('value', '')))
            else:
                await locator.select_option(value=str(action.get('value', '')))
            if self.pending_popups:
                self.page = self.pending_popups.pop()
                await self.page.wait_for_load_state('domcontentloaded')
        elif kind in ('BACK', 'SCROLL', 'WAIT'):
            await self.save(action, phase)
            if kind == 'BACK':
                await self.page.go_back(wait_until='domcontentloaded')
            elif kind == 'SCROLL':
                await self.page.mouse.wheel(0, max(-1800, min(1800, int(action.get('pixels', 700)))))
            else:
                await asyncio.sleep(min(5, max(0, float(action.get('seconds', 1)))))
        else:
            raise ValueError(f'Unsupported browser operation {kind}')
        await self.observe()

    async def inject(self, events):
        # This only renders experiment messages. It never changes a third-party backend.
        await self._bounded(self.page.evaluate("""events => {
          document.querySelector('[data-eventarena-panel]')?.remove();
          const panel=document.createElement('aside'); panel.setAttribute('data-eventarena-panel','true');
          panel.style.cssText='position:fixed;right:12px;top:12px;width:350px;z-index:2147483647;padding:16px;background:white;color:black;border:2px solid black;font:14px sans-serif;white-space:pre-wrap';
          panel.textContent=events.map(e=>e.source+': '+e.text).join('\\n\\n');
          document.body.appendChild(panel);
        }""", events), 'event.inject')

    async def checkpoint_case(self, case_id, goal, events, task_id=None):
        current = await self.save(None, 'checkpoint')
        trajectory = [{'step': r['step'], 'url':r['url'], 'observation':r['observation'],
                       'action':json.dumps(r['action'], ensure_ascii=False) if r['action'] else None}
                      for r in self.history]
        row = {'case_id':case_id, 'base_task_id':task_id, 'goal':goal,
               'state':{'url':current['url'],'summary':current['observation']}, 'trajectory':trajectory,
               'annotation':{'provenance':'live_browser_observation','human_reviewed':False,
                             'status':'needs_gold_review','event_provenance':'researcher_injected'}}
        if len(events) == 1:
            row['event'] = events[0]
        else:
            row['events'] = events
        (self.run_dir / 'checkpoint_case.json').write_text(json.dumps(row, ensure_ascii=False, indent=2))
        return row
