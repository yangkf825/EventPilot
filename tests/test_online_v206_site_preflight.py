"""Registered-entry preflight tests never contact a website or model API."""
import asyncio
import csv
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import preflight_online_v2 as preflight
from run_online_v2 import select_cases


def task(cid, url='https://a.example/primary', *, group=None, update=False):
    case = {'case_id': cid, 'url': url, 'site': 'example', 'goal': 'Public lookup',
            'task_family_id': 'family-a', 'split': 'development',
            'verification': {'CURRENT_TASK': {'method': 'llm_judge'}},
            'setup_tasks': [{'id': 'PRIOR_B', 'url': 'https://a.example/setup'}],
            'events': [{'id': 'E1', 'execution': {'kind': 'research_task',
                       'url': 'https://b.example/event', 'goal': 'Event lookup'}}],
            'gold': {'decision': 'INTERRUPT', 'follow_up': 'HANDLE'}}
    if group:
        case.update(group_type=group[0], group_id=group[1])
    if update:
        case['events'].append({'id': 'E2', 'execution': {'kind': 'update_goal',
            'updated_goal': 'Revised public lookup', 'updated_verification': {'method': 'llm_judge'}}})
    return case


@pytest.fixture
def data(tmp_path):
    folder = tmp_path / 'dataset'
    folder.mkdir()
    sets = {
        'single_event.jsonl': [task('S1', update=True), task('S2', 'https://c.example/')],
        'counterfactual.jsonl': [task('G1A', group=('goal', 'G1')), task('G1B', group=('goal', 'G1')),
                               task('T1A', group=('state', 'T1')), task('T1B', group=('state', 'T1'))],
        'multi_event.jsonl': [task('M1', 'https://m.example/')],
    }
    for name, cases in sets.items():
        (folder / name).write_text(''.join(json.dumps(c) + '\n' for c in cases))
    return folder


def args(data, tmp_path, *extra):
    parsed = preflight.parser().parse_args([
        '--data-dir', str(data), '--output', str(tmp_path / 'site.csv'), *extra])
    parsed.experiments = list(preflight.EXPERIMENTS) if parsed.experiments == 'all' else parsed.experiments.split(',')
    return parsed


def observed(url, text='Public page with documented useful information.', title='Public entry'):
    return {'url': url, 'text': text, 'title': title, 'candidates': [{'node_id': '1'}]}


@pytest.mark.parametrize('status, observation, expected', [
    (200, observed('https://a.example/'), 'entry_available'),
    (403, observed('https://a.example/'), 'http_forbidden'),
    (500, observed('https://a.example/'), 'http_error'),
    (404, observed('https://a.example/'), 'http_error'),
    (None, observed('https://a.example/'), 'navigation_no_response'),
    (True, observed('https://a.example/'), 'navigation_no_response'),
    (200, observed('https://a.example/', text='   \n'), 'blank_page'),
    (200, observed('about:blank'), 'blank_page'),
    (200, observed('https://a.example/', title='Just a moment'), 'blocked_challenge'),
    (200, observed('https://a.example/', text='Verify that you are human'), 'blocked_challenge'),
    (200, observed('https://a.example/', title='CAPTCHA challenge'), 'blocked_challenge'),
    (200, observed('https://a.example/', text='Access denied'), 'blocked_challenge'),
    (200, {'url': 'https://a.example/', 'text': None}, 'observation_error'),
    (200, None, 'observation_error'),
])
def test_availability_requires_actual_nonblank_unblocked_capture(status, observation, expected):
    assert preflight.classify_observation(status, observation) == expected


@pytest.mark.parametrize('url', ['', None, 'about:blank', 'file:///etc/hosts',
                                  'https://user:secret@host.example/', 'https://[invalid'])
def test_invalid_or_credential_bearing_urls_are_not_navigated(url):
    assert preflight.valid_url(url) is False


def test_selected_primary_setup_event_urls_are_deduplicated_and_keep_roles(data, tmp_path):
    plan = preflight.selected_plan(args(data, tmp_path))
    targets = {t['entry_url']: t for t in plan['targets']}
    assert set(targets) == {'https://a.example/primary', 'https://a.example/setup',
                            'https://b.example/event', 'https://c.example/', 'https://m.example/'}
    assert {r['role'] for r in targets['https://a.example/setup']['references']} == {'setup'}
    assert {r['role'] for r in targets['https://b.example/event']['references']} == {'event'}
    assert len(plan['targets']) == 5
    assert 'gold' not in json.dumps(plan)
    assert plan['counts'] == {'main': 2, 'ablation': 2, 'counterfactual': 4,
                             'multi': 1, 'baseline': 2, 'final_intent': 1}


