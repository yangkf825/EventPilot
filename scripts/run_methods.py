#!/usr/bin/env python3
"""Run paper-method comparisons with isolated records and one API worker pool.

Methods run sequentially; cases inside each method run concurrently. This keeps
the requested API concurrency global rather than multiplying it by five.
"""
from __future__ import annotations
import argparse
import csv
import getpass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import run as runner
from eventpilot.io import read_json,write_json,file_digest,digest
from eventpilot.methods.paper import METHODS
from eventpilot.report import atomic_csv,export


def merge_tables(root,methods,planned):
    """Merge every method without dropping unstarted registered denominators."""
    out=root/'tables'
    states=[]
    for method in methods:
        summary=read_json(root/method/'tables/summary.json',{})
        states.append({'method':method,'registered_canonical':planned[method],
                       'recorded':summary.get('recorded',0),
                       'pending':planned[method]-summary.get('recorded',0),
                       'complete_run':summary.get('complete_run',False),
                       'unknown_execution':summary.get('unknown_execution',0)})
    csv_names=sorted({p.name for method in methods for p in (root/method/'tables').glob('*.csv')})
    for name in csv_names:
        rows=[]
        for method in methods:
            path=root/method/'tables'/name
            if path.exists():
                with path.open(encoding='utf-8-sig',newline='') as stream:
                    rows.extend({**row,'method':method} for row in csv.DictReader(stream))
        atomic_csv(out/name,rows)
    total={'methods':states,'registered_canonical':sum(planned.values()),
           'recorded':sum(s['recorded'] for s in states),
           'complete_run':all(s['complete_run'] for s in states),
           'execution_scope':'offline teacher-forced action/control predictions; no backend outcomes',
           'main_view':'state','no_api_calls_during_export':True}
    write_json(out/'suite_summary.json',total)
    atomic_csv(out/'method_completion.csv',states)
    return total


def child_command(a,method):
    command=[sys.executable,'-u',str(ROOT/'scripts/run.py'),
             '--config',str(a.config),'--data',str(a.data),
             '--corpus-root',str(a.corpus_root),'--only',a.only,
             '--experiments',a.experiments,'--deliveries',a.deliveries,
             '--run-root',str(a.run_root/method),'--method',method]
    for key in ('workers','api_concurrency','repeats','max_commands','candidate_limit',
                'output_tokens','max_model_requests','reflexion_trials','job_order_seed'):
        command.extend(['--'+key.replace('_','-'),str(getattr(a,key))])
    for key in ('case_ids','case_limit'):
        if getattr(a,key) is not None:command.extend(['--'+key.replace('_','-'),str(getattr(a,key))])
    for key in ('resume','retry_infra','dry_run'):
        if getattr(a,key):command.append('--'+key.replace('_','-'))
    return command


