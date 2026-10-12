"""Official execution uses the new transport with the frozen v2 job rules.

Preparation gate, sharing, recovery, aggregation and metrics remain unchanged.
The explicit lifecycle variant avoids changing legacy code or its run fingerprint.
"""
from run_online_v2 import (
    FILES,
    LimitedModel,
    StorageIOError,
    archive_shared_episode,
    asyncio,
    checkpoint_key,
    copy,
    coverage_report,
    enforce_coverage,
    job_path,
    json,
    metric_record,
    now,
    open_append,
    read_cases,
    read_json,
    recover_failed_checkpoints,
    rejudgeable_episode,
    retryable_infrastructure_episode,
    status,
    summarize,
    traceback,
    write_json
)
from .evaluation import evaluate_episode, rejudge_saved_episode
from .evaluation_recovery import recover_evaluation, save_evaluation_failure


async def obtain_checkpoint(case, repeat, selected_cases, root, prep_model, opts, locks):
    from eventarena_official.engine import prepare_checkpoint, prepare_state_pair
    if case.get('group_type') == 'state':
        key = f"state_pair_{case['group_id']}_repeat_{repeat}"
        async with locks.setdefault(key, asyncio.Lock()):
            directory = root / 'checkpoints' / key
            pair = read_json(directory / 'state_pair.json')
            if not pair:
                members = [c for c in selected_cases if c.get('group_type') == 'state'
                           and c['group_id'] == case['group_id']]
                pair = await prepare_state_pair(members, prep_model, directory, opts)
                write_json(directory / 'state_pair.json', pair)
            checkpoint = copy.deepcopy(pair.get('checkpoints', {}).get(case['case_id'], {}))
            return pair, checkpoint, key
    key = checkpoint_key(case, repeat)
    async with locks.setdefault(key, asyncio.Lock()):
        directory = root / 'checkpoints' / key
        prepared = read_json(directory / 'episode.json')
        if not prepared:
            prep_case = copy.deepcopy(case)
            source_id = case.get('cf', {}).get('checkpoint_source_case')
            source = next((c for c in selected_cases if c['case_id'] == source_id), case)
            prep_case = copy.deepcopy(source)
            prep_case['goal'] = case.get('checkpoint_navigation_goal', source['goal'])
            prepared = await prepare_checkpoint(prep_case, prep_model, directory, opts)
        checkpoint = copy.deepcopy(prepared.get('checkpoint', {}))
        return prepared, checkpoint, key


