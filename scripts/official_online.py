#!/usr/bin/env python3
"""Import official Online-Mind2Web, test live entries, register and run extensions."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts')]
from eventarena_official.corpus import import_corpus, load_corpus


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', type=Path, default=ROOT/'data/official_online')
    subs = p.add_subparsers(dest='command', required=True)
    imp = subs.add_parser('import')
    imp.add_argument('--input', type=Path, required=True)
    imp.add_argument('--revision', help='Pinned official HF Git revision, if independently verified')
    check = subs.add_parser('check-sites')
    check.add_argument('--output', type=Path, required=True)
    check.add_argument('--task-ids', help='Comma-separated exact official task IDs')
    check.add_argument('--workers', type=int, default=3)
    check.add_argument('--timeout', type=float, default=25)
    check.add_argument('--resume', action='store_true')
    check.add_argument('--retry-failed', action='store_true')
    check.add_argument('--screenshots', action='store_true')
    audit=subs.add_parser('audit-sites',help='Reclassify all saved site observations; keep original captures')
    audit.add_argument('--report',type=Path,required=True)
    audit.add_argument('--output',type=Path,required=True)
    pairs=subs.add_parser('pairs',help='List related official A/B candidates; no automatic Gold labels')
    pairs.add_argument('--availability',type=Path)
    pairs.add_argument('--output',type=Path,required=True)
    paths=subs.add_parser('probe-tasks',help='Check author-registered public paths; no LLM model score')
    paths.add_argument('--plans',type=Path,required=True)
    paths.add_argument('--output',type=Path,required=True)
    paths.add_argument('--timeout',type=float,default=35)
    build=subs.add_parser('build-events',help='Compile registered author events without changing official A/B')
    build.add_argument('--spec',type=Path,required=True)
    build.add_argument('--output',type=Path,required=True)
    baseline=subs.add_parser('baseline',help='Run exact official tasks without events before authoring triggers')
    baseline.add_argument('--config',type=Path,default=ROOT/'models.gateway.json')
    baseline.add_argument('--only',required=True,help='One configured Actor alias')
    baseline.add_argument('--judge-model',help='Keep this alias fixed across tested actors')
    baseline.add_argument('--task-ids')
    baseline.add_argument('--limit',type=int,default=5)
    baseline.add_argument('--availability',type=Path,required=True)
    baseline.add_argument('--run-root',type=Path,required=True)
    baseline.add_argument('--max-steps',type=int,default=40)
    baseline.add_argument('--max-context-chars',type=int,default=450000)
    baseline.add_argument('--episode-timeout',type=float,default=600)
    baseline.add_argument('--headless',action='store_true')
    baseline.add_argument('--resume',action='store_true')
    baseline.add_argument('--retry-infra',action='store_true')
    baseline.add_argument('--dry-run',action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    runtime = os.environ.get('EA_M2W_RUNTIME')
    if runtime:
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', runtime+'/browsers')
    if args.command == 'import':
        result = import_corpus(args.input,args.data_dir,args.revision)
    elif args.command=='audit-sites':
        from eventarena_official.availability import audit_saved_report
        result=audit_saved_report(args.report,args.output)
        result={k:v for k,v in result.items() if k not in {'rows','tasks'}}
    elif args.command=='build-events':
        from eventarena_official.registration import compile_extension
        result=compile_extension(args.data_dir,args.spec,args.output)
    elif args.command=='probe-tasks':
        tasks,manifest=load_corpus(args.data_dir)
        from eventarena_official.feasibility import probe_tasks
        result=asyncio.run(probe_tasks(tasks,json.loads(args.plans.read_text()),manifest,args.output,args.timeout))
    elif args.command=='pairs':
        tasks,manifest=load_corpus(args.data_dir)
        available=None
        if args.availability:
            r=json.loads(args.availability.read_text())
            available={row['entry_url'] for row in r['rows'] if row['category']=='entry_available'}
        from eventarena_official.registration import pair_candidates
        from eventarena.storage_io import atomic_write_json
        pairs=pair_candidates(tasks,available);atomic_write_json(args.output,pairs)
        result={'candidate_pairs':len(pairs),'output':str(args.output),'gold_created':False}
    elif args.command=='baseline':
        tasks,args.source_manifest=load_corpus(args.data_dir)
        r=json.loads(args.availability.read_text())
        plan=json.loads((args.availability.parent/'manifest.json').read_text())
        if plan['source_sha256']!=args.source_manifest['raw_sha256']:
            raise ValueError('Site test belongs to a different official corpus')
        from eventarena_official.baseline import select_tasks,execute
        selected=select_tasks(tasks,args.task_ids,args.limit,r)
        args.judge_model=args.judge_model or args.only
        args.project_root=ROOT
        args.config=args.config.resolve()
        if min(args.limit,args.max_steps,args.max_context_chars,args.episode_timeout)<=0:
            raise ValueError('Use positive budgets')
        result=asyncio.run(execute(args,args.run_root.resolve(),selected))
    else:
        tasks,manifest = load_corpus(args.data_dir)
        if args.task_ids:
            ids = set(args.task_ids.split(','))
            if not ids.issubset({t['task_id'] for t in tasks}):
                raise ValueError('Unknown official task ID')
            tasks = [t for t in tasks if t['task_id'] in ids]
        if args.workers < 1 or not 0 < args.timeout <= 300:
            raise ValueError('Use positive workers and timeout <=300 seconds')
        from eventarena_official.availability import check_sites
        result = asyncio.run(check_sites(tasks,manifest,args.output,workers=args.workers,timeout=args.timeout,
                                         resume=args.resume,retry_failed=args.retry_failed,screenshots=args.screenshots))
        result = {k:v for k,v in result.items() if k not in {'rows','tasks'}}
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2),flush=True)


if __name__ == '__main__':
    main()
