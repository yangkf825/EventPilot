#!/usr/bin/env python3
"""Official task event extensions, reusing the frozen EventArena v2 tools/metrics."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
import run_online_v2 as legacy
from eventarena.run_lock import exclusive_run
from eventarena_official import PROTOCOL
from eventarena_official.registration import validate_extension
from eventarena_official.transport import POLICY
from eventarena_official.context_access import POLICY as CONTEXT_POLICY,LIMITS
from eventarena_official.browser import REPLAY_POLICY
from eventarena_official.preparation_import import plan_import, materialize_import
from eventarena_official.evaluation import POLICY as EVALUATION_SCHEMA_POLICY
from eventarena_official.evaluation_recovery import migrate_constraint_schema
from eventarena_official.judge_context import POLICY as JUDGE_CONTEXT_POLICY, MAX_INLINE_CHARS


def fingerprint():
    # Also binds every reused tool, evaluator and metric implementation.
    paths=[Path(__file__),ROOT/'scripts/official_online.py']+sorted((ROOT/'eventarena_official').glob('*.py'))
    return legacy.sha({'legacy_engine_sha256':legacy.code_fingerprint(),
                       'official_files':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}})


def main(argv=None):
    cli=legacy.parser()
    cli.description=__doc__
    cli.set_defaults(data_dir=ROOT/'data/official_online/events_pilot')
    cli.add_argument('--preparation-from-run', type=Path,
                     help='Reuse compatible factual preparation from an unchanged source with no tested actors started')
    cli.add_argument('--prep-workers',type=int,default=2,help='Parallel preparation units; each website is prepared serially')
    args=cli.parse_args(argv)
    root=args.run_root.expanduser().resolve()
    if args.command=='status':
        print(json.dumps(legacy.status(root),ensure_ascii=False,indent=2));return
    if args.command=='evaluate':
        with exclusive_run(root):
            saved=legacy.read_json(root/'manifest.json')
            if not saved or saved.get('protocol_version')!=PROTOCOL or saved.get('code_sha256')!=fingerprint():
                raise ValueError('Use the frozen official extension protocol/code of this run')
            for k,v in saved['arguments'].items():setattr(args,k,v)
            selected={e:legacy.read_cases(root/'registered_data'/(e+'.jsonl')) for e in saved['counts']}
            legacy.summarize(root,selected,saved['models'],args)
        return
    if args.preparation_mode not in {'before-actors','casewise'}:
        raise ValueError('Shared qualification is required before each tested case/group')
    if args.prep_workers<1 or args.prep_workers>4:
        raise ValueError('prep-workers must be between 1 and 4')
    if args.preparation_mode=='casewise' and args.repeats!=1:
        raise ValueError('The casewise reporting protocol requires repeats=1')
    args.config=args.config.resolve();args.data_dir=args.data_dir.resolve()
    validation=validate_extension(args.data_dir,strict_review=args.strict_review)
    args.experiments=list(legacy.EXPERIMENTS) if args.experiments=='all' else args.experiments.split(',')
    if len(set(args.experiments))!=len(args.experiments) or any(e not in legacy.EXPERIMENTS for e in args.experiments):
        raise ValueError('Unknown/duplicate experiment')
    if min(args.repeats,args.workers,args.single_limit,args.cf_groups,args.multi_limit,args.max_steps,
           args.pre_steps,args.setup_steps,args.max_context_chars,args.loop_limit)<1:
        raise ValueError('Budgets must be positive')
    if not 0<=args.min_preparation_coverage<=1:
        raise ValueError('Preparation coverage must be between 0 and 1')
    configured={m['name']:m for m in legacy.read_json(args.config)['models']}
    names=args.only.split(',') if args.only else [n for n,m in configured.items() if m.get('enabled')]
    if not names or len(set(names))!=len(names) or any(n not in configured for n in names):
        raise ValueError('Select unique configured models')
    args.prep_model=args.prep_model or names[0]
    args.judge_model=None if args.judge_model=='none' else args.judge_model or names[0]
    if any(n not in configured for n in (args.prep_model,args.judge_model) if n):
        raise ValueError('Unknown fixed preparation/Judge model')
    selected=legacy.select_cases(args)
    empty=[e for e,c in selected.items() if not c]
    if empty:
        raise ValueError('No registered cases for '+','.join(empty)+'; select only built experiment sets. Empty sets cannot silently count as completed experiments.')
    jobs=legacy.make_jobs(selected,names,args.repeats,args.seed)
    manifest=legacy.make_manifest(args,selected,names,configured,jobs)
    manifest['arguments']['prep_workers']=args.prep_workers
    manifest.update(schema_version=PROTOCOL,protocol_version=PROTOCOL,code_sha256=fingerprint(),
                    input_transport_policy=POLICY,
                    context_access_policy=CONTEXT_POLICY,context_read_limits=LIMITS,
                    checkpoint_replay_policy=REPLAY_POLICY,
                    evaluation_schema_policy=EVALUATION_SCHEMA_POLICY,
                    judge_context_policy=JUDGE_CONTEXT_POLICY,judge_max_inline_chars=MAX_INLINE_CHARS,
                    official_source=legacy.read_json(args.data_dir/'manifest.json')['source'],
                    extension_validation=validation,official_leaderboard_comparable=False)
    if args.preparation_mode=='casewise':
        from eventarena_official.casewise import POLICY as AVAILABILITY_POLICY
        manifest.update(availability_policy=AVAILABILITY_POLICY,global_coverage_abort=False,
                        primary_score_scope='common_qualified_cases; runtime_unknowns_and_coverage_reported_separately')
    saved_import=legacy.read_json(root/'manifest.json') if args.resume else None
    if saved_import and saved_import.get('preparation_import'):
        import_plan=saved_import['preparation_import']
        if not args.preparation_from_run or Path(import_plan['source_run']).resolve()!=args.preparation_from_run.resolve():
            raise ValueError('Resume must retain the registered preparation source')
    else:
        import_plan=plan_import(args.preparation_from_run, manifest) if args.preparation_from_run else None
    manifest['preparation_import']=import_plan
    print(json.dumps({'protocol':PROTOCOL,'models':names,'counts':manifest['counts'],
                      'registered_jobs':len(jobs),'repeats':args.repeats,'no_api_call':args.dry_run,
                      'preparation_mode':args.preparation_mode,'prep_workers':args.prep_workers,
                      'global_coverage_abort':args.preparation_mode=='before-actors',
                      'imported_preparation_files':len(import_plan['files']) if import_plan else 0,
                      'gold_human_reviewed':manifest['gold_human_reviewed'],
                      'original_A_goals':'preserved_exactly','metrics':'existing_EventArena_v2'},ensure_ascii=False,indent=2),flush=True)
    if args.dry_run:return
    if not args.judge_model:
        raise ValueError('Live official tasks require a fixed evidence Judge; deterministic fixtures use tests')
    for n in set(names+[args.prep_model,args.judge_model]):
        if n and not os.environ.get(configured[n]['api_key_env']):
            raise ValueError('Set '+configured[n]['api_key_env']+' in your terminal')
    with exclusive_run(root):
        previous=legacy.read_json(root/'manifest.json')
        if previous:
            if args.resume and previous!=manifest:
                migrate_constraint_schema(root,previous,manifest)
                previous=legacy.read_json(root/'manifest.json')
            if not args.resume or previous!=manifest:
                raise ValueError('Existing run requires --resume and identical protocol, source, cases, code and settings')
        elif root.exists() and any(p.name!='logs' for p in root.iterdir()):
            raise ValueError('Nonempty result directory without a manifest')
        root.mkdir(parents=True,exist_ok=True)
        legacy.write_json(root/'manifest.json',manifest)
        if import_plan:
            materialize_import(root, import_plan)
            print('[PREP IMPORT] Reusing compatible factual preparation; verified qualification is reused only for identical cases/policies. No tested actor scores imported.',flush=True)
        from eventarena_official.corpus import write_jsonl
        for e,cases in selected.items():write_jsonl(root/'registered_data'/(e+'.jsonl'),cases)
        runtime=os.environ.get('EA_M2W_RUNTIME')
        if runtime:os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',runtime+'/browsers')
        from eventarena_official.execution import execute
        asyncio.run(execute(args,root,selected,names))
        legacy.summarize(root,selected,names,args)
        print('Official-source extension finished:',root,flush=True)


if __name__=='__main__':
    main()
