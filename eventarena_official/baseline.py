"""No-event execution of exact official tasks; access tests are not outcomes."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path

from eventarena.run_lock import exclusive_run
from eventarena.storage_io import atomic_write_json, StorageIOError
from eventarena.providers import Model
from .engine import run_episode
from .transport import POLICY
from .context_access import POLICY as CONTEXT_POLICY,LIMITS
from eventarena.online_v2_metrics import summarize_tristate
from .corpus import digest, load_corpus, now, task_case
from .availability import save_csv


def fingerprint(root):
    paths=sorted((root/'eventarena').glob('*.py'))+sorted((root/'eventarena_official').glob('*.py'))
    paths+=[root/'scripts/official_online.py',root/'scripts/run_online_v2.py']
    return digest(json.dumps({str(p.relative_to(root)):digest(p.read_bytes()) for p in paths},sort_keys=True).encode())


def select_tasks(tasks,ids,limit,availability):
    if ids:
        requested=set(ids.split(','))
        if not requested.issubset({t['task_id'] for t in tasks}):
            raise ValueError('Unknown official task ID')
        selected=[t for t in tasks if t['task_id'] in requested]
    else:
        available={r['entry_url'] for r in availability['rows'] if r['category']=='entry_available'}
        selected=[t for t in tasks if t['url'] in available][:limit]
    if not selected:
        raise ValueError('No official tasks selected')
    return selected


def task_capability_review(task):
    """Conservative review flags only, not proof that a task is impossible."""
    import re
    goal=task['goal']
    checks=[('login_required',r'\b(?:log ?in|sign ?in)\b'),
            ('external_submission',r'\b(?:send|subscribe|subscription|submit a request|apply for|post a|leave a comment)\b'),
            ('transaction_review',r'\b(?:book|purchase|buy|order)\b')]
    return [name for name,pattern in checks if re.search(pattern,goal,re.I)]


async def execute(args,root,selected):
    from run_online_v2 import LimitedModel, public_config
    config=json.loads(args.config.read_text())
    configured={m['name']:m for m in config['models']}
    if args.only not in configured or args.judge_model not in configured:
        raise ValueError('Unknown configured Actor/Judge alias')
    flags={t['task_id']:task_capability_review(t) for t in selected}
    manifest={'schema':'official_no_event_baseline_v5','input_transport_policy':POLICY,
              'context_access_policy':CONTEXT_POLICY,'context_read_limits':LIMITS,
              'source_sha256':args.source_manifest['raw_sha256'],
              'source_revision':args.source_manifest['revision'],'tasks':selected,
              'task_capability_flags':flags,
              'models':public_config({n:configured[n] for n in {args.only,args.judge_model}}),
              'actor':args.only,'judge':args.judge_model,'code_sha256':fingerprint(args.project_root),
              'max_steps':args.max_steps,'max_context_chars':args.max_context_chars,
              'episode_timeout_s':args.episode_timeout,'headless':args.headless,
              'no_event':True,'task_goal_policy':'exact_official_goal_and_entry',
              'scope':'research browser tools; external submissions/login/transactions require capability review'}
    if args.dry_run:
        print(json.dumps({'tasks':len(selected),'actor':args.only,'judge':args.judge_model,
                          'capability_review':flags,'no_api_call':True},ensure_ascii=False,indent=2));return
    for name in {args.only,args.judge_model}:
        if not os.environ.get(configured[name]['api_key_env']):
            raise ValueError('Set '+configured[name]['api_key_env']+' in your terminal first')
    with exclusive_run(root):
        saved=root/'manifest.json'
        if saved.exists():
            if not args.resume or json.loads(saved.read_text())!=manifest:
                raise ValueError('Resume requires identical official tasks, code, Actor/Judge and budgets')
        elif any(p.name!='logs' for p in root.iterdir()):
            raise ValueError('Nonempty unregistered run directory')
        atomic_write_json(saved,manifest)
        actor=LimitedModel(Model(args.only,args.config),root,args.only)
        judge_model=LimitedModel(Model(args.judge_model,args.config),root,args.judge_model)
        results=[]
        for n,task in enumerate(selected,1):
            directory=root/'episodes'/('OMW_'+task['task_id'])
            result_path=directory/'result.json'
            previous=json.loads(result_path.read_text()) if result_path.exists() else None
            # API/site/Judge unknowns may be retried explicitly; scored failures stay frozen.
            if previous and args.resume and not (args.retry_infra and previous.get('success') is None):
                results.append(previous)
                print(f"[TASK {n}/{len(selected)}] CACHED {task['task_id']} {previous['status']}",flush=True);continue
            if previous:
                atomic_write_json(directory/'previous_results'/f"{now().replace(':','_')}.json",previous)
            directory.mkdir(parents=True,exist_ok=True)
            if flags[task['task_id']]:
                result={'task_id':task['task_id'],'goal':task['goal'],'entry_url':task['url'],
                        'status':'capability_review_required','flags':flags[task['task_id']],
                        'success':None,'task_success_verified':False,'actor_called':False}
            else:
                print(f"[TASK {n}/{len(selected)}] START {task['task_id']} {task['url']}",flush=True)
                def progress(row):
                    atomic_write_json(root/'status.json',{'task_id':task['task_id'],'completed':len(results),
                                                          'total':len(selected),'updated_at':now(),**row})
                    print(f"  {task['task_id']} {row.get('stage')} {row.get('state')}",flush=True)
                async def judge(messages):return await judge_model.call(messages)
                try:
                    async with asyncio.timeout(args.episode_timeout):
                        episode=await run_episode(task_case(task),actor,directory/'trace',
                            {'headless':args.headless,'max_steps':args.max_steps,'max_context_chars':args.max_context_chars,
                             'format_retries':1,'loop_limit':4,'judge':judge,'progress':progress})
                    success=episode['evaluation']['phase_results']['CURRENT_TASK']['success']
                    result={'task_id':task['task_id'],'goal':task['goal'],'entry_url':task['url'],
                            'status':episode['status'],'success':success,
                            'task_success_verified':success is True,'actor_called':True,
                            'verification':episode['evaluation']['phase_results']['CURRENT_TASK'],
                            'context_read_count':len(episode.get('context_reads',[])),
                            'context_access_policy':CONTEXT_POLICY,
                            'episode_file':str(directory/'trace/episode.json')}
                except StorageIOError:raise
                except TimeoutError:
                    result={'task_id':task['task_id'],'goal':task['goal'],'entry_url':task['url'],
                            'status':'episode_timeout','success':None,'task_success_verified':False,'actor_called':True}
            atomic_write_json(result_path,result);results.append(result)
            report={'timestamp_utc':now(),'completed':len(results),'registered':len(selected),
                    'TaskCompletion':summarize_tristate([r['success'] for r in results]+[None]*(len(selected)-len(results))),
                    'fixed_judge':args.judge_model,'results':results,'official_leaderboard_comparable':False}
            atomic_write_json(root/'task_results.json',report);save_csv(root/'task_results.csv',results)
            print(f"[TASK {n}/{len(selected)}] DONE {task['task_id']} success={result['success']} {result['status']}",flush=True)
        # An entirely cached resume must still return its existing outcomes.
        report={'timestamp_utc':now(),'completed':len(results),'registered':len(selected),
                'TaskCompletion':summarize_tristate([r['success'] for r in results]),
                'fixed_judge':args.judge_model,'results':results,'official_leaderboard_comparable':False}
        atomic_write_json(root/'task_results.json',report);save_csv(root/'task_results.csv',results)
        return report