async def execute(args, root, selected, names):
    from eventarena.providers import Model
    from eventarena_official.engine import run_episode, run_checkpoint_episode
    models = {name: LimitedModel(Model(name, args.config), root, name)
              for name in set(names + [args.prep_model, args.judge_model]) if name}
    locks, done = {}, 0
    retried_shared = set()
    manifest = read_json(root / 'manifest.json')
    cases = {(experiment, case['case_id']): case for experiment, rows in selected.items() for case in rows}
    single_cases = read_cases(args.data_dir / FILES['main'])
    original_by_id = {c['case_id']: c for c in single_cases}
    queue = asyncio.Queue()
    if args.retry_infra:
        recover_failed_checkpoints(root)
    eligibility = None
    if getattr(args,'preparation_mode','on-demand') == 'before-actors':
        from eventarena_official.preparation import prepare_pool
        eligibility = await prepare_pool(args,root,selected,models,locks,obtain_checkpoint)
        gate_report = coverage_report(selected, eligibility, args.repeats,
                                      getattr(args, 'min_preparation_coverage', 0.9))
        write_json(root / 'preparation_gate.json', gate_report)
        print('[PREP GATE] ' + json.dumps(gate_report['experiments'], ensure_ascii=False), flush=True)
        if not gate_report['passed']:
            write_json(root / 'status.json', status(root))
        enforce_coverage(gate_report)
        if args.command == 'prepare':
            return
    for job in manifest['jobs']:
        queue.put_nowait(job)

    async def worker():
        nonlocal done
        while not queue.empty():
            blocked={name:wrapper.model.blocked.diagnostics for name,wrapper in models.items()
                     if getattr(wrapper.model,'blocked',None) is not None}
            if blocked:
                write_json(root/'provider_pause.json',{'stage':'actors','providers':blocked,
                    'resume':'Restore provider access and use the same run with --resume --retry-infra.'})
                raise RuntimeError('Provider access/quota unavailable; run paused before sampling further jobs')
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            folder = job_path(root, job)
            folder.mkdir(parents=True, exist_ok=True)
            old_job = read_json(folder / 'job.json', {})
            old_episode = read_json(folder / 'episode.json')
            retryable = retryable_infrastructure_episode(old_episode)
            if args.resume and old_job.get('state') == 'finished' and not (args.retry_infra and retryable):
                done += 1
                print(f"[{done}/{len(manifest['jobs'])}] CACHED {job['model']} {job['experiment']}/{job['view']} repeat={job['repeat']} {job['case_id']}", flush=True)
                queue.task_done()
                continue
            attempt = old_job.get('attempt', 0) + 1
            attempt_folder = folder / 'attempts' / f'attempt_{attempt:03d}'
            attempt_folder.mkdir(parents=True, exist_ok=True)
            if old_episode:
                write_json(folder / 'previous_attempts' / f'episode_{attempt-1}.json', old_episode)
            state = {**job, 'state': 'running', 'attempt': attempt, 'started_at': now(), 'updated_at': now()}
            write_json(folder / 'job.json', state)

            def progress(info):
                state.update(updated_at=now(), progress=info)
                write_json(folder / 'job.json', state)
                with open_append(root / 'progress.jsonl') as stream:
                    stream.write(json.dumps({'timestamp': now(), **job, **info}, ensure_ascii=False) + '\n')
                print(f"  {job['model']} {job['case_id']} {job['experiment']}/{job['view']} {info.get('stage','')} {info.get('state','')}", flush=True)

            judge_records = []

            async def judge(messages):
                row = {'messages': messages, 'api_ok': False,'context_access_records':[]}
                progress({'stage': 'judge', 'state': 'calling'})
                try:
                    from eventarena_official.judge_context import call_evidence_judge
                    row['raw'] = await call_evidence_judge(models[args.judge_model],messages,row['context_access_records'])
                    row['api_ok'] = True
                    return row['raw']
                except Exception as error:
                    row['error_type'] = type(error).__name__
                    raise
                finally:
                    judge_records.append(row)
                    write_json(attempt_folder / 'judge_responses.json', judge_records)
                    progress({'stage': 'judge', 'state': 'returned', 'api_ok': row['api_ok']})

            opts = {k: getattr(args, k) for k in ('headless', 'max_steps', 'pre_steps', 'setup_steps',
                                                 'max_context_chars', 'loop_limit', 'format_retries')}
            opts.update(repeat=job['repeat'], progress=progress, judge=judge if args.judge_model else None)
            case = cases[(job['experiment'], job['case_id'])]
            try:
                share = job['experiment'] == 'main' or (job['experiment'] == 'ablation' and job['view'] == 'trajectory')
                if old_job.get('state') == 'evaluation_pending':
                    saved_actor = read_json(old_job['evaluation_recovery']['actor_episode_file'])
                    write_json(attempt_folder / 'episode.json', saved_actor)
                    episode = await recover_evaluation(case, old_job, folder, judge if args.judge_model else None)
                    if share:
                        shared_file = root / 'shared_episodes' / job['model'] / f"repeat_{job['repeat']}" / case['case_id'] / 'episode.json'
                        episode['shared_episode_file'] = old_job['evaluation_recovery']['actor_episode_file']
                        write_json(shared_file, episode)
                elif args.retry_infra and rejudgeable_episode(old_episode) and not share:
                    episode = await rejudge_saved_episode(case, old_episode, judge if args.judge_model else None)
                elif job['experiment'] in {'main', 'ablation', 'counterfactual', 'baseline', 'multi'}:
                    source_case = original_by_id[case['original_case_id']] if job['experiment'] == 'baseline' else case
                    source_pool = selected['counterfactual'] if job['experiment'] == 'counterfactual' else single_cases
                    gate = eligibility.get((source_case['case_id'],job['repeat'])) if eligibility is not None else None
                    if gate is not None and gate['eligible'] is not True:
                        # A frozen failed gate cannot be repaired by a tested
                        # view/model. Do not issue preparer calls per skipped job.
                        prepared = {'status': gate.get('preparation_status')}
                        cp, key = {}, gate.get('checkpoint_key')
                    else:
                        prepared, cp, key = await obtain_checkpoint(source_case, job['repeat'], source_pool,
                                                                  root, models[args.prep_model], opts, locks)
                    if prepared.get('status') != 'checkpoint_ready' or not cp or (gate is not None and not gate['eligible']):
                        episode = {'case_id': case['case_id'], 'repeat': job['repeat'], 'view': job['view'],
                                   'protocol': 'controlled_episode_v2', 'status': 'checkpoint_unavailable',
                                   'decisions': {}, 'follow_ups': {}, 'responses': [],
                                   'preparation_status': prepared.get('status'),
                                   'preparation_failure_status': prepared.get('preparation_failure_status'),
                                   'preparation_failure_is_model': prepared.get('preparation_failure_is_model'),
                                   'checkpoint_key':key,
                                   'eligibility_status': gate,
                                   'evaluation': {'episode_success': None, 'branch_success': None,
                                                  'environment_valid':False,
                                                  'failure_category': 'checkpoint_preparation_failed',
                                                  'common_preparer_failure_not_tested_actor':True}}
                    else:
                        cp['goal'] = case['goal']
                        cp['events'] = copy.deepcopy(case.get('events', []))
                        cp['event'] = copy.deepcopy(cp['events'][0]) if len(cp['events']) == 1 else None
                        if share:
                            cache_key = f"full/{job['model']}/{job['repeat']}/{case['case_id']}"
                            async with locks.setdefault(cache_key, asyncio.Lock()):
                                shared_file = root / 'shared_episodes' / job['model'] / f"repeat_{job['repeat']}" / case['case_id'] / 'episode.json'
                                episode = read_json(shared_file)
                                if args.retry_infra and cache_key not in retried_shared:
                                    # Main and Full Trajectory are two reports of one
                                    # episode. Recovery must not rerun it for each alias.
                                    retried_shared.add(cache_key)
                                    if retryable_infrastructure_episode(episode):
                                        archived = archive_shared_episode(shared_file, episode, attempt)
                                        if rejudgeable_episode(episode):
                                            episode = await rejudge_saved_episode(case, episode, judge if args.judge_model else None)
                                            episode['infrastructure_recovery'][-1]['preserved_shared_episode'] = str(archived)
                                            write_json(shared_file, episode)
                                        else:
                                            # No valid complete trajectory exists;
                                            # only this shared episode is replayed.
                                            shared_file.unlink()
                                            episode = None
                                if episode is None:
                                    episode = await run_checkpoint_episode(case, models[job['model']], cp, 'trajectory', attempt_folder, opts)
                                    episode['shared_episode_file'] = str(attempt_folder / 'episode.json')
                                    write_json(shared_file, episode)
                                else:
                                    episode = copy.deepcopy(episode)
                        else:
                            episode = await run_checkpoint_episode(case, models[job['model']], cp, job['view'], attempt_folder, opts,
                                                                   suppress_events=job['experiment'] == 'baseline')
                        episode['checkpoint_key'] = key
                else:
                    episode = await run_episode(case, models[job['model']], attempt_folder, opts)
                    if not episode.get('evaluation'):
                        episode['evaluation'] = await evaluate_episode(case, episode, judge if args.judge_model else None)
                episode.update(repeat=job['repeat'], view=job['view'], model=job['model'], experiment=job['experiment'])
                episode['judge_responses'] = episode.get('judge_responses', []) + judge_records
                episode['attempt_directory'] = str(attempt_folder)
                write_json(folder / 'episode.json', episode)
                write_json(folder / 'metric_record.json', metric_record(episode))
                state.update(state='finished', updated_at=now(), result=episode.get('evaluation', {}).get('episode_success'),
                             result_status=episode.get('status'))
                write_json(folder / 'job.json', state)
                done += 1
                prep_note = f" (preparation={episode['preparation_status']})" if episode.get('preparation_status') else ''
                print(f"[{done}/{len(manifest['jobs'])}] DONE {job['model']} {job['experiment']}/{job['view']} repeat={job['repeat']} {job['case_id']}: {state['result_status']}{prep_note} ESR={state['result']}", flush=True)
            except asyncio.CancelledError:
                state.update(state='interrupted', updated_at=now())
                write_json(folder / 'job.json', state)
                raise
            except StorageIOError:
                # A storage outage leaves the job resumable, never scored.
                state.update(state='interrupted', updated_at=now(), error_type='StorageIOError')
                try:
                    write_json(folder / 'job.json', state)
                except OSError:
                    pass  # Retain the original error if this drive is still unavailable.
                raise
            except Exception as error:
                if save_evaluation_failure(folder, attempt_folder, state, error):
                    print(f"[{done+1}/{len(manifest['jobs'])}] EVALUATION PENDING {job['id']}: Actor trace preserved; resume retries scoring only", flush=True)
                    (attempt_folder / 'error_traceback.txt').write_text(''.join(traceback.format_tb(error.__traceback__)), encoding='utf-8')
                    raise RuntimeError(f"Evaluation failed after completed Actor trace; resume scoring from {attempt_folder}") from None
                # Never log provider response bodies, authorization or exception text.
                episode = {'case_id': case['case_id'], 'repeat': job['repeat'], 'view': job['view'],
                           'status': 'runner_error', 'error_type': type(error).__name__, 'responses': [],
                           'evaluation': {'episode_success': None, 'failure_category': 'evaluation_unverified'}}
                write_json(folder / 'episode.json', episode)
                write_json(folder / 'metric_record.json', metric_record(episode))
                state.update(state='finished', updated_at=now(), result_status='runner_error', error_type=type(error).__name__)
                write_json(folder / 'job.json', state)
                done += 1
                print(f"[{done}/{len(manifest['jobs'])}] UNVERIFIED {job['id']} {type(error).__name__}", flush=True)
                # Frame locations identify implementation failures without
                # printing exception bodies, response headers, or local values.
                (attempt_folder / 'error_traceback.txt').write_text(''.join(traceback.format_tb(error.__traceback__)), encoding='utf-8')
                raise RuntimeError(f"Runner stopped after {type(error).__name__}; inspect {attempt_folder / 'error_traceback.txt'}") from None
            finally:
                queue.task_done()
                if done % 20 == 0 or queue.empty():
                    summarize(root, selected, names, args)

    await asyncio.gather(*(worker() for _ in range(args.workers)))


