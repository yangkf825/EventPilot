"""Shared casewise qualification, before any Actor samples that case/group.

No global coverage threshold prevents qualified groups from running. Initial
eligibility is shared across models/views and never reads their scored outcomes.
"""
from __future__ import annotations
import asyncio
import copy
import time
from urllib.parse import urlsplit

from run_online_v2 import FILES,checkpoint_key,now,read_cases,read_json,sha,write_json

POLICY='casewise_common_qualification_conditional_reporting_v1'

def qualified(row):
    return isinstance(row,dict) and row.get('eligible') is True and row.get('availability_policy')==POLICY

def unit_key(case,repeat):
    if case.get('group_type')=='state':return f"state_pair_{case['group_id']}_repeat_{repeat}"
    return checkpoint_key(case,repeat)


class EligibilityProvider:
    def __init__(self,args,root,selected,models,locks,obtain_checkpoint):
        self.args,self.root,self.selected=args,root,selected
        self.models,self.locks,self.obtain_checkpoint=models,locks,obtain_checkpoint
        originals={c['case_id']:c for c in read_cases(args.data_dir/FILES['main'])}
        self.originals=originals;self.sources={};self.units={};self.unit_experiment={}
        for experiment,cases in selected.items():
            if experiment=='final_intent':continue
            for case in cases:
                source=originals[case['original_case_id']] if experiment=='baseline' else case
                for repeat in range(args.repeats):
                    key=unit_key(source,repeat);identity=(source['case_id'],repeat)
                    self.sources[identity]=source
                    self.units.setdefault(key,{})[identity]=source
                    self.unit_experiment[key]='counterfactual' if experiment=='counterfactual' else 'multi' if experiment=='multi' else 'main'
        self.semaphore=asyncio.Semaphore(args.prep_workers)
        self.unit_locks={};self.site_locks={};self.results={};self.active={}
        for key,members in self.units.items():
            cached={identity:read_json(self.canonical(*identity)) for identity in members}
            if all(r and r.get('availability_policy')==POLICY for r in cached.values()) and (
                not args.retry_infra or all(qualified(r) for r in cached.values())):
                if any(r.get('case_sha256')!=sha(members[identity]) for identity,r in cached.items()):
                    raise ValueError('Casewise qualification cache has changed cases')
                self.results[key]=cached
        self.started=time.monotonic();self.persist()

    def canonical(self,cid,repeat):
        return self.root/'preparation_eligibility'/f'repeat_{repeat}'/cid/'eligibility.json'

    def persist(self):
        rows=[r for group in self.results.values() for r in group.values()]
        state={'phase':'casewise_execution','state':'running','policy':POLICY,
               'planned':len(self.sources),'completed':len(rows),'eligible':sum(qualified(r) for r in rows),
               'units':len(self.units),'qualified_units_completed':len(self.results),
               'active_units':copy.deepcopy(list(self.active.values())),
               'prep_workers':self.args.prep_workers,'same_site_preparation_parallelism':1,
               'elapsed_s':round(time.monotonic()-self.started,1),'updated_at':now(),
               'no_global_abort_for_low_coverage':True,'selection_reads_tested_actor_results':False}
        write_json(self.root/'casewise_preparation_state.json',state)

    async def ensure(self,case,repeat):
        key=unit_key(case,repeat)
        async with self.unit_locks.setdefault(key,asyncio.Lock()):
            if key not in self.results:
                async with self.semaphore:
                    members=list(self.units[key].items())
                    hosts=sorted({urlsplit(url).hostname for _,c in members
                                  for url in [c['url']]+[s['url'] for s in c.get('setup_tasks',[])]})
                    acquired=[]
                    try:
                        for host in hosts:
                            lock=self.site_locks.setdefault(host,asyncio.Lock());await lock.acquire();acquired.append(lock)
                        self.active[key]={'unit':key,'case_ids':[cid for (cid,_),_ in members],'started_at':now()};self.persist()
                        cached={identity:read_json(self.canonical(*identity)) for identity,_ in members}
                        for identity,c in members:
                            row=cached[identity]
                            if row and (row.get('case_id')!=identity[0] or row.get('repeat')!=identity[1] or row.get('case_sha256') not in (None,sha(c))):
                                raise ValueError('Qualification cache differs from the frozen case')
                        complete=all(cached.values())
                        verified=complete and all(r.get('eligible') is True for r in cached.values())
                        if complete and (not self.args.retry_infra or verified):
                            gates=cached
                        else:
                            gates=await self.prepare(key,members,cached)
                        # Shared controlled units must qualify together. A half
                        # pair or half batch/stream comparison cannot be selected.
                        if len(members)>1 and not all(r.get('eligible') is True for r in gates.values()):
                            for row in gates.values():
                                row.setdefault('individual_preparation_reason',row['reason'])
                                row.update(eligible=False,reason='controlled_group_preparation_incomplete')
                        for identity,row in gates.items():
                            row.update(availability_policy=POLICY,qualification_unit=key,
                                       selection_reads_tested_actor_results=False)
                            write_json(self.canonical(*identity),row)
                        self.results[key]=gates
                        print(f"[QUALIFIED {len(self.results)}/{len(self.units)}] {key}: "
                              f"{sum(qualified(r) for r in gates.values())}/{len(members)} usable; available cases run immediately",flush=True)
                    finally:
                        self.active.pop(key,None)
                        for lock in reversed(acquired):lock.release()
                        self.persist()
            return copy.deepcopy(self.results[key][(case['case_id'],repeat)])

    async def prepare(self,key,members,cached):
        from .preparation import prepare_pool
        folder=self.root/'qualification_units'/key
        for identity,row in cached.items():
            if row:write_json(folder/'preparation_eligibility'/f'repeat_{identity[1]}'/identity[0]/'eligibility.json',row)
        unit_args=copy.copy(self.args)
        unit_args.repeats=1
        # The public casewise protocol currently has one registered repeat.
        cases=[c for _,c in members];experiment=self.unit_experiment[key]
        unit_selected={experiment:cases}

        async def central_checkpoint(case,repeat,pool,ignored_root,model,opts,locks):
            source_pool=self.selected.get('counterfactual',[]) if experiment=='counterfactual' else list(self.originals.values())
            return await self.obtain_checkpoint(case,repeat,source_pool,self.root,model,opts,locks)

        return await prepare_pool(unit_args,folder,unit_selected,self.models,self.locks,central_checkpoint)

    def finish(self):
        self.persist()
        state=read_json(self.root/'casewise_preparation_state.json')
        state.update(state='finished' if len(self.results)==len(self.units) else 'partial',finished_at=now())
        write_json(self.root/'casewise_preparation_state.json',state)
