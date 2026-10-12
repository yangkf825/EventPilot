"""Official transport variant; lifecycle/outcome rules mirror frozen v2.0.6.

The public-input transport and scoped memory reads differ. Browser/task semantics,
factual preparation, reset audits and evaluation functions reuse the frozen engine.
"""
from __future__ import annotations
from eventarena.online_v2_engine import AutonomousEpisode as BaseEpisode
from eventarena.online_v2_engine import (
    Path,
    StorageIOError,
    _bind_environment_capture,
    _checkpoint_expected,
    _hash,
    _write,
    asyncio,
    context_for_view,
    controlled_input_audit,
    copy,
    inspect,
    replay_checkpoint_audit,
    restore_checkpoint
)
from .browser import Browser, REPLAY_POLICY
from . import PROTOCOL
from .transport import prepare_messages, POLICY
from .context_access import ContextAccess,read_request,POLICY as CONTEXT_POLICY,LIMITS


class AutonomousEpisode(BaseEpisode):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.episode.update(engine_protocol=PROTOCOL,input_transport_policy=POLICY,
                            context_access_policy=CONTEXT_POLICY,context_read_limits=LIMITS,
                            context_reads=[],checkpoint_replay_policy=REPLAY_POLICY)

    async def call_record(self,stage,messages,parser):
        access=ContextAccess(messages);reads=[];saved=False
        def parse(raw):
            query=read_request(raw)
            return query if query is not None else parser(raw)
        while True:
            remaining=min(LIMITS['max_reads_per_call']-len(reads),
                          LIMITS['max_reads_per_episode']-len(self.episode['context_reads']))
            transmitted,audit=access.messages(self.opts.get('max_context_chars',450000),reads,remaining)
            if audit['mode']=='indexed' and not saved:
                _write(self.run_dir/'public_contexts'/(access.context_id+'.json'),access.source_record());saved=True
            row=await super().call_record(stage,transmitted,parse)
            row.update(input_transport=audit,request_sent=row.get('error')!='context_overflow')
            if row.get('parsed',{}) is None or row['parsed'].get('op')!='READ_CONTEXT':
                self.persist();return row
            # Retrieval responses cannot enter decision tables or execute a
            # browser action. They carry the parent stage for auditing costs.
            row.update(stage='context_read',parent_stage=stage)
            if remaining<=0:
                row.update(parsed=None,error='step_budget_exhausted',resource_limit='context_read_budget_exhausted')
                self.episode.update(status='step_budget_exhausted',resource_limit='context_read_budget_exhausted')
                self.persist();return row
            if not saved:
                _write(self.run_dir/'public_contexts'/(access.context_id+'.json'),access.source_record());saved=True
            request=row['parsed'];result=access.read(request,max_chars=min(LIMITS['max_read_chars'],
                max(512,self.opts.get('max_context_chars',450000)//5)))
            read={'parent_stage':stage,'context_id':access.context_id,'request':copy.deepcopy(request),
                  'result':result,'response_index':len(self.episode['responses'])-1}
            reads.append(read);self.episode['context_reads'].append(copy.deepcopy(read))
            row['context_read_result']=result;self.persist()
            progress=self.opts.get('progress')
            if progress:progress({'stage':'context_read','state':'returned','reads':len(self.episode['context_reads'])})

    async def actor_step(self,browser):
        result=await super().actor_step(browser)
        if self.episode.get('resource_limit')=='context_read_budget_exhausted':
            self.episode['status']='step_budget_exhausted';self.persist()
        return result

    async def deliver(self,browser,observation,prepare_only=False):
        result=await super().deliver(browser,observation,prepare_only)
        if self.episode.get('resource_limit')=='context_read_budget_exhausted':
            self.episode['status']='step_budget_exhausted';self.persist()
        return result


async def run_checkpoint_episode(case, model, checkpoint, view, run_dir, opts=None,
                                 browser_factory=Browser, suppress_events=False):
    """Full autonomous continuation from an audited, shared real checkpoint."""
    checkpoint = copy.deepcopy(checkpoint.get('checkpoint', checkpoint))
    if checkpoint.get('provenance') != 'actual_live_browser_checkpoint':
        raise ValueError('Full evaluation requires an actual browser checkpoint')
    checkpoint.update(case_id=case['case_id'], goal=case['goal'])
    events = copy.deepcopy(case.get('events', []))
    checkpoint.update(events=events, event=events[0] if events else None)
    options = {**(opts or {}), 'decision_view':view,
               'execution_prefix':context_for_view(checkpoint, view),
               'initial_decision_checkpoint':checkpoint}
    engine = AutonomousEpisode(case, model, run_dir, options)
    engine.episode.update(protocol='controlled_episode_v2', view=view,
                          information_policy='event-boundary projection; common current tools/goals; only selected pre-event memory; all new factual observations',
                          checkpoint=checkpoint)
    phase = 'browser_open'
    try:
        async with browser_factory(run_dir, headless=options.get('headless',False)) as browser:
            phase = 'checkpoint_replay'
            audit, observation = await restore_checkpoint(engine, browser, checkpoint)
            if not audit.get('available', True):
                if engine.episode['status'] == 'started':
                    engine.episode['status'] = 'checkpoint_projection_unavailable'
            elif not audit['matches']:
                engine.episode['status'] = 'checkpoint_replay_mismatch'
            else:
                engine.episode['controlled_action_boundary'] = len(engine.episode['actions'])
                engine.episode['checkpoints'].append(copy.deepcopy(checkpoint))
                factor = case.get('group_type', case.get('cf',{}).get('factor'))
                if factor and not suppress_events:
                    engine.episode['counterfactual_input_audit'] = controlled_input_audit(checkpoint, view, factor, case.get('cf',{}).get('state_fields'))
                if not suppress_events:
                    # Registered trigger was already established during actual
                    # preparation. Deliver only after the independent replay audit.
                    triggers = [e.get('trigger') for e in engine.events]
                    for event in engine.events:
                        if event.get('trigger',{}).get('type') != 'event_stage':
                            event['trigger'] = {'kind':'url_contains','value':observation['url']}
                    phase = 'event_delivery'
                    await engine.deliver(browser, observation)
                    for event, trigger in zip(engine.events, triggers):
                        if trigger is None: event.pop('trigger', None)
                        else: event['trigger'] = trigger
                engine.opts.pop('initial_decision_checkpoint', None)
                if engine.episode['status'] == 'started':
                    phase = 'execution'
                    await engine.continue_episode(browser)
            phase = 'browser_close'
    except StorageIOError:
        raise
    except (asyncio.CancelledError, KeyboardInterrupt):
        engine.episode['status'] = 'interrupted'
        engine.persist()
        raise
    except Exception as error:
        engine.episode.update(status='browser_error', error={'category': type(error).__name__, 'phase': phase})
    from .evaluation import evaluate_episode
    engine.episode['task_states'] = copy.deepcopy(engine.tasks)
    engine.persist()  # Durable Actor evidence precedes potentially failing evaluation.
    engine.episode['evaluation'] = await evaluate_episode(case, engine.episode, options.get('judge'))
    engine.episode['actual_schedule'] = engine.episode['evaluation'].get('actual_schedule', [])
    engine.persist()
    return engine.episode


async def run_episode(case, model, run_dir, opts=None, browser_factory=Browser, evaluator=None):
    engine = AutonomousEpisode(case, model, run_dir, opts)
    try:
        async with browser_factory(run_dir, headless=(opts or {}).get("headless", False)) as browser:
            episode = await engine.run(browser, (opts or {}).get("prepare_only", False))
    except StorageIOError:
        raise
    except (asyncio.CancelledError, KeyboardInterrupt):
        engine.episode["status"] = "interrupted"
        engine.persist()
        raise
    except Exception as error:
        episode = engine.episode
        episode.update(status="browser_error", error={"category": type(error).__name__})
    if evaluator is None and not (opts or {}).get("prepare_only"):
        from .evaluation import evaluate_episode
        evaluator = lambda c, e: evaluate_episode(c, e, judge=(opts or {}).get("judge"))
    if evaluator is not None:
        result = evaluator(case, episode)
        episode["evaluation"] = await result if inspect.isawaitable(result) else result
        episode["actual_schedule"] = episode["evaluation"].get("actual_schedule", [])
    episode["task_states"] = copy.deepcopy(engine.tasks)
    _write(Path(run_dir) / "episode.json", episode)
    return episode


async def prepare_checkpoint(case, model, run_dir, opts=None, browser_factory=Browser):
    episode = await run_episode(case, model, run_dir, {**(opts or {}), "prepare_only": True}, browser_factory)
    if episode.get('status') == 'checkpoint_ready' and isinstance(episode.get('checkpoint'), dict):
        checkpoints = [episode['checkpoint'], *episode.get('checkpoints', [])]
        try:
            capture = _bind_environment_capture(run_dir, checkpoints)
            if episode.get('environment_capture_required') and capture is None:
                raise ValueError('Registered browser archive was not exported')
        except (OSError, ValueError) as error:
            episode.update(status='checkpoint_capture_unavailable', preparation_failure_is_model=False,
                           error={'category': type(error).__name__, 'phase': 'environment_capture_export'})
            episode['partial_checkpoint'] = episode.pop('checkpoint')
            checkpoint_path = Path(run_dir) / 'checkpoint_case.json'
            if checkpoint_path.exists():
                checkpoint_path.unlink()
        else:
            _write(Path(run_dir) / 'checkpoint_case.json', episode['checkpoint'])
        _write(Path(run_dir) / 'episode.json', episode)
    return episode


async def prepare_state_pair(cases, model, run_dir, opts=None, browser_factory=Browser):
    """Prepare two factual B states and publish them only as one audited pair."""
    if len(cases) != 2:
        raise ValueError('A State pair must contain two cases')
    completed = next((c for c in cases if c.get('state_variant')=='target_completed'),None)
    unfinished = next((c for c in cases if c.get('state_variant')=='related_completed'),None)
    if completed is None or unfinished is None:
        completed = next((c for c in cases if c.get('setup_tasks')), None)
        unfinished = next((c for c in cases if not c.get('setup_tasks')), None)
    if completed is None or unfinished is None:
        raise ValueError('State pair needs one actual B setup and one unfinished B')
    if completed['goal'] != unfinished['goal'] or completed['url'] != unfinished['url']:
        raise ValueError('State pair must share A goal and entry')
    if completed.get('checkpoint_audit_policy') != unfinished.get('checkpoint_audit_policy'):
        raise ValueError('State pair must share a registered checkpoint audit policy')
    engine = AutonomousEpisode(unfinished, model, run_dir, opts)
    result = {'status': 'started', 'checkpoints': {}}
    phase = 'browser_open'
    try:
        async with browser_factory(run_dir, headless=(opts or {}).get('headless', False)) as browser:
            browser.checkpoint_audit_policy = copy.deepcopy(unfinished.get('checkpoint_audit_policy'))
            phase = 'A_checkpoint_preparation'
            prepared = await engine.run(browser, prepare_only=True)
            if not isinstance(prepared, dict) or prepared.get('status') != 'checkpoint_ready' or not isinstance(prepared.get('checkpoint'), dict):
                result.update(status=prepared.get('status', 'checkpoint_setup_unverified') if isinstance(prepared, dict) else 'checkpoint_setup_unverified',
                              preparation_episode=copy.deepcopy(engine.episode))
                if result['status'] in {'started', 'checkpoint_ready'}:
                    result['status'] = 'checkpoint_setup_unverified'
            else:
                first = copy.deepcopy(prepared['checkpoint'])
                result['partial_checkpoints'] = {unfinished['case_id']: first}
                a_page = browser.page
                engine.case = copy.deepcopy(completed)
                engine.episode['status'] = 'started'
                phase = 'B_setup_preparation'
                browser.page = await browser.context.new_page()
                okay = await engine.warmup(browser)
                # A failed or unknown B preparation is terminal. In particular,
                # do not perform a restoration capture that could replace its
                # model/provider failure with an unrelated browser exception.
                if okay is not True:
                    status = engine.episode.get('status', 'checkpoint_setup_unverified')
                    if status in {'started', 'checkpoint_ready'}:
                        status = 'checkpoint_setup_unverified'
                    result.update(status=status, setup_evaluation=copy.deepcopy(engine.episode.get('setup_evaluation')),
                                  preparation_failure_status=engine.episode.get('preparation_failure_status'),
                                  preparation_failure_is_model=engine.episode.get('preparation_failure_is_model'))
                else:
                    # Explicit verification, durable outputs and a completion
                    # claim are all required; no truthy unknown dictionary passes.
                    verified = all(engine.episode['prior_results'].get(s.get('alias_event_id', s['id']), {}).get('verification', {}).get('success') is True and
                                   engine.episode['prior_results'].get(s.get('alias_event_id', s['id']), {}).get('outputs') and
                                   engine.tasks.get(s['id'], {}).get('completed') is True
                                   for s in completed['setup_tasks'])
                    if not verified:
                        result.update(status='checkpoint_setup_unverified', preparation_failure_is_model=False)
                    else:
                        phase = 'A_restoration_audit'
                        browser.page = a_page
                        engine.pages['A'], engine.active = a_page, 'A'
                        restored = await engine.observe(browser)
                        expected = _checkpoint_expected(first)
                        audit = replay_checkpoint_audit(expected, restored, policy=completed.get('checkpoint_audit_policy'))
                        result['restoration_audit'] = audit
                        if not audit.get('available', True):
                            result['status'] = 'checkpoint_setup_unverified'
                        elif audit['matches'] is not True:
                            result['status'] = 'checkpoint_restoration_mismatch'
                        else:
                            second = engine.checkpoint(restored, engine.events)
                            second['state'] = copy.deepcopy(first['state'])
                            second['state']['prior_results'] = copy.deepcopy(engine.episode['prior_results'])
                            second.update(case_id=completed['case_id'], restored_A_observation=restored,
                                          restoration_audit=audit, shared_A_observation_sha256=_hash(expected))
                            first['shared_A_observation_sha256'] = _hash(expected)
                            result['checkpoints'] = {unfinished['case_id']: first, completed['case_id']: second}
                            result.pop('partial_checkpoints', None)
                            result.update(status='checkpoint_ready',
                                          setup_evaluation={k: v['verification'] for k, v in engine.episode['prior_results'].items()})
                result['preparation_episode'] = copy.deepcopy(engine.episode)
            phase = 'browser_close'
    except StorageIOError:
        raise
    except (asyncio.CancelledError, KeyboardInterrupt):
        result.update(status='interrupted', checkpoints={}, preparation_episode=copy.deepcopy(engine.episode), failure_phase=phase)
        _write(Path(run_dir) / 'state_pair.json', result)
        raise
    except Exception as error:
        if result.get('checkpoints'):
            result['partial_checkpoints'] = result['checkpoints']
        result.update(status='browser_error', checkpoints={}, error={'category': type(error).__name__, 'phase': phase},
                      preparation_episode=copy.deepcopy(engine.episode))
    # Archive exists only after the browser context has closed successfully.
    if result['status'] == 'checkpoint_ready':
        try:
            capture = _bind_environment_capture(run_dir, result['checkpoints'].values())
            if engine.episode.get('environment_capture_required') and capture is None:
                raise ValueError('Registered browser archive was not exported')
        except (OSError, ValueError) as error:
            result['partial_checkpoints'] = result['checkpoints']
            result.update(status='checkpoint_capture_unavailable', checkpoints={}, preparation_failure_is_model=False,
                          error={'category': type(error).__name__, 'phase': 'environment_capture_export'})
    # The generic preparation artifacts cannot claim pair readiness on their own.
    engine.episode['status'] = result['status']
    engine.episode['state_pair_status'] = result['status']
    engine.episode['state_pair_ready'] = result['status'] == 'checkpoint_ready'
    engine.episode.pop('checkpoint', None)
    engine.persist()
    checkpoint_path = Path(run_dir) / 'checkpoint_case.json'
    if checkpoint_path.exists():
        if result['status'] == 'checkpoint_ready':
            _write(checkpoint_path, {'status': 'checkpoint_ready', 'checkpoints': result['checkpoints']})
        else:
            checkpoint_path.unlink()
    result['preparation_episode'] = copy.deepcopy(engine.episode)
    _write(Path(run_dir) / 'state_pair.json', result)
    return result


