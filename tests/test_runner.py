"""Runner tests use local stubs, not billed API requests."""
import asyncio
import copy
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('v11_run', ROOT / 'scripts/run.py')
run = importlib.util.module_from_spec(spec); spec.loader.exec_module(run)
spec = importlib.util.spec_from_file_location('v11_setup', ROOT / 'scripts/setup_data.py')
setup = importlib.util.module_from_spec(spec); spec.loader.exec_module(setup)
from eventpilot.providers import ProviderError, ProviderText, response_error
from eventpilot.io import read_json, write_json


def case(cid='C1', exp='main', group='', kind=None):
    return {'case_id': cid, 'experiment': exp, 'group_id': group, 'cf_type': kind,
            'tasks': {'A': {'source_task_id': 'source1', 'goal': 'Find weather.'}},
            'review': {'human_approved': True}}


class RegistrationTests(unittest.TestCase):
    def test_context_ablation_uses_exact_200_reviewed_cases_and_four_views(self):
        rows = list(run.read_jsonl(ROOT / 'data/cases.jsonl'))
        jobs, aliases = run.jobs_for(rows, [], ['ds'], run.CONTEXT_ABLATIONS, 1, ['sequential'])
        self.assertEqual(len(jobs), 800)
        self.assertEqual(aliases, [])
        selected = {j['case_id'] for j in jobs}
        self.assertEqual(len(selected), 200)
        self.assertEqual(len({j['key'] for j in jobs}), 800)
        for exp, count in zip(run.CONTEXT_ABLATIONS, (50, 50, 100)):
            for view in run.CONTEXT_VIEWS:
                subset = [j for j in jobs if j['experiment'] == exp and j['view'] == view]
                self.assertEqual(len(subset), count)
                self.assertEqual({j['delivery'] for j in subset}, {'sequential'})
        self.assertFalse(any(c['case_id'] in selected for c in rows if c.get('cf_type') == 'semantic' or c['experiment'] == 'main'))
        can = {c['case_id'] for c in rows if c['experiment'] == 'multi' and c['subtype'].startswith('CAN/')}
        self.assertEqual(len(can), 25)
        self.assertTrue(can <= selected)

    def test_optional_multi_delivery_does_not_duplicate_cf_delivery(self):
        rows = list(run.read_jsonl(ROOT / 'data/cases.jsonl'))
        jobs, _ = run.jobs_for(rows, [], ['ds'], run.CONTEXT_ABLATIONS, 1, run.DELIVERIES)
        self.assertEqual(len(jobs), 1600)
        self.assertEqual(sum(j['experiment'] == 'context_ablation_goal' for j in jobs), 200)
        self.assertEqual(sum(j['experiment'] == 'context_ablation_state' for j in jobs), 200)
        self.assertEqual(sum(j['experiment'] == 'context_ablation_multi' for j in jobs), 1200)

    def test_full_ablation_shares_main_and_all_models(self):
        rows = [case(), case('P1', 'counterfactual', 'P', 'goal'), case('P2', 'counterfactual', 'P', 'goal'), case('M1', 'multi')]
        jobs, aliases = run.jobs_for(rows, [], ['ds', 'glm'], run.EXPERIMENTS, 1, run.DELIVERIES)
        self.assertEqual(len(jobs), 16)  # (1 main + 2 ablation + 2 CF + 3 multi) per model
        self.assertEqual(len(aliases), 2)
        self.assertEqual({j['model'] for j in jobs if j['experiment'] == 'ablation'}, {'ds', 'glm'})
        bykey = {j['key'] for j in jobs}
        self.assertTrue(all(a['canonical_job_key'] in bykey for a in aliases))
        self.assertFalse(any(j['experiment'] == 'ablation' and j['view'] == 'trajectory' for j in jobs))

    def test_case_selection_expands_pair_and_triple(self):
        rows = [case('P1', 'counterfactual', 'P', 'goal'), case('P2', 'counterfactual', 'P', 'goal'),
                case('T1', 'counterfactual', 'T', 'semantic'), case('T2', 'counterfactual', 'T', 'semantic'), case('T3', 'counterfactual', 'T', 'semantic')]
        self.assertEqual([c['case_id'] for c in run.filter_cases(rows, 'P1')], ['P1', 'P2'])
        self.assertEqual(len(run.filter_cases(rows, 'T2')), 3)
        with self.assertRaises(ValueError):
            run.filter_cases(rows, 'missing')

    def test_run_lock_rejects_second_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            with run.run_lock(Path(tmp)):
                with self.assertRaises(RuntimeError):
                    with run.run_lock(Path(tmp)):
                        pass

    @unittest.skipIf(sys.platform=='win32','Windows symlink creation may require administrator rights')
    def test_code_identity_resolves_project_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            alias=Path(tmp)/'project_alias'
            alias.symlink_to(ROOT,target_is_directory=True)
            spec=importlib.util.spec_from_file_location('ep12_alias_runner',alias/'scripts/run.py')
            other=importlib.util.module_from_spec(spec);spec.loader.exec_module(other)
            self.assertEqual(other.code_identity(),run.code_identity())

    def test_endpoint_quota_is_distinct_from_rate_limit(self):
        quota = response_error(types.SimpleNamespace(status_code=429, json=lambda: {'error': {'message': 'insufficient quota'}}))
        rate = response_error(types.SimpleNamespace(status_code=429, json=lambda: {'error': {'message': 'requests per minute exceeded'}}))
        self.assertTrue(quota.diagnostics['quota_exhausted'])
        self.assertFalse(quota.diagnostics['retryable'])
        self.assertFalse(rate.diagnostics['quota_exhausted'])
        code=response_error(types.SimpleNamespace(status_code=429,json=lambda:{'error':{'code':'USAGE_LIMIT_EXCEEDED','message':'Limit reached'}}))
        self.assertTrue(code.diagnostics['quota_exhausted'])
        self.assertFalse(code.diagnostics['retryable'])
        auth=response_error(types.SimpleNamespace(status_code=200,json=lambda:{'error':{'code':'INVALID_API_KEY'}}))
        self.assertEqual(auth.diagnostics['category'],'authentication_error')
        self.assertTrue(rate.diagnostics['retryable'])

    def test_provider_response_diagnostics_deepcopy(self):
        raw = ProviderText('{}', {'fields': ['content']})
        other = copy.deepcopy(raw); other.diagnostics['fields'].append('new')
        self.assertEqual(raw.diagnostics['fields'], ['content'])

    def test_source_copy_hash_mismatch_keeps_existing_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'page.html'; path.write_bytes(b'previous')
            with self.assertRaises(ValueError):
                setup.atomic_copy(io.BytesIO(b'changed'), path, '0' * 64)
            self.assertEqual(path.read_bytes(), b'previous')
            self.assertEqual(list(Path(tmp).glob('*.tmp')), [])