def stream_child(command,log):
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('a',encoding='utf-8',buffering=1) as stream:
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                 text=True,encoding='utf-8',errors='replace',bufsize=1)
        try:
            for line in process.stdout:
                print(line,end='',flush=True);stream.write(line)
            return process.wait()
        except KeyboardInterrupt:
            if process.poll() is None:process.send_signal(signal.SIGINT)
            try:process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate();process.wait(timeout=10)
            raise


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-root',type=Path,required=True)
    p.add_argument('--methods',default='all',help='all or comma-separated direct,cot,react,reflexion,ps')
    p.add_argument('--only',default='gateway_ds')
    p.add_argument('--config',type=Path,default=ROOT/'models.json')
    p.add_argument('--data',type=Path,default=ROOT/'data')
    p.add_argument('--corpus-root',type=Path,default=ROOT/'corpus')
    p.add_argument('--experiments',default='main')
    p.add_argument('--deliveries',default=','.join(runner.DELIVERIES))
    p.add_argument('--case-ids')
    p.add_argument('--case-limit',type=int)
    for name,value in [('workers',8),('api-concurrency',4),('repeats',1),('max-commands',128),
                       ('candidate-limit',50),('output-tokens',8192),('max-model-requests',1024),
                       ('job-order-seed',20261105)]:p.add_argument('--'+name,type=int,default=value)
    p.add_argument('--reflexion-trials',type=int,choices=(1,2,3),default=2)
    for flag in ('resume','retry-infra','dry-run','export-only'):p.add_argument('--'+flag,action='store_true')
    a=p.parse_args(argv);a.run_root=a.run_root.expanduser().resolve()
    methods=list(METHODS) if a.methods=='all' else a.methods.split(',')
    if not methods or len(set(methods))!=len(methods) or not set(methods)<=set(METHODS):p.error('Unknown/duplicate methods')
    saved=read_json(a.run_root/'suite_manifest.json')
    if a.export_only:
        if not saved:p.error('No suite_manifest.json in run-root')
        for method in saved['methods']:
            if (a.run_root/method/'run_manifest.json').exists():export(a.run_root/method)
        print(json.dumps(merge_tables(a.run_root,saved['methods'],saved['planned']),indent=2));return
    for key in ('workers','api_concurrency','repeats','max_commands','candidate_limit','output_tokens','max_model_requests'):
        if getattr(a,key)<1:p.error(key+' must be positive')
    if a.case_limit is not None and a.case_limit<1:p.error('case-limit must be positive')
    names=a.only.split(',');experiments=a.experiments.split(',');deliveries=a.deliveries.split(',')
    for values,allowed in ((names,None),(experiments,runner.AVAILABLE_EXPERIMENTS),(deliveries,runner.DELIVERIES)):
        if not values or len(set(values))!=len(values) or allowed and not set(values)<=set(allowed):p.error('Unknown/duplicate selection')
    config=read_json(a.config)
    selected=[m for m in config['models'] if m['name'] in names]
    if len(selected)!=len(names):p.error('Unknown configured model')
    if any('api_key' in m or 'key' in m for m in config['models']):p.error('Config must not contain credentials')
    cases,controls,_,dm=runner.load_data(a.data)
    cases=runner.filter_cases(cases,a.case_ids,a.case_limit)
    ids={t['source_task_id'] for c in cases for t in c['tasks'].values()}
    controls=[c for c in controls if c['tasks']['A']['source_task_id'] in ids]
    jobs,aliases=runner.jobs_for(cases,controls,names,experiments,a.repeats,deliveries)
    if not jobs:p.error('No selected jobs')
    planned={method:len(jobs) for method in methods}
    manifest={'protocol':'eventpilot.paper_suite.12','methods':methods,'models':names,'planned':planned,
              'logical_per_method':len(jobs)+len(aliases),'experiments':experiments,'deliveries':deliveries,
              'case_ids':a.case_ids,'case_limit':a.case_limit,'repeats':a.repeats,
              'dataset_digest':dm.get('dataset_digest'),'config_digest':digest(config),
              'code_sha256':runner.code_identity(),'launcher_sha256':file_digest(Path(__file__)),
              'budgets':{k:getattr(a,k) for k in ('max_commands','candidate_limit','output_tokens','max_model_requests','reflexion_trials','job_order_seed')}}
    if a.dry_run:
        # Validate actual child registrations and the corpus without credentials
        # or creating experiment files.
        print(json.dumps(manifest,indent=2),flush=True)
        for method in methods:
            result=subprocess.run(child_command(a,method),check=False)
            if result.returncode:raise SystemExit(result.returncode)
        return
    with runner.run_lock(a.run_root):
        saved=read_json(a.run_root/'suite_manifest.json')
        if saved:
            if not a.resume:p.error('Existing suite: use --resume')
            if saved!=manifest:p.error('Resume identity mismatch; only worker concurrency may change')
        else:
            if any((a.run_root/m/'run_manifest.json').exists() for m in methods):p.error('Existing method runs without suite registration')
            write_json(a.run_root/'suite_manifest.json',manifest)
        for m in selected:
            key=m['api_key_env']
            if not os.environ.get(key):
                if not sys.stdin.isatty():p.error('Set '+key+' before starting in a non-interactive terminal')
                os.environ[key]=getpass.getpass(m['name']+' API Key: ')
                if not os.environ[key]:p.error('Empty API key')
        result=0
        try:
            for method in methods:
                print(f'[METHOD] {method}; results: {a.run_root/method}',flush=True)
                result=stream_child(child_command(a,method),a.run_root/'logs'/(method+'.log'))
                merge_tables(a.run_root,methods,planned)
                if result:
                    print('[SUITE PAUSED] Remaining methods retained as pending; resume this same root.',flush=True)
                    break
        except KeyboardInterrupt:
            result=130;print('[INTERRUPTED] Resume the same suite root.',flush=True)
        finally:
            summary=merge_tables(a.run_root,methods,planned)
            print(json.dumps(summary,indent=2),flush=True)
            print('Merged tables: '+str(a.run_root/'tables'),flush=True)
        if result:raise SystemExit(result)


if __name__=='__main__':main()