def test_pilot_id_selection_uses_same_full_cf_groups_as_runner(data, tmp_path):
    selection = args(data, tmp_path, '--case-ids', 'S1', '--cf-group-ids', 'T1', '--multi-case-ids', 'M1')
    plan = preflight.selected_plan(selection)
    runner = select_cases(selection)
    assert {experiment: [r['case_id'] for r in plan['cases'] if r['experiment'] == experiment]
            for experiment in runner} == {experiment: [c['case_id'] for c in cases]
                                         for experiment, cases in runner.items()}
    assert plan['counts']['counterfactual'] == 2
    assert plan['counts']['final_intent'] == 1
    assert 'https://c.example/' not in {t['entry_url'] for t in plan['targets']}


def test_source_selection_respects_limits_and_split(data, tmp_path):
    selection = args(data, tmp_path, '--single-limit', '1', '--cf-groups', '1', '--multi-limit', '1',
                     '--split', 'development')
    assert preflight.selected_plan(selection)['counts']['main'] == 1
    selection.split = 'heldout_candidate'
    plan = preflight.selected_plan(selection)
    assert not plan['targets']
    assert not preflight.coverage_report(plan, [], .9)['passed']


def test_profiles_mode_keeps_legacy_profile_limit_without_dataset_reads(data, tmp_path):
    selection = args(data, tmp_path, '--mode', 'profiles', '--limit', '2')
    selection.data_dir = Path('/nonexistent/dataset')
    plan = preflight.selected_plan(selection)
    assert plan['counts'] == {'profiles': 2}
    assert len(plan['targets']) == 2


def test_failure_of_one_setup_or_event_url_blocks_its_affected_case(data, tmp_path):
    plan = preflight.selected_plan(args(data, tmp_path))
    rows = [{**target, 'category': 'http_forbidden' if target['entry_url'] == 'https://b.example/event'
             else 'entry_available'} for target in plan['targets']]
    report = preflight.coverage_report(plan, rows, .9)
    assert report['coverage'] == .8
    assert not report['passed']
    assert report['experiments']['main']['coverage'] == 0
    assert report['experiments']['final_intent']['coverage'] == 1
    assert not report['task_success_verified']


def test_case_coverage_can_block_even_when_url_coverage_passes(data, tmp_path):
    plan = preflight.selected_plan(args(data, tmp_path))
    rows = [{**target, 'category': 'entry_available'} for target in plan['targets']]
    # Additional available URLs must not conceal the one URL required by most cases.
    for i in range(15):
        plan['targets'].append({'entry_url': f'https://extra.example/{i}'})
        rows.append({'entry_url': f'https://extra.example/{i}', 'category': 'entry_available'})
    next(r for r in rows if r['entry_url'] == 'https://b.example/event')['category'] = 'http_forbidden'
    report = preflight.coverage_report(plan, rows, .9)
    assert report['coverage'] == .95
    assert not report['passed']


class FakePage:
    def __init__(self, runtime):
        self.runtime = runtime
        self.url = None

    async def goto(self, url, **kwargs):
        self.url = url
        self.runtime.navigated.append(url)
        status, _ = self.runtime.behavior.get(url, (200, observed(url)))
        if isinstance(status, Exception):
            raise status
        return None if status is None else SimpleNamespace(status=status)

    async def evaluate(self, _script):
        _, observation = self.runtime.behavior.get(self.url, (200, observed(self.url)))
        if observation == 'hang':
            await asyncio.sleep(999)
        if isinstance(observation, Exception):
            raise observation
        return observation

    async def screenshot(self, path):
        self.runtime.screenshots.append(path)

    async def close(self):
        self.runtime.closed_pages.append(self.url)


class FakeRuntime:
    def __init__(self, behavior=None, startup_error=None):
        self.behavior = behavior or {}
        self.startup_error = startup_error
        self.navigated, self.closed_pages, self.screenshots = [], [], []
        self.chromium = self
        self.browser_closed = self.runtime_stopped = False

    def __call__(self):
        return self

    async def start(self):
        return self

    async def launch(self, **_kwargs):
        if self.startup_error:
            raise self.startup_error
        return self

    async def new_page(self):
        return FakePage(self)

    async def close(self):
        self.browser_closed = True

    async def stop(self):
        self.runtime_stopped = True


