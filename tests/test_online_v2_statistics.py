import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('analyze_online_v2', ROOT / 'scripts/analyze_online_v2.py')
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def test_family_bootstrap_keeps_pair_members_and_repeats_together():
    cases = [{'case_id':x,'task_family_id':'F1','group_id':'G','cf_group_id':'G'} for x in ('A','B')]
    episodes = [{'case_id':c['case_id'],'repeat':r} for c in cases for r in range(3)]
    sampled_cases, sampled_episodes = analysis.replicate(cases, episodes, ['F1','F1'])
    assert len(sampled_cases) == 4 and len(sampled_episodes) == 12
    assert len({c['case_id'] for c in sampled_cases}) == 4
    assert {c['group_id'] for c in sampled_cases} == {'bootstrap_0_G','bootstrap_1_G'}
    assert {e['repeat'] for e in sampled_episodes} == {0,1,2}


def test_unknown_outcomes_are_not_given_confidence_bounds():
    result = analysis.interval(.5, [None,.5], 20, .95)
    assert result['lower'] is None and result['upper'] is None
    assert analysis.interval(.5, [.2,.5,.8], 1, .95)['status'] == 'insufficient_independent_families'
