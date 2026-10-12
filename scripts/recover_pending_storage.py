#!/usr/bin/env python3
"""Restore hash-checked exact pending JSON values, without altering scores."""
import argparse,hashlib,json,os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from eventarena.storage_io import atomic_write_json
from eventarena.run_lock import exclusive_run
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-root',type=Path,required=True);p.add_argument('--journal',type=Path,required=True);a=p.parse_args()
 root=a.run_root.resolve();records=sorted(a.journal.glob('*.pending.json'));count=0
 # Validate all records before the first restore; never follow an unrelated
 # absolute path from a damaged or manually edited journal.
 parsed=[]
 for path in records:
  r=json.loads(path.read_text());target=Path(r['target']).resolve()
  if r.get('format')!='exact_pending_atomic_json_v1' or not target.is_relative_to(root):raise ValueError('Journal target outside this run')
  if hashlib.sha256(r['payload'].encode()).hexdigest()!=r['sha256']:raise ValueError('Pending value hash mismatch')
  value=json.loads(r['payload']);parsed.append((path,target,value))
 with exclusive_run(root):
  for path,target,value in parsed:
   if target.exists():
    backup=a.journal/'before_restore'/target.relative_to(root);backup.parent.mkdir(parents=True,exist_ok=True)
    if not backup.exists():backup.write_bytes(target.read_bytes())
   atomic_write_json(target,value);path.rename(path.with_suffix('.restored'));count+=1
 print(json.dumps({'restored_exact_values':count,'changed_labels_or_scores':False}))
if __name__=='__main__':main()