class FakeSession:
    def __init__(self, case, corpus, model, experiment, view, repeat, **kwargs):
        self.n = 0
        self.view=view; self.resources={}; self.decision_resources={}
        self.episode = {'case_id': case['case_id'], 'calls': []}
    def next_request(self):
        if self.n == 2:
            return None
        return {'role': 'prefix' if self.n == 0 else 'actor',
                'messages': [{'role': 'system', 'content': 'Return JSON.'}, {'role': 'user', 'content': '{"public_task":"Public task only."}'}]}
    def accept(self, parsed, role):
        self.n += 1
    def close(self, status='agent_finished', diagnostics=None):
        return {**self.episode, 'status': status, 'error_diagnostics': diagnostics,
                'evaluation': {'episode_success': status == 'agent_finished'}}
    def snapshot(self):
        return dict(self.episode)


class FakeModel:
    calls = 0
    fail = False
    def __init__(self, *args):
        self.usage = []
    def call(self, messages, max_tokens):
        type(self).calls += 1
        if type(self).fail:
            raise ProviderError('quota_exhausted', 402, quota=True)
        self.usage.append({'usage': {'total_tokens': 4}})
        return ProviderText('{"cmd":"finish"}', {'finish_reason': 'stop'})


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_method_keeps_original_requests(self):
        args = types.SimpleNamespace(api_concurrency=1, workers=1, resume=False, retry_infra=False,
                                     max_commands=10, candidate_limit=10, output_tokens=128, method='direct')
        rows = [case('DIRECT')]
        jobs, _ = run.jobs_for(rows, [], ['fake'], ['main'], 1, ['sequential'])
        module = types.ModuleType('eventpilot.engine'); module.Session = FakeSession
        FakeModel.fail = False; FakeModel.calls = 0
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'eventpilot.engine': module}), patch.object(run, 'Model', FakeModel):
            root = Path(tmp)
            await run.execute(args, None, rows, [], jobs, root, None)
            rec = read_json(root / 'records' / (jobs[0]['key'] + '.json'))
            self.assertEqual(rec['method'], 'direct')
            self.assertEqual(FakeModel.calls, 2)
            self.assertEqual(rec['calls'][0]['messages'], FakeSession(rows[0], None, '', '', '', 0).next_request()['messages'])
            self.assertNotIn('lgr_graph', json.dumps(rec))

    async def test_auxiliary_passes_are_billed_and_logged(self):
        args=types.SimpleNamespace(api_concurrency=1,workers=1,resume=False,retry_infra=False,
                                  max_commands=10,candidate_limit=10,output_tokens=128,method='cot')
        rows=[case('COT')];jobs,_=run.jobs_for(rows,[],['fake'],['main'],1,['sequential'])
        module=types.ModuleType('eventpilot.engine');module.Session=FakeSession
        FakeModel.fail=False;FakeModel.calls=0
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules,{'eventpilot.engine':module}), patch.object(run,'Model',FakeModel):
            root=Path(tmp);await run.execute(args,None,rows,[],jobs,root,None)
            rec=read_json(root/'records'/(jobs[0]['key']+'.json'))
            self.assertEqual(FakeModel.calls,4)
            self.assertEqual([c['method_stage'] for c in rec['calls']],['cot_reason','cot_final']*2)
            self.assertEqual(rec['efficiency']['api_calls'],4)
            self.assertEqual(len(rec['usage']),4)

    async def test_direct_request_cap_is_known_model_failure(self):
        args=types.SimpleNamespace(api_concurrency=1,workers=1,resume=False,retry_infra=False,
                                  max_commands=10,candidate_limit=10,output_tokens=128,method='direct',max_model_requests=1)
        rows=[case('CAP')];jobs,_=run.jobs_for(rows,[],['fake'],['main'],1,['sequential'])
        module=types.ModuleType('eventpilot.engine');module.Session=FakeSession
        FakeModel.fail=False;FakeModel.calls=0
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules,{'eventpilot.engine':module}), patch.object(run,'Model',FakeModel):
            root=Path(tmp);stopped=await run.execute(args,None,rows,[],jobs,root,None)
            rec=read_json(root/'records'/(jobs[0]['key']+'.json'))
            self.assertFalse(stopped)
            self.assertEqual(rec['status'],'method_budget_exhausted')
            self.assertEqual(FakeModel.calls,1)

    async def test_prefix_cache_and_resume_keep_closed_model_failure(self):
        args = types.SimpleNamespace(api_concurrency=1, workers=1, resume=False, retry_infra=False,
                                     max_commands=10, candidate_limit=10, output_tokens=128)
        rows = [case('C1'), case('C2')]
        jobs, _ = run.jobs_for(rows, [], ['fake'], ['main'], 1, ['sequential'])
        module = types.ModuleType('eventpilot.engine'); module.Session = FakeSession
        FakeModel.fail = False; FakeModel.calls = 0
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'eventpilot.engine': module}), patch.object(run, 'Model', FakeModel):
            root = Path(tmp)
            stopped = await run.execute(args, None, rows, [], jobs, root, None)
            self.assertFalse(stopped)
            self.assertEqual(FakeModel.calls, 3)  # shared prefix + one actor per case
            for j in jobs:
                p = root / 'records' / (j['key'] + '.json')
                rec = read_json(p); rec['status'] = 'invalid_model_output'; write_json(p, rec)
            args.resume = True; args.retry_infra = True
            await run.execute(args, None, rows, [], jobs, root, None)
            self.assertEqual(FakeModel.calls, 3)  # never retry model errors selectively

    async def test_quota_does_not_drain_unstarted_jobs(self):
        args = types.SimpleNamespace(api_concurrency=1, workers=1, resume=False, retry_infra=False,
                                     max_commands=10, candidate_limit=10, output_tokens=128)
        rows = [case('C1'), case('C2'), case('C3')]
        jobs, _ = run.jobs_for(rows, [], ['fake'], ['main'], 1, ['sequential'])
        module = types.ModuleType('eventpilot.engine'); module.Session = FakeSession
        FakeModel.fail = True; FakeModel.calls = 0
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'eventpilot.engine': module}), patch.object(run, 'Model', FakeModel):
            root = Path(tmp)
            stopped = await run.execute(args, None, rows, [], jobs, root, None)
            self.assertTrue(stopped)
            self.assertEqual(len(list((root / 'records').glob('*.json'))), 1)
            self.assertEqual(read_json(root / 'progress.json')['queued'], 2)
            self.assertEqual(read_json(root / 'records' / (jobs[0]['key'] + '.json'))['status'], 'provider_error')
        FakeModel.fail = False


if __name__ == '__main__':
    unittest.main()
