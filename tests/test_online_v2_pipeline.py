import csv
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runner_v2_pipeline',ROOT/'scripts/run_online_v2.py')
runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)


def c(cid='C'):
    return {'case_id':cid,'task_family_id':'F1','events':[{'id':'E1'}],
            'gold':{'decision':'IGNORE','follow_up':None}}


def e(case, repeat=0):
    return {'case_id':case['case_id'],'repeat':repeat,'status':'agent_finished',
            'protocol':'continuous_v2','api_ok':True,'exposed':True,
            'decisions':{'E1':'IGNORE'},'follow_ups':{'E1':None},'events':{'E1':{'delivered':True}},
            'evaluation':{'episode_success':True,'branch_success':True,'task_success':True,
                          'environment_valid':True,'exposed':True,'event_results':{'E1':{'distraction':False}}}}


def test_incremental_tables_preserve_registered_repeat_denominator(tmp_path):
    case = c(); jobs = runner.make_jobs({'main':[case]},['ds'],3,1)
    runner.write_json(tmp_path/'manifest.json',{'jobs':jobs})
    job = next(j for j in jobs if j['repeat'] == 0)
    runner.write_json(runner.job_path(tmp_path,job)/'metric_record.json',e(case))
    runner.summarize(tmp_path,{'main':[case]},['ds'],SimpleNamespace(repeats=3))
    metric = runner.read_json(tmp_path/'main/ds/metrics.json')['trajectory']
    assert metric['registered_episodes'] == 3 and metric['recorded_episodes'] == 1
    assert metric['Acc'] is None and metric['MicroESR'] is None
    rows = list(csv.DictReader((tmp_path/'main/comparison.csv').open(encoding='utf-8-sig')))
    assert rows[0]['Acc_N'] == '3' and rows[0]['Acc_unknown'] == '2'
    ledger = list(csv.DictReader((tmp_path/'failure_ledger.csv').open(encoding='utf-8-sig')))
    assert [r['failure_category'] for r in ledger] == ['missing_episode','missing_episode']


def test_three_counterfactual_tables_and_input_audit_are_exported(tmp_path):
    cases, episodes = [], []
    for kind, count in [('goal',2),('state',2),('semantic',3)]:
        for index in range(count):
            case = c(f'{kind}_{index}'); case.update(group_id=kind,group_type=kind)
            cases.append(case)
            episode = e(case); episode.update(protocol='frozen_checkpoint_v2',view='state',evaluation={},
                counterfactual_input_audit={'invariant_sha256':'fixed_'+kind,
                    'intervention_sha256':str(index),'intervention_visible':True})
            episodes.append(episode)
    jobs = runner.make_jobs({'counterfactual':cases},['ds'],1,1)
    runner.write_json(tmp_path/'manifest.json',{'jobs':jobs})
    for job in jobs:
        episode = next(v for v in episodes if v['case_id'] == job['case_id'])
        runner.write_json(runner.job_path(tmp_path,job)/'metric_record.json',episode)
    runner.summarize(tmp_path,{'counterfactual':cases},['ds'],SimpleNamespace(repeats=1))
    for kind in ('goal','state','semantic'):
        rows = list(csv.DictReader((tmp_path/f'counterfactual/{kind}_comparison.csv').open(encoding='utf-8-sig')))
        assert len(rows) == 1 and rows[0]['group_accuracy'] == '1.0'
        assert rows[0]['group_N'] == '1'
    assert all(r['valid'] for r in runner.read_json(tmp_path/'counterfactual/ds/input_invariance_audit.json'))
