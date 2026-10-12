#!/usr/bin/env python3
"""Audit full authored data and export a local review catalog; no model calls."""
from __future__ import annotations
import argparse
import collections
import csv
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from build_publication_dataset import split
from eventarena_official.registration import validate_extension
from eventarena_official.corpus import read_jsonl

def audit(directory):
 d=Path(directory);validation=validate_extension(d)
 sets={k:read_jsonl(d/f) for k,f in {'main':'single_event.jsonl','counterfactual':'counterfactual.jsonl','multi':'multi_event.jsonl'}.items()}
 source={t['task_id']:t for t in read_jsonl(d/'source_task_index.jsonl')}
 for rows in sets.values():
  for c in rows:
   a=source[c['source_task_a']['task_id']]
   if c['goal']!=a['goal'] or c['url']!=a['url']:raise ValueError('Original A changed')
   for e in c['events']:
    if e['execution']['kind']=='research_task' and split(source[e['source_task']['task_id']])!=split(a):raise ValueError('B partition leakage')
   for s in c.get('setup_tasks',[]):
    if split(source[s['source_task']['task_id']])!=split(a):raise ValueError('Setup partition leakage')
 bounds={}
 for view in ['event-only','goal']:
  buckets=collections.defaultdict(collections.Counter)
  for c in sets['main']:
   v={'event':{k:c['events'][0][k] for k in ['source','text','id']},'rules':c['rules']}
   if view=='goal':v['goal']=c['goal']
   buckets[json.dumps(v,sort_keys=True)][c['events'][0]['gold']['decision']]+=1
  bounds[view]=sum(max(v.values()) for v in buckets.values())/len(sets['main'])
 if bounds!={'event-only':0.8,'goal':0.9}:raise ValueError('Registered information-identifiability bounds differ')
 foils=[c for rows in sets.values() for c in rows if c.get('state_variant')=='related_completed']
 if not all(c['setup_tasks'][0]['url']==c['events'][0]['execution']['url'] for c in foils):raise ValueError('State foil is not same-site')
 columns=['experiment','case_id','source_A_id','site','original_task','difficulty','difficulty_mechanisms','group_type','group_id','state_variant','actual_setup_goal','event_id','event_source','event_text','source_B_id','event_task','decision','follow_up','label_reason','arrival','trigger','split']
 with (d/'CASE_CATALOG.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=columns);w.writeheader()
  for exp,rows in sets.items():
   for c in rows:
    for e in c['events']:
     w.writerow({'experiment':exp,'case_id':c['case_id'],'source_A_id':c['source_task_a']['task_id'],'site':c['site'],'original_task':c['goal'],'difficulty':c['difficulty'],'difficulty_mechanisms':';'.join(c.get('difficulty_mechanisms',[])),'group_type':c.get('group_type',''),'group_id':c.get('group_id',''),'state_variant':c.get('state_variant',''),'actual_setup_goal':' | '.join(s['goal'] for s in c.get('setup_tasks',[])),'event_id':e['id'],'event_source':e['source'],'event_text':e['text'],'source_B_id':e.get('source_task',{}).get('task_id',''),'event_task':e['execution'].get('goal',e['execution'].get('updated_goal','')),'decision':e['gold']['decision'],'follow_up':e['gold']['follow_up'],'label_reason':e['label_reason'],'arrival':c.get('multi_arrival_mode','single'),'trigger':json.dumps(e.get('trigger',c['trigger']),ensure_ascii=False),'split':c['split']})
 report={'validation':validation,'cases':{k:len(v) for k,v in sets.items()},
  'source_A_tasks':len({c['source_task_a']['task_id'] for rows in sets.values() for c in rows}),
  'goal_fidelity':'all exact','cross_partition_source_tasks':0,'verified_identifiability_bounds':bounds,
  'hard_state_same_site_foils':len(foils),'human_review':False,'model_calls':0}
 (d/'BUILD_AUDIT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(report,ensure_ascii=False,indent=2));return report

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data-dir',type=Path,default=ROOT/'data/official_online/events_v6_final');audit(p.parse_args().data_dir)