def test_mock_browser_saves_csv_and_json_reports_and_actual_observations(data, tmp_path):
    selection = args(data, tmp_path)
    runtime = FakeRuntime({'https://b.example/event': (403, observed('https://b.example/event', 'Forbidden'))})
    report = asyncio.run(preflight.inspect_sites(selection, playwright_factory=runtime))
    assert not report['passed']
    assert len(runtime.navigated) == len(set(runtime.navigated)) == 5
    assert len(runtime.closed_pages) == 5
    assert runtime.browser_closed and runtime.runtime_stopped
    saved = json.loads(selection.output.with_suffix('.json').read_text())
    assert saved == report
    with selection.output.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 5
    assert any(r['category'] == 'http_forbidden' for r in rows)
    assert all(json.loads(r['references']) for r in rows)
    assert len(list((tmp_path / 'site_observations').glob('*.json'))) == 5
    assert not runtime.screenshots


def test_navigation_timeout_and_dom_hang_remain_unavailable(data, tmp_path):
    selection = args(data, tmp_path, '--timeout', '.02')
    runtime = FakeRuntime({'https://a.example/primary': (TimeoutError('provider-free timeout'), None),
                           'https://b.example/event': (200, 'hang')})
    report = asyncio.run(preflight.inspect_sites(selection, playwright_factory=runtime))
    categories = {row['entry_url']: row['category'] for row in report['rows']}
    assert categories['https://a.example/primary'] == 'timeout'
    assert categories['https://b.example/event'] == 'timeout'
    assert len(runtime.closed_pages) == 5


def test_runtime_start_failure_is_not_a_website_success(data, tmp_path):
    selection = args(data, tmp_path)
    runtime = FakeRuntime(startup_error=RuntimeError('browser not installed'))
    report = asyncio.run(preflight.inspect_sites(selection, playwright_factory=runtime))
    assert not report['passed']
    assert report['failure_categories'] == {'browser_launch_error': 5}
    assert not runtime.navigated
    assert runtime.runtime_stopped


def test_valid_all_entries_only_certify_reachability(data, tmp_path):
    runtime = FakeRuntime()
    report = asyncio.run(preflight.inspect_sites(args(data, tmp_path), playwright_factory=runtime))
    assert report['passed']
    assert report['coverage'] == 1
    assert report['task_success_verified'] is False
    assert not report['uses_tested_actor_results']
    assert 'does not prove' in report['scope']


def test_artifact_storage_failure_stops_instead_of_claiming_website_outage(data, tmp_path, monkeypatch):
    import eventarena.storage_io as storage
    def storage_failed(_path, _value):
        raise storage.StorageIOError(5, 'fixture I/O failure')
    monkeypatch.setattr(storage, 'atomic_write_json', storage_failed)
    selection = args(data, tmp_path)
    runtime = FakeRuntime()
    with pytest.raises(storage.StorageIOError):
        asyncio.run(preflight.inspect_sites(selection, playwright_factory=runtime))
    assert not selection.output.exists()
    assert runtime.browser_closed and runtime.runtime_stopped


def test_threshold_exit_three_blocks_launcher(monkeypatch, tmp_path):
    async def unavailable(_args):
        return {'passed': False, 'coverage': .5}
    monkeypatch.setattr(preflight, 'inspect_sites', unavailable)
    assert preflight.main(['--output', str(tmp_path / 'check.csv')]) == 3


def test_threshold_success_exit_zero(monkeypatch, tmp_path):
    async def available(_args):
        return {'passed': True, 'coverage': 1}
    monkeypatch.setattr(preflight, 'inspect_sites', available)
    assert preflight.main(['--output', str(tmp_path / 'check.csv')]) == 0


@pytest.mark.parametrize('options', [
    ['--workers', '0'], ['--timeout', '0'], ['--min-coverage', '1.1'],
    ['--timeout', 'inf'], ['--timeout', 'nan'], ['--output', 'report.json'],
    ['--mode', 'dataset', '--limit', '1'], ['--experiments', 'invented'],
    ['--mode', 'profiles', '--case-ids', 'S1'],
])
def test_invalid_configuration_stops_before_network(options):
    with pytest.raises(SystemExit) as error:
        preflight.main(options)
    assert error.value.code == 2


def test_unknown_registered_ids_stop_before_browser(data, tmp_path):
    runtime = FakeRuntime()
    selection = args(data, tmp_path, '--case-ids', 'missing')
    with pytest.raises(ValueError, match='Unknown case ID'):
        asyncio.run(preflight.inspect_sites(selection, playwright_factory=runtime))
    assert not runtime.navigated


def test_old_limit_only_cli_remains_a_profiles_diagnostic(monkeypatch, tmp_path):
    calls = []
    async def capture(selection):
        calls.append(selection)
        return {'passed': True, 'coverage': 1}
    monkeypatch.setattr(preflight, 'inspect_sites', capture)
    assert preflight.main(['--output', str(tmp_path / 'legacy.csv'), '--limit', '3']) == 0
    assert calls[0].mode == 'profiles'
    assert calls[0].limit == 3
