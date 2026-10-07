#!/usr/bin/env python3
"""Apply explicit scoring corrections to old evidence without overwriting a run."""
import argparse
import hashlib
from pathlib import Path
from types import SimpleNamespace
from run_online_v2 import read_json,read_cases,summarize,write_json,code_fingerprint


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-root',type=Path,required=True)
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    source=a.run_root.resolve()
    manifest=read_json(source/'manifest.json')
    if not manifest:raise ValueError('Registered source run required')
    target=(a.output or source/'analysis_recomputed_v204').resolve()
    if target==source or target.exists():raise ValueError('Choose a new output directory; original results are immutable')
    target.mkdir(parents=True)
    cases={e:read_cases(source/'registered_data'/f'{e}.jsonl') for e in manifest['counts']}
    summarize(target,cases,manifest['models'],SimpleNamespace(**manifest['arguments']),source_root=source)
    write_json(target/'SCORING_CORRECTION.json',{
        'source_run':str(source),'source_manifest_sha256':hashlib.sha256((source/'manifest.json').read_bytes()).hexdigest(),
        'source_code_sha256':manifest['code_sha256'],'corrected_code_sha256':code_fingerprint(),
        'corrections':['retain registered CF experiment records and all repeats',
                       'plan scores use each actually visible event queue; whole execution uses complete episode'],
        'original_prompts_responses_and_labels_changed':False,'api_calls':0,
        'not_a_new_model_run':True})
    print('Corrected report saved separately:',target)


if __name__=='__main__':main()
