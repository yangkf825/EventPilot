"""Common factual preparation and reset audit before tested actors are sampled.

Eligibility never reads Gold, predictions, or tested model success. The same
frozen case-repeat gate applies to every information view, model and baseline.
Progress records contain only operational metadata, never prompts or Gold.
"""
import asyncio
import copy
import json
import time


HEARTBEAT_SECONDS = 15
RESET_AUDIT_TIMEOUT_SECONDS = 120
_PROGRESS_FIELDS = {'stage', 'state', 'api_ok', 'step', 'step_index', 'action_index',
                    'task_id', 'op', 'operation', 'error_type', 'preparation_status'}


class PreparationProgress:
    """Persist the shared preparation phase separately from actor job states."""
    def __init__(self, root, planned, preparer, judge_model):
        from run_online_v2 import now, write_json
        self.root, self.now, self.write_json = root, now, write_json
        self.started = time.monotonic()
        self.case_started = self.started
        self.state = {'phase': 'preparation', 'state': 'running', 'planned': planned,
                      'completed': 0, 'eligible': 0, 'current_case': None,
                      'preparer_model': preparer, 'judge_model': judge_model,
                      'scope': 'shared_case_repeat_initial_conditions_before_tested_actors',
                      'selection_reads_tested_model_results': False,
                      'started_at': now(), 'updated_at': now(), 'last_progress': None}
        self.root.mkdir(parents=True, exist_ok=True)
        self.persist()

    def persist(self):
        self.state['updated_at'] = self.now()
        self.state['elapsed_s'] = round(time.monotonic() - self.started, 1)
        self.write_json(self.root / 'preparation_state.json', self.state)

    def begin(self, case_id, repeat, index):
        self.case_started = time.monotonic()
        self.state.update(current_case={'case_id': case_id, 'repeat': repeat, 'index': index},
                          case_started_at=self.now(), actor_requests=0)
        self.progress({'stage': 'case', 'state': 'START'})

    def progress(self, info):
        # Engine callbacks may contain error messages or other internal values.
        # Only operational fields enter the user-facing progress records.
        safe = {k: v for k, v in info.items() if k in _PROGRESS_FIELDS
                and isinstance(v, (str, int, float, bool, type(None)))}
        stamp = self.now()
        if safe.get('stage') in {'actor', 'actor_format_retry'} and safe.get('state') == 'calling':
            self.state['actor_requests'] += 1
        self.state['last_progress'] = {'timestamp': stamp, **safe}
        self.state['case_elapsed_s'] = round(time.monotonic() - self.case_started, 1)
        self.persist()
        current = self.state.get('current_case') or {}
        record = {'timestamp': stamp, 'scope': 'common_preparation',
                  'case_id': current.get('case_id'), 'repeat': current.get('repeat'), **safe}
        for name in ('preparation_progress.jsonl', 'progress.jsonl'):
            with (self.root / name).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        print(f"[PREP {current.get('index', 0)}/{self.state['planned']}] "
              f"{current.get('case_id', '')} repeat={current.get('repeat', '')}: "
              f"{safe.get('stage', '')} {safe.get('state', '')}", flush=True)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            self.state['case_elapsed_s'] = round(time.monotonic() - self.case_started, 1)
            self.persist()
            current = self.state.get('current_case') or {}
            last = self.state.get('last_progress') or {}
            record = {'timestamp': self.now(), 'scope': 'common_preparation',
                      'stage': 'heartbeat', 'state': 'waiting',
                      'case_id': current.get('case_id'), 'repeat': current.get('repeat'),
                      'last_stage': last.get('stage'), 'last_state': last.get('state'),
                      'case_elapsed_s': round(time.monotonic() - self.case_started, 1)}
            for name in ('preparation_progress.jsonl', 'progress.jsonl'):
                with (self.root / name).open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            print(f"[PREP {current.get('index', 0)}/{self.state['planned']}] "
                  f"{current.get('case_id', '')} repeat={current.get('repeat', '')}: "
                  f"waiting {last.get('stage', '')}/{last.get('state', '')} "
                  f"({record['case_elapsed_s']}s; completed={self.state['completed']})", flush=True)

    def complete(self, row, cached=False):
        self.state['completed'] += 1
        self.state['eligible'] += row.get('eligible') is True
        self.progress({'stage': 'case', 'state': 'CACHED' if cached else 'DONE',
                       'preparation_status': row.get('reason')})
        print(f"  preparation result: {row.get('reason')}; eligible={row.get('eligible') is True}", flush=True)

    def finish(self, rows):
        self.state.update(state='finished', current_case=None, completed=len(rows),
                          eligible=sum(r.get('eligible') is True for r in rows), finished_at=self.now())
        self.persist()
        print(f"[PREP] Finished common preparation: {len(rows)}/{self.state['planned']}; "
              f"eligible={self.state['eligible']}. Tested actor jobs may now start.", flush=True)


