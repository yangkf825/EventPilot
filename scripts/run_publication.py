#!/usr/bin/env python3
"""Full v6 experiment registration and execution; never generate fake scores."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--only',default='gateway_ds,gateway_glm,gateway_kimi,gateway_gpt')
 p.add_argument('--config',type=Path,default=ROOT/'models.gateway.json')
 p.add_argument('--settings',type=Path,default=ROOT/'config/publication_v6.json')
 p.add_argument('--run-root',type=Path,required=True)
 p.add_argument('--prep-model');p.add_argument('--judge-model')
 p.add_argument('--preparation-from-run',type=Path)
 p.add_argument('--prep-workers',type=int)
 p.add_argument('--model-id',help='Override the exact model ID for one selected model, before creating a new run')
 p.add_argument('--resume',action='store_true');p.add_argument('--retry-infra',action='store_true')
 p.add_argument('--register-only',action='store_true',help='Freeze/check the entire job selection without calling models')
 a=p.parse_args();s=json.loads(a.settings.read_text());config=a.config.resolve()
 if a.retry_infra and not a.resume:p.error('--retry-infra requires --resume')
 cfg=json.loads(config.read_text());lookup={m['name']:m for m in cfg['models']}
 prep_source=a.prep_model or s['prep_model'];judge_source=a.judge_model or s['judge_model']
 if prep_source not in lookup or judge_source not in lookup:p.error('Unknown fixed role source model')
 for name,source in [('publication_preparer',prep_source),('publication_judge',judge_source)]:
  if name in lookup:p.error('Reserved role alias already configured')
  role=copy.deepcopy(lookup[source]);role.update(name=name,enabled=False,role_source_name=source)
  cfg['models'].append(role)
 if a.model_id:
  names=a.only.split(',')
  if len(names)!=1:p.error('--model-id requires exactly one selected model')
  matches=[m for m in cfg['models'] if m['name']==names[0]]
  if len(matches)!=1:p.error('Unknown model name')
  matches[0]['model']=a.model_id
 encoded=json.dumps(cfg,sort_keys=True,ensure_ascii=False,indent=2)+'\n'
 folder=ROOT/'.model_overrides';folder.mkdir(exist_ok=True)
 config=folder/(hashlib.sha256(encoded.encode()).hexdigest()+'.json')
 if not config.exists():config.write_text(encoded)
 argv=['run','--config',str(config),'--data-dir',str(ROOT/s['data_dir']),
       '--only',a.only,'--run-root',str(a.run_root),
       '--prep-model','publication_preparer','--judge-model','publication_judge']
 argv+=['--prep-workers',str(a.prep_workers if a.prep_workers is not None else s.get('prep_workers',2))]
 if a.preparation_from_run:argv+=['--preparation-from-run',str(a.preparation_from_run)]
 for k in ('experiments','single_limit','cf_groups','multi_limit','repeats','seed','workers',
           'max_steps','pre_steps','setup_steps','max_context_chars','loop_limit','format_retries',
           'min_preparation_coverage','preparation_mode'):
  argv+=['--'+k.replace('_','-'),str(s[k])]
 for flag,value in [('headless',s['headless']),('resume',a.resume),('retry-infra',a.retry_infra),('dry-run',a.register_only)]:
  if value:argv+=['--'+flag]
 if not a.register_only:
  os.environ['EP_RUN_ROOT']=str(a.run_root.expanduser().resolve())
 from run_official_online import main as execute
 execute(argv)
 if not a.register_only:
  from export_publication import export
  export(a.run_root.expanduser().resolve())

if __name__=='__main__':main()
