#!/usr/bin/env python3
"""Export actual v6 scores and uncertainty; missing results stay unavailable."""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from run_online_v2 import read_json,read_cases,write_csv,write_json,job_path,job_views,audit_counterfactual_groups
from eventarena.online_v2_metrics import score_cases,cluster_bootstrap_ci,_records,_decision_records,_actual_success,_classification

def export(root):
 root=Path(root);manifest=read_json(root/'manifest.json')
 if not manifest:raise ValueError('No registered run; no experimental scores can be exported')
 dest=root/'paper_tables';dest.mkdir(exist_ok=True)
 tables={};intervals=[];strata=[]
 for experiment in manifest['counts']:
  source=root/experiment/'comparison.csv'
  if source.exists():
   with source.open() as stream:rows=list(csv.DictReader(stream))
   tables[experiment]=rows;write_csv(dest/(experiment+'.csv'),rows)
  if experiment=='counterfactual':
   for kind in ('goal','state','semantic'):
    source=root/experiment/(kind+'_comparison.csv')
    if source.exists():
     with source.open() as stream:rows=list(csv.DictReader(stream))
     tables['counterfactual_'+kind]=rows;write_csv(dest/('counterfactual_'+kind+'.csv'),rows)
  cases=read_cases(root/'registered_data'/(experiment+'.jsonl'))
  for model in manifest['models']:
   for view in job_views(experiment):
    episodes=[]
    for case in cases:
     for repeat in range(manifest['arguments']['repeats']):
      job={'model':model,'experiment':experiment,'view':view,'repeat':repeat,'case_id':case['case_id']}
      episode=read_json(job_path(root,job)/'metric_record.json')
      if episode:episodes.append(episode)
    if experiment=='counterfactual':audit_counterfactual_groups(cases,episodes)
    records,_=_records(cases,episodes,experiment,view)
    if experiment=='counterfactual':
     for row in records:
      if not row['episode'] or row['episode'].get('counterfactual_protocol_valid') is not True:row['episode']=None
    decision_rows=_decision_records(records)
    for row in records+decision_rows:
     row['cluster']=urlsplit(row['case']['url']).hostname.removeprefix('www.')
    for key,values in [('Acc',[{**r,'value':r['decision_correct']} for r in decision_rows]),
                       ('MicroESR',[{**r,'value':_actual_success(r['case'],r['episode'])} for r in records])]:
     ci=cluster_bootstrap_ci(values,cluster_key='cluster',n_resamples=2000,seed=20261012)
     intervals.append({'experiment':experiment,'model':model,'view':view,'metric':key,
        **ci,'cluster_scope':'source_A_website','scope':'all_registered; unknowns suppress interval'})
    if decision_rows:
     def f1(sample):return _classification(sample)['macro_f1']
     ci=cluster_bootstrap_ci(decision_rows,cluster_key='cluster',n_resamples=2000,seed=20261012,statistic=f1)
     intervals.append({'experiment':experiment,'model':model,'view':view,'metric':'MacroF1',**ci,
        'cluster_scope':'source_A_website','scope':'all_registered; unknowns suppress interval'})
    for field in ('split','difficulty','multi_arrival_mode'):
     labels=sorted({c[field] for c in cases if field in c})
     for label in labels:
      subset=[c for c in cases if c.get(field)==label];ids={c['case_id'] for c in subset}
      metrics=score_cases(subset,[e for e in episodes if e['case_id'] in ids],experiment,view)
      strata.append({'experiment':experiment,'model':model,'view':view,'stratum':field,'value':label,
       'cases':len(subset),**{k:metrics.get(k) for k in ('Acc','MacroF1','FIR','MIR','FollowupAcc','FollowupF1','MicroESR','TaskCompletion','PTAR','Coverage','EventF1','ScheduleExactMatch','multi_ESR')}})
 write_csv(dest/'confidence_intervals.csv',intervals);write_csv(dest/'stratified_results.csv',strata)
 write_json(dest/'tables.json',tables)
 output=['# EventPilot v6：实际运行结果表','',
  '空值或 NA 表示不可判定，不能当成 0。比例以百分比报告；N、known、unknown 等计数保持整数。',
  '主实验与 Full Trajectory 消融共享同一 episode。跨表不可重复累加为独立样本。',
  'Follow-up 仅评价 Gold 与预测均为 INTERRUPT 的案例，同时报告其分母和 JointFUAcc。','']
 for name,rows in tables.items():
  output+=['## '+name,'']
  if not rows:output+=['尚无结果。',''];continue
  keys=list(rows[0]);output+=['| '+' | '.join(keys)+' |','| '+' | '.join(['---']*len(keys))+' |']
  for row in rows:
   values=[]
   for key in keys:
    v=row.get(key,'')
    if v=='' or v is None:values.append('NA');continue
    if key not in {'cases','repeats','case_N','group_N','group_known','Gold_interrupt_N','Followup_eligible_N'} and not any(key.endswith('_'+x) for x in ('n','N','known','unknown')):
     try:v=f'{float(v)*100:.2f}'
     except ValueError:pass
    values.append(str(v).replace('|','\\|'))
   output+=['| '+' | '.join(values)+' |']
  output+=['']
 (dest/'TABLES.md').write_text('\n'.join(output)+'\n')
 write_json(dest/'RESULT_STATUS.json',{'actual_results_only':True,'protocol':manifest['protocol_version'],
   'gold_human_reviewed':manifest['gold_human_reviewed'],
   'judge_human_reviewed':False,'ci_resamples':2000,'publication_ready':False,
   'remaining_publication_requirements':['human label audit','stratified independent Judge audit','check preparation/outcome coverage and controlled-group validity'],
   'no_guaranteed_publication_or_target_accuracy':True})
 print('Actual result tables exported:',dest)
 return dest

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-root',type=Path,required=True);export(p.parse_args().run_root)
