#!/usr/bin/env python3
"""Offline engineering checks only. No model API and no publishable LLM scores."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

sys.pycache_prefix=os.environ.get('PYTHONPYCACHEPREFIX',str(Path(tempfile.gettempdir())/'eventpilot_v11_selfcheck_cache'))
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from eventpilot.compiler import validate
from eventpilot.corpus import Corpus
from eventpilot.io import read_jsonl,write_json
from test_reviewed_engine import run_oracle


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus-root',type=Path,default=ROOT/'corpus')
    parser.add_argument('--output',type=Path,default=ROOT/'validation/engineering_sweep.json')
    args=parser.parse_args()
    validation=validate(ROOT/'data')
    cases=list(read_jsonl(ROOT/'data/cases.jsonl'))+list(read_jsonl(ROOT/'data/baselines.jsonl'))
    corpus=Corpus(args.corpus_root)
    failures=[];count=0;started=time.monotonic()
    for case in cases:
        deliveries=('sequential','batch','sequential_no_action') if case['experiment']=='multi' else ('sequential',)
        for delivery in deliveries:
            try:
                record=run_oracle(case,corpus,delivery)
                if record['evaluation']['episode_success'] is not True:
                    failures.append({'case_id':case['case_id'],'delivery':delivery,
                                     'evaluation':record['evaluation'],'stages':record['decision_stages'],
                                     'windows':record.get('action_windows',[])})
            except Exception:
                failures.append({'case_id':case['case_id'],'delivery':delivery,'exception':traceback.format_exc()})
            count+=1
            if count%50==0:
                print(f'[ENGINEERING ONLY] {count} registered paths; failures={len(failures)}',flush=True)
    result={'fixture':'PRIVATE_ORACLE_NOT_LLM','model_API_calls':0,
            'meaning':'source/control/denominator reachability check, not measured model performance',
            'dataset_digest':validation['dataset_digest'],'executions':count,'failures':len(failures),
            'details':failures,'elapsed_seconds':round(time.monotonic()-started,3)}
    write_json(args.output,result)
    print(json.dumps({k:v for k,v in result.items() if k!='details'},indent=2),flush=True)
    if failures:raise SystemExit(1)


if __name__=='__main__':main()
