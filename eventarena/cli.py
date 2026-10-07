"""Offline corpus preparation plus real-browser recording; no automatic Gold creation."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def configure():
    runtime = os.environ.get('EA_M2W_RUNTIME')
    if runtime:
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(Path(runtime) / 'browsers'))
    os.environ.setdefault('HF_HOME', str(ROOT / 'cache/huggingface'))


async def record(args):
    from .browser import Browser
    run = ROOT / 'runs/recordings' / datetime.now().strftime('%Y%m%d_%H%M%S')
    goal, url, task_id = args.goal, args.url, None
    if args.task_id:
        from .corpus import jsonl
        matches = [t for t in jsonl(ROOT / 'data/normalized/online_tasks.jsonl') if t['task_id']==args.task_id]
        if len(matches)!=1:
            raise ValueError('Unknown task_id; import the official Online_Mind2Web.json first')
        task = matches[0]
        goal, url, task_id = task['goal'], task['url'], task['task_id']
    if not url or not goal:
        raise ValueError('Provide --url and --goal, or --task-id')
    events = json.loads(Path(args.events).read_text()) if args.events else []
    print('Run directory:',run)
    print('Manual real-browser collection: inspect the browser, then issue JSON actions.')
    print('Commands: observe, checkpoint, done, or {"op":"CLICK","node_id":"0"}.')
    async with Browser(run, headless=args.headless) as browser:
        await browser.execute({'op':'GOTO','url':url})
        while True:
            obs = await browser.observe()
            print(json.dumps({'url':obs['url'],'title':obs['title'],'candidates':obs['candidates']},ensure_ascii=False))
            command = await asyncio.to_thread(input, 'Action> ')
            if command == 'done':
                await browser.save(None,'final_observation')
                break
            if command == 'observe':
                continue
            if command == 'checkpoint':
                if not events:
                    raise ValueError('Provide --events JSON to capture an event checkpoint')
                case = await browser.checkpoint_case(f'LIVE_{run.name}',goal,events,task_id)
                await browser.inject(events)
                print('Checkpoint saved:',case['case_id'],'; labels need independent review')
                continue
            try:
                await browser.execute(json.loads(command))
            except Exception as error:
                print(type(error).__name__,str(error))
    print('Saved factual trajectory, HTML, screenshots and Playwright trace:',run)


def import_online(path):
    rows=json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(rows,dict):
        rows=rows.get('tasks',rows.get('data',[]))
    output=ROOT / 'data/normalized/online_tasks.jsonl'
    ids=set()
    with output.open('w',encoding='utf-8') as stream:
        for row in rows:
            uid=row.get('task_id') or row.get('annotation_id')
            goal=row.get('confirmed_task') or row.get('task_description') or row.get('task')
            url=row.get('website') or row.get('url')
            if not uid or uid in ids or not goal or not isinstance(url,str) or not url.startswith('https://'):
                raise ValueError('Invalid official Online-Mind2Web task record')
            ids.add(uid)
            # reference_length is retained in raw data but deliberately omitted from agent inputs.
            stream.write(json.dumps({'task_id':uid,'goal':goal,'url':url,'level':row.get('level'),
                                     'source':'osunlp/Online-Mind2Web'},ensure_ascii=False)+'\n')
    print('Imported',len(ids),'official tasks to',output)


def main():
    configure()
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('normalize')
    action=commands.add_parser('action-eval');action.add_argument('--only',default='deepseek_flash')
    action.add_argument('--limit',type=int,default=10);action.add_argument('--split')
    action.add_argument('--dry-run',action='store_true')
    draft=commands.add_parser('candidates'); draft.add_argument('--limit',type=int,default=100)
    draft.add_argument('--seed',type=int,default=20261002)
    draft.add_argument('--split',choices=['train','test','test_task','test_website','test_domain'])
    imp=commands.add_parser('import-online');imp.add_argument('path')
    rec=commands.add_parser('record');rec.add_argument('--url');rec.add_argument('--goal')
    rec.add_argument('--task-id');rec.add_argument('--events');rec.add_argument('--headless',action='store_true')
    smoke=commands.add_parser('browser-check');smoke.add_argument('--url',default='https://example.com')
    agent=commands.add_parser('agent');agent.add_argument('--scenario',required=True)
    agent.add_argument('--only',default='deepseek_flash');agent.add_argument('--max-steps',type=int,default=20)
    agent.add_argument('--view',choices=['event-only','goal','state','trajectory'],default='trajectory')
    agent.add_argument('--headless',action='store_true')
    suite=commands.add_parser('suite');suite.add_argument('--only',default='deepseek_flash')
    suite.add_argument('--data-dir',default='data/events');suite.add_argument('--dry-run',action='store_true')
    suite.add_argument('--run-root',default='runs/offline_pilot')
    suite.add_argument('--resume',action='store_true')
    suite.add_argument('--strict-review',action='store_true')
    sub=commands.add_parser('validate-events');sub.add_argument('--data-dir',default='data/events')
    sub.add_argument('--strict-review',action='store_true')
    args=parser.parse_args()
    if args.command=='normalize':
        from .corpus import normalize
        print(json.dumps(normalize(),ensure_ascii=False,indent=2))
    elif args.command=='candidates':
        from .corpus import candidates
        print(json.dumps(candidates(args.limit,args.seed,args.split),ensure_ascii=False,indent=2))
    elif args.command=='action-eval':
        from .action_eval import run
        run(args)
    elif args.command=='import-online':
        import_online(args.path)
    elif args.command=='record':
        asyncio.run(record(args))
    elif args.command=='agent':
        from .agent import run
        asyncio.run(run(args))
    elif args.command=='browser-check':
        async def check():
            from .browser import Browser
            run=ROOT/'runs/browser_check'
            async with Browser(run,headless=True) as b:
                await b.execute({'op':'GOTO','url':args.url})
                obs=await b.observe()
                await b.save()
                print(json.dumps({'title':obs['title'],'url':obs['url'],'candidate_count':len(obs['candidates']),
                                 'real_browser':True,'trace':str(run/'trace.zip')},ensure_ascii=False))
        asyncio.run(check())
    elif args.command in ('suite','validate-events'):
        from .protocol import validate_directory
        folder=ROOT/args.data_dir
        validate_directory(folder,strict_review=args.strict_review)
        if args.command=='suite':
            for experiment,filename in [('main','single_event.jsonl'),('ablation','single_event.jsonl'),
                                        ('counterfactual','counterfactual.jsonl'),('multi','multi_event.jsonl')]:
                cmd=[sys.executable,str(ROOT/'scripts/run_models.py'),'run','--experiment',experiment,
                     '--only',args.only,'--data',str(folder/filename),
                     '--run-root',str(ROOT/args.run_root),'--config',str(ROOT/'models.json')]
                if args.dry_run:cmd.append('--dry-run')
                if args.resume:
                    run_dir=ROOT/args.run_root/experiment/args.only
                    if (run_dir/'manifest.json').is_file():
                        cmd.append('--resume')
                    elif run_dir.exists():
                        raise ValueError(f'{run_dir}: existing run has no manifest; cannot safely resume')
                subprocess.run(cmd,check=True)


if __name__=='__main__':
    main()
