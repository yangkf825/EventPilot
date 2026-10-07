import asyncio
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_online_v2 as runner
from prepare_online_v2 import prepare_pool
from paired_online_v2 import paired_coverage
from test_online_v2_engine import case


def test_common_eligibility_deduplicates_views_and_excludes_entire_incomplete_cf_group(tmp_path,monkeypatch):
    import eventarena.browser as browser_module
    import eventarena.online_v2_engine as engine
    a=case();a['case_id']='C1';b=copy.deepcopy(a);b['case_id']='C2'
    for c in (a,b):c.update(group_id='G1',group_type='goal')
    data=tmp_path/'data';data.mkdir();(data/'single_event.jsonl').write_text(json.dumps(a)+'\n'+json.dumps(b)+'\n')
    class StubBrowser:
        def __init__(self,*args,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    async def restore(obj,browser,checkpoint):
        assert obj.model.__class__.__name__=='NeverCalled'
        return {'matches':obj.case['case_id']=='C1'},{}
    calls=[]
    async def obtain(c,r,pool,root,model,opts,locks):
        calls.append(c['case_id'])
        return {'status':'checkpoint_ready'},{'state':{}},c['case_id']
    monkeypatch.setattr(browser_module,'Browser',StubBrowser)
    monkeypatch.setattr(engine,'restore_checkpoint',restore)
    args=SimpleNamespace(data_dir=data,repeats=1,headless=True,max_steps=5,pre_steps=5,setup_steps=5,
                         max_context_chars=450000,loop_limit=4,format_retries=1,judge_model=None,prep_model='ds',retry_infra=False)
    selected={'counterfactual':[a,b],'ablation':[a]}
    gates=asyncio.run(prepare_pool(args,tmp_path/'run',selected,{'ds':object()},{},obtain))
    assert sorted(calls)==['C1','C2']
    assert len(gates)==2 and not any(g['eligible'] for g in gates.values())
    assert all(g['reason']=='controlled_group_preparation_incomplete' for g in gates.values())
    assert (tmp_path/'run/preparation_eligibility/manifest.json').exists()


def test_pairing_compares_same_original_task_and_never_different_revision_targets(tmp_path):
    a=case('DEFER');a.update(case_id='C1',task_family_id='F1')
    revision=case('REPLAN');revision.update(case_id='C2',task_family_id='F1')
    for c in (a,revision):
        for experiment,suffix in (('main',''),('baseline','_AONLY')):
            job={'experiment':experiment,'model':'ds','view':'trajectory','case_id':c['case_id']+suffix,'repeat':0}
            runner.write_json(runner.job_path(tmp_path,job)/'metric_record.json',{
                'evaluation':{'environment_valid':True,'task_success':True,
                              'episode_success':experiment=='baseline'}})
    rows=paired_coverage(tmp_path,{'main':[a,revision],'baseline':[a,revision]},['ds'],1)
    row=rows[0]
    assert row['registered_pairs']==1 and row['shared_known_pairs']==1
    assert row['success_delta_left_minus_right']==0


def test_metric_record_preserves_visible_plan_stages():
    stages=[{'known_event_ids':['E1','E2'],'new_event_ids':['E1','E2'],'api_ok':True,'parsed':{'schedule':['E1','CURRENT_TASK']}}]
    assert runner.metric_record({'case_id':'M1','decision_stages':stages})['decision_stages']==stages


def test_prepare_command_cannot_accidentally_run_actor_on_demand(tmp_path):
    import pytest
    with pytest.raises(ValueError,match='never sample tested actors'):
        runner.main(['prepare','--preparation-mode','on-demand','--run-root',str(tmp_path/'run')])