async def prepare_pool(args, root, selected, models, locks, obtain_checkpoint):
    from run_online_v2 import FILES, read_cases, read_json, write_json, write_csv
    from eventarena.online_v2_engine import AutonomousEpisode, restore_checkpoint
    from eventarena.browser import Browser
    originals = {c['case_id']: c for c in read_cases(args.data_dir / FILES['main'])}
    plan = {}
    for experiment, cases in selected.items():
        if experiment == 'final_intent':
            continue
        for case in cases:
            source = originals[case['original_case_id']] if experiment == 'baseline' else case
            for repeat in range(args.repeats):
                plan[(source['case_id'], repeat)] = (source, experiment)
    directory = root / 'preparation_eligibility'
    rows = []
    progress = PreparationProgress(root, len(plan), args.prep_model, args.judge_model)

    class NeverCalled:
        def call(self, messages):
            raise AssertionError('The reset audit must never call a tested model')

    try:
        for n, ((cid, repeat), (case, experiment)) in enumerate(sorted(plan.items()), 1):
            folder = directory / f'repeat_{repeat}' / cid
            progress.begin(cid, repeat, n)
            cached = read_json(folder / 'eligibility.json')
            if cached and not getattr(args, 'retry_infra', False):
                rows.append(cached)
                progress.complete(cached, cached=True)
                continue
            heartbeat = asyncio.create_task(progress.heartbeat())
            judge_rows = []

            async def judge(messages):
                progress.progress({'stage': 'judge', 'state': 'calling'})
                api_ok = False
                try:
                    result = await models[args.judge_model].call(messages)
                    api_ok = True
                    judge_rows.append({'messages': messages, 'raw': str(result)})
                    write_json(folder / 'judge_responses.json', judge_rows)
                    return result
                finally:
                    progress.progress({'stage': 'judge', 'state': 'returned', 'api_ok': api_ok})

            opts = {k: getattr(args, k) for k in ('headless', 'max_steps', 'pre_steps', 'setup_steps',
                                                'max_context_chars', 'loop_limit', 'format_retries')}
            opts.update(repeat=repeat, judge=judge if args.judge_model else None, progress=progress.progress)
            row = {'case_id': cid, 'repeat': repeat, 'checkpoint_key': None,
                   'preparation_status': None, 'eligible': False,
                   'reason': 'common_preparation_unavailable', 'independent_of_tested_actor_scores': True}
            try:
                pool = selected.get('counterfactual', []) if experiment == 'counterfactual' else list(originals.values())
                progress.progress({'stage': 'checkpoint_preparation', 'state': 'calling'})
                prepared, cp, key = await obtain_checkpoint(case, repeat, pool, root, models[args.prep_model], opts, locks)
                row.update(checkpoint_key=key, preparation_status=prepared.get('status'))
                progress.progress({'stage': 'checkpoint_preparation', 'state': 'returned',
                                   'preparation_status': prepared.get('status')})
                if prepared.get('status') == 'checkpoint_ready' and cp:
                    engine = AutonomousEpisode(case, NeverCalled(), folder / 'reset_audit', opts)
                    progress.progress({'stage': 'reset_audit', 'state': 'calling'})
                    reset_deadline = asyncio.timeout(RESET_AUDIT_TIMEOUT_SECONDS)
                    try:
                        # Include browser lifecycle in the deadline. Browser cleanup
                        # has its own bounded cancellation handling; uncertainty does
                        # not become a model failure or a successful state match.
                        async with reset_deadline:
                            async with Browser(folder / 'reset_audit', headless=args.headless) as browser:
                                audit, _ = await restore_checkpoint(engine, browser, cp)
                        available = audit.get('available', True) is True
                        matched = audit.get('matches')
                        reason = ('reset_verified' if available and matched is True else
                                  'reset_state_mismatch' if available and matched is False else
                                  'reset_audit_unavailable')
                        row.update(eligible=available and matched is True, reason=reason,
                                   reset_audit=copy.deepcopy(audit))
                        if reason == 'reset_audit_unavailable':
                            row['preparation_failure_is_model'] = False
                            engine.episode.update(status='checkpoint_projection_unavailable',
                                                  preparation_failure_is_model=False,
                                                  checkpoint_replay_audit=copy.deepcopy(audit))

                    except TimeoutError as error:
                        # Browser operations have their own, shorter deadlines.
                        # Only an expired outer deadline represents the 120s limit.
                        expired = reset_deadline.expired()
                        reason = 'reset_audit_timeout' if expired else 'reset_audit_error'
                        row.update(reason=reason, error_type=type(error).__name__,
                                   preparation_failure_is_model=False,
                                   reset_audit={'matches': None, 'available': False,
                                                'reason': reason, 'error_type': type(error).__name__})
                        if expired:
                            row['reset_audit']['timeout_s'] = RESET_AUDIT_TIMEOUT_SECONDS
                    except Exception as error:
                        row.update(reason='reset_audit_error', error_type=type(error).__name__,
                                   preparation_failure_is_model=False,
                                   reset_audit={'matches': None, 'available': False,
                                                'reason': 'reset_audit_error',
                                                'error_type': type(error).__name__})
                    finally:
                        if row['reason'] in {'reset_audit_timeout', 'reset_audit_error'}:
                            engine.episode.update(status='browser_error',
                                                  preparation_failure_is_model=False,
                                                  checkpoint_replay_audit=copy.deepcopy(row['reset_audit']),
                                                  error={'category': row['error_type'], 'phase': 'reset_audit'})
                        engine.persist()
                        progress.progress({'stage': 'reset_audit', 'state': 'returned',
                                           'preparation_status': row['reason']})
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as error:
                row.update(reason='common_preparation_error', error_type=type(error).__name__,
                           preparation_failure_is_model=False)
                progress.progress({'stage': 'checkpoint_preparation', 'state': 'error',
                                   'error_type': type(error).__name__})
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            write_json(folder / 'eligibility.json', row)
            rows.append(row)
            progress.complete(row)
    except (asyncio.CancelledError, KeyboardInterrupt):
        progress.state.update(state='interrupted', interrupted_at=progress.now())
        progress.persist()
        raise
    except Exception as error:
        progress.state.update(state='error', error_type=type(error).__name__)
        progress.persist()
        raise
    # A control experiment is eligible only as a complete registered group.
    gates = {(r['case_id'], r['repeat']): r for r in rows}
    groups = {}
    for c in selected.get('counterfactual', []):
        groups.setdefault(c['group_id'], []).append(c['case_id'])
    for gid, members in groups.items():
        for repeat in range(args.repeats):
            complete = all(gates.get((cid, repeat), {}).get('eligible') is True for cid in members)
            if not complete:
                for cid in members:
                    row = gates[(cid, repeat)]
                    row.setdefault('individual_preparation_reason', row['reason'])
                    row.update(eligible=False, group_id=gid,
                               reason='controlled_group_preparation_incomplete')
    for row in rows:
        write_json(directory / f"repeat_{row['repeat']}" / row['case_id'] / 'eligibility.json', row)
    write_json(directory / 'manifest.json', {'eligible': sum(r['eligible'] for r in rows), 'registered': len(rows),
               'scope': 'shared_case_repeat_initial_conditions_before_tested_actors',
               'selection_reads_tested_model_results': False, 'rows': rows})
    write_csv(directory / 'comparison.csv', [{k: v for k, v in r.items() if not isinstance(v, (dict, list))} for r in rows])
    progress.finish(rows)
    return gates
