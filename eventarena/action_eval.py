"""Offline next-action evaluation on recorded (teacher-forced) snapshots."""
import json
from datetime import datetime
from pathlib import Path

from .corpus import ROOT, jsonl, snapshot_actions
from .providers import Model, object_output


def run(args):
    model=None if args.dry_run else Model(args.only)
    run_dir=ROOT/'runs/action_eval'/f'{args.only}_{datetime.now():%Y%m%d_%H%M%S}'
    records=[]
    for task in jsonl(ROOT/'data/normalized/tasks.jsonl'):
        if args.split and task['split']!=args.split:continue
        for index,step in enumerate(task['steps']):
            candidates=snapshot_actions(task,index)
            prompt={'task':task['goal'],'previous_recorded_actions':[s['action'] for s in task['steps'][:index]],
                    'page_candidates':candidates}
            system=('Predict the next action on this recorded webpage. Return one JSON object: '
                    '{"node_id":"the observed backend node ID", "op":"CLICK|TYPE|SELECT", "value":""}. '
                    'Select only a listed node_id. This is offline action prediction, not live browser execution.')
            if args.dry_run:
                print(json.dumps({'task_id':task['task_id'],'checkpoint':index,
                                  'prompt':prompt,'system':system,'no_api_call':True},ensure_ascii=False,indent=2))
                return
            raw=model.call([{'role':'system','content':system},{'role':'user','content':json.dumps(prompt,ensure_ascii=False)}])
            try:
                pred=object_output(raw)
            except (ValueError,TypeError):pred={}
            gold=step['operation']
            element_ok=str(pred.get('node_id')) in step['positive_node_ids']
            operation_ok=pred.get('op','').upper()==gold['op']
            value_ok=(str(pred.get('value','')).strip()==str(gold.get('value','')).strip()) if gold['op'] in ('TYPE','SELECT') else True
            records.append({'task_id':task['task_id'],'action_uid':step['action_uid'],'prediction':pred,
                            'raw':raw,'element_correct':element_ok,'operation_correct':operation_ok and value_ok,
                            'step_correct':element_ok and operation_ok and value_ok})
            run_dir.mkdir(parents=True,exist_ok=True)
            with (run_dir/'predictions.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(records[-1],ensure_ascii=False)+'\n')
            print(f'[{len(records)}/{args.limit}] {task["task_id"]} step {index}: {records[-1]["step_correct"]}',flush=True)
            if len(records)>=args.limit:break
        if len(records)>=args.limit:break
    if not records:raise ValueError('No matching snapshot actions')
    results={'mode':'offline_teacher_forced','n_steps':len(records),'task_success':None,
             'ElementAcc':sum(r['element_correct'] for r in records)/len(records),
             'OperationExactAcc':sum(r['operation_correct'] for r in records)/len(records),
             'StepExactAcc':sum(r['step_correct'] for r in records)/len(records),
             'note':'Exact-match diagnostic; not the original paper operation-token F1 or live task success.',
             'provider_usage':model.usage}
    (run_dir/'metrics.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(json.dumps(results,ensure_ascii=False,indent=2))
