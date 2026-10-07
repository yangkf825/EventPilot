#!/usr/bin/env python3
"""Run continuous episodes and fixed-checkpoint diagnostics without label-driven routing."""
from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import random
import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
EXPERIMENTS = ('main', 'ablation', 'counterfactual', 'multi', 'baseline', 'final_intent')
VIEWS = ('event-only', 'goal', 'state', 'trajectory')
FILES = {'main': 'single_event.jsonl', 'ablation': 'single_event.jsonl',
         'counterfactual': 'counterfactual.jsonl', 'multi': 'multi_event.jsonl',
         'baseline': 'single_event.jsonl', 'final_intent': 'single_event.jsonl'}


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def read_cases(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def public_config(value):
    """Never put credential values into a run manifest, even in custom configs."""
    if isinstance(value, dict):
        return {k: ('[redacted]' if k.casefold() in {'api_key', 'key', 'token', 'password', 'authorization'}
                    else public_config(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [public_config(v) for v in value]
    return value


def baseline_case(case, final_intent=False):
    result = copy.deepcopy(case)
    if final_intent:
        updates = [e for e in case.get('events', []) if e.get('execution', {}).get('kind') == 'update_goal']
        if not updates:
            raise ValueError('Final-intent baseline requires an authorized goal revision')
        event = updates[-1]
        result['goal'] = event['execution']['updated_goal']
        verification = event['execution'].get('updated_verification', event['execution'].get('verification'))
        if verification:
            result['verification'] = {'CURRENT_TASK': copy.deepcopy(verification)}
        result['setup_tasks'] = []
    else:
        verification = copy.deepcopy(case.get('baseline_verification', case.get('verification', {})))
        result['verification'] = ({'CURRENT_TASK': verification}
                                  if 'method' in verification else verification)
    for spec in result.get('verification', {}).values():
        if isinstance(spec, dict):
            spec['checks'] = [c for c in spec.get('checks', []) if c.get('type') != 'required_event_completed']
    result['events'] = []
    result.pop('event', None)
    result['gold'] = {}
    result['original_case_id'] = case['case_id']
    result['case_id'] = case['case_id'] + ('_FINAL' if final_intent else '_AONLY')
    result['baseline_type'] = 'final_intent_upfront' if final_intent else 'no_event'
    return result


def select_cases(args):
    selected = {}
    for experiment in args.experiments:
        cases = read_cases(args.data_dir / FILES[experiment])
        if getattr(args, 'split', 'all') != 'all':
            cases = [c for c in cases if c.get('split') == args.split]
        if experiment == 'counterfactual':
            groups = {}
            for case in cases:
                groups.setdefault(case['group_type'], [])
                if case['group_id'] not in groups[case['group_type']]:
                    groups[case['group_type']].append(case['group_id'])
            if getattr(args, 'cf_group_ids', None):
                wanted = set(args.cf_group_ids.split(','))
                if not wanted.issubset({c['group_id'] for c in cases}):
                    raise ValueError('Unknown counterfactual group ID')
            else:
                wanted = {gid for ids in groups.values() for gid in ids[:args.cf_groups]}
            cases = [c for c in cases if c['group_id'] in wanted]
        else:
            wanted_ids = args.multi_case_ids if experiment == 'multi' else args.case_ids
            if wanted_ids:
                wanted = set(wanted_ids.split(','))
                if not wanted.issubset({c['case_id'] for c in cases}):
                    raise ValueError('Unknown case ID for ' + experiment)
                cases = [c for c in cases if c['case_id'] in wanted]
            else:
                cases = cases[:args.multi_limit if experiment == 'multi' else args.single_limit]
        if experiment == 'baseline':
            cases = [baseline_case(c) for c in cases]
        elif experiment == 'final_intent':
            cases = [baseline_case(c, True) for c in cases
                     if any(e.get('execution', {}).get('kind') == 'update_goal' for e in c.get('events', []))]
        selected[experiment] = cases
    return selected


def job_views(experiment):
    if experiment == 'ablation':
        return VIEWS
    # All three Counterfactual interventions use the same visible information.
    # State history is the controlled workspace state, not a second trajectory.
    return ('state',) if experiment == 'counterfactual' else ('trajectory',)


def make_jobs(selected, models, repeats, seed):
    jobs = []
    for experiment, cases in selected.items():
        for repeat in range(repeats):
            for case in cases:
                for view in job_views(experiment):
                    for model in models:
                        job = {'model': model, 'experiment': experiment, 'repeat': repeat,
                               'view': view, 'case_id': case['case_id']}
                        job['id'] = '/'.join(str(job[k]) for k in ('experiment', 'model', 'view', 'repeat', 'case_id'))
                        jobs.append(job)
    random.Random(seed).shuffle(jobs)
    return jobs


def job_path(root, job):
    return root / job['experiment'] / job['model'] / 'episodes' / job['view'] / f"repeat_{job['repeat']}" / job['case_id']


def code_fingerprint():
    paths = [Path(__file__), ROOT / 'scripts/validate_online_v2.py', ROOT/'scripts/prepare_online_v2.py',
             ROOT/'scripts/analyze_online_v2.py',ROOT/'scripts/paired_online_v2.py'] + sorted((ROOT / 'eventarena').glob('*.py'))
    return sha({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()})


def make_manifest(args, selected, models, configured, jobs):
    return {'schema_version': 'eventarena_online_run_v2', 'models': models,
            'runtime': {'python': platform.python_version(),
                        'packages': {name: importlib.metadata.version(name) for name in ('playwright','httpx')}},
            'model_configs': public_config({m: configured[m] for m in set(models + [args.prep_model, args.judge_model]) if m}),
            'dataset_sha256': sha(selected), 'code_sha256': code_fingerprint(),
            'arguments': {k: getattr(args, k) for k in ('experiments', 'repeats', 'workers', 'seed', 'single_limit', 'multi_limit',
                           'cf_groups', 'cf_group_ids', 'case_ids', 'multi_case_ids', 'split', 'prep_model', 'judge_model', 'max_steps',
                           'pre_steps', 'setup_steps', 'max_context_chars', 'loop_limit', 'format_retries', 'headless')} | {
                               'preparation_mode':getattr(args,'preparation_mode','on-demand')},
            'output_policy': 'actor/Judge: one unambiguous strict JSON object; single classification also accepts an exact bare primary label, missing follow-up is incorrect; actor-only bounded format retry; no semantic action repair',
            'observation_policy': 'complete live rendered body.innerText plus visible controls; full factual history',
            'jobs': jobs, 'counts': {e: len(c) for e, c in selected.items()},
            'source_families': {e: len({c.get('task_family_id', c.get('base_task_id', c['case_id'])) for c in cases})
                                for e, cases in selected.items()},
            'protocols': {'main': 'full autonomous continuation from common actual checkpoint; identical episode to ablation trajectory at same case/model/repeat',
                          'ablation': 'audited shared factual checkpoint; four event-boundary information projections followed by full autonomous execution; selected pre-event memory only',
                          'counterfactual': 'audited factual interventions; state-view initial decisions and full autonomous execution; invariant audit',
                          'multi': 'full continuation from audited common checkpoint; each visible queue stage scored separately; actual schedule uses whole episode',
                          'baseline': 'matched factual initial history and A checkpoint; no event delivered; common preparation not actor-scored',
                          'final_intent': 'effective revised goal supplied upfront'},
            'gold_human_reviewed': all(c.get('annotation', {}).get('human_reviewed') is True for cases in selected.values() for c in cases),
            'judge_human_calibrated': False,
            'judge_same_backbone_as_actor': args.judge_model in models}


def write_csv(path, rows, fields=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def status(root):
    manifest = read_json(root / 'manifest.json')
    if not manifest:
        return {'run_root': str(root), 'registered': False}
    states, active = {}, []
    for job in manifest['jobs']:
        saved = read_json(job_path(root, job) / 'job.json', {})
        state = saved.get('state', 'pending')
        states[state] = states.get(state, 0) + 1
        if state == 'running':
            active.append({**job, 'updated_at': saved.get('updated_at'), 'progress': saved.get('progress')})
    preparation = read_json(root / 'preparation_state.json')
    phase = 'evaluation'
    if preparation and preparation.get('state') != 'finished':
        phase = 'preparation'
    elif not active and states.get('finished', 0) == len(manifest['jobs']):
        phase = 'finished'
    return {'run_root': str(root), 'registered': True, 'total': len(manifest['jobs']),
            'states': states, 'active': active, 'phase': phase, 'preparation': preparation}


def metric_record(episode):
    fields = ('case_id', 'repeat', 'view', 'model', 'experiment', 'status', 'protocol', 'decisions',
              'follow_ups', 'predicted_schedule', 'decision_stages', 'events', 'evaluation', 'task_states', 'actual_schedule',
              'exposed', 'api_ok', 'decision_invalid', 'counterfactual_input_audit',
              'counterfactual_protocol_valid', 'preparation_status', 'checkpoint_key',
              'format_retry_count', 'format_diagnostics', 'navigation_errors', 'checkpoint_replay_audit',
              'shared_episode_file', 'information_policy', 'preparation_failure_status', 'preparation_failure_is_model',
              'environment_capture', 'failure_phase', 'eligibility_status')
    result = {k: copy.deepcopy(episode[k]) for k in fields if k in episode}
    result['responses'] = [{k: r.get(k) for k in ('stage', 'parsed', 'api_ok', 'error')}
                           for r in episode.get('responses', []) if r.get('stage') == 'decision']
    return result


def audit_counterfactual_groups(cases, episodes):
    """Require visible interventions and identical remaining actual inputs."""
    groups = {}
    by_id = {c['case_id']: c for c in cases}
    for episode in episodes:
        case = by_id[episode['case_id']]
        groups.setdefault((case['group_type'], case['group_id'], episode.get('repeat', 0)), []).append(episode)
    reports = []
    for (kind, group, repeat), members in groups.items():
        expected = 3 if kind == 'semantic' else 2
        audits = [m.get('counterfactual_input_audit') for m in members]
        available = len(members) == expected and all(isinstance(a, dict) for a in audits)
        if not available:
            valid, reason = None, 'actual_checkpoints_or_group_members_unavailable'
        else:
            invariants = {a.get('invariant_sha256') for a in audits}
            changes = {a.get('intervention_sha256') for a in audits}
            valid = (len(invariants) == 1 and None not in invariants and len(changes) == expected
                     and all(a.get('intervention_visible') is True for a in audits))
            reason = 'controlled_intervention_verified' if valid else 'noncontrolled_or_invisible_intervention'
        for member in members:
            member['counterfactual_protocol_valid'] = valid
        reports.append({'type': kind, 'group_id': group, 'repeat': repeat, 'valid': valid,
                        'reason': reason, 'case_ids': [m['case_id'] for m in members],
                        'individual_audits': audits})
    return reports


CORE_COLUMNS = {
    'main': ('Acc', 'MacroF1', 'FIR', 'MIR', 'FollowupAcc', 'FollowupF1', 'JointFUAcc',
             'I_SR', 'D_SR', 'H_SR', 'R_SR', 'T_SR', 'MacroESR', 'MicroESR', 'EHS', 'TaskCompletion', 'Coverage'),
    'ablation': ('Acc', 'MacroF1', 'FIR', 'MIR', 'FollowupAcc', 'FollowupF1', 'JointFUAcc', 'I_SR', 'D_SR', 'H_SR', 'R_SR', 'T_SR', 'MacroESR', 'MicroESR', 'EHS', 'TaskCompletion', 'Coverage'),
    'counterfactual': ('Acc', 'MacroF1', 'FIR', 'MIR', 'FollowupAcc', 'FollowupF1', 'JointFUAcc', 'I_SR', 'D_SR', 'H_SR', 'R_SR', 'T_SR', 'MacroESR', 'MicroESR', 'EHS', 'TaskCompletion', 'Coverage', 'OverallAcc', 'CFA', 'SFA', 'SemanticRobustAccuracy'),
    'multi': ('EventF1', 'CompleteCaseAcc', 'PriorityAcc', 'CriticalEventRecall', 'FIR',
              'ScheduleExactMatch', 'ActualPriorityAcc', 'ActualScheduleExactMatch', 'multi_ESR', 'Coverage'),
    'baseline': ('TaskCompletion', 'MicroESR', 'Coverage'),
    'final_intent': ('TaskCompletion', 'MicroESR', 'Coverage'),
}


def summarize(root, selected, models, args, source_root=None):
    from eventarena.online_v2_metrics import score_cases
    read_root = source_root or root
    all_rows, failure_rows, case_studies, diagnostics_rows = [], [], [], []
    for experiment, cases in selected.items():
        rows, cf_rows = [], {'goal': [], 'state': [], 'semantic': []}
        for name in models:
            metrics_by_view = {}
            for view in job_views(experiment):
                episodes = []
                for repeat in range(args.repeats):
                    for case in cases:
                        job = {'experiment': experiment, 'model': name, 'view': view,
                               'repeat': repeat, 'case_id': case['case_id']}
                        episode = read_json(job_path(read_root, job) / 'metric_record.json')
                        if experiment == 'multi' and episode is not None and 'decision_stages' not in episode:
                            complete = read_json(job_path(read_root, job) / 'episode.json', {})
                            if 'decision_stages' in complete:
                                episode['decision_stages'] = copy.deepcopy(complete['decision_stages'])
                        if episode is None:
                            episode = {'case_id': case['case_id'], 'repeat': repeat, 'view': view,
                                       'status': 'registered_pending', 'api_ok': False,
                                       'protocol': 'controlled_episode_v2' if experiment in {'main','ablation','counterfactual','baseline'} else 'continuous_v2',
                                       'evaluation': {'episode_success': None, 'branch_success': None}}
                        if episode:
                            episodes.append(episode)
                            evaluation = episode.get('evaluation', {})
                            if experiment == 'multi' and episode.get('status') != 'registered_pending':
                                truth = case.get('gold', {}).get('decisions', {})
                                planned_correct = all(episode.get('decisions', {}).get(eid) == (g.get('decision') if isinstance(g, dict) else g)
                                                      for eid, g in truth.items())
                                if (any(episode.get('events', {}).get(eid, {}).get('delivered') is False for eid in truth)
                                    or episode.get('status') in {'provider_error','checkpoint_unavailable','registered_pending'}):
                                    planned_correct = None
                                case_studies.append({'case_study': 'D', 'model': name, 'case_id': case['case_id'],
                                                     'repeat': repeat, 'planned_decisions_correct': planned_correct,
                                                     'episode_success': evaluation.get('episode_success'),
                                                     'actual_schedule': evaluation.get('actual_schedule'),
                                                     'predicted_schedule': episode.get('predicted_schedule'),
                                                     'failure_category': evaluation.get('failure_category'),
                                                     'episode_file': str(job_path(read_root, job) / 'episode.json')})
                if experiment == 'counterfactual':
                    audits = audit_counterfactual_groups(cases, episodes)
                    write_json(root / experiment / name / 'input_invariance_audit.json', audits)
                metrics = score_cases(cases, episodes, experiment=experiment, view=view)
                if True:  # All experiments now produce independent execution outcomes.
                    by_case_repeat = {(e['case_id'], e.get('repeat', 0)): e for e in episodes}
                    for failure in metrics.get('failure_ledger', {}).get('rows', []):
                        if failure['category'] == 'success':
                            continue
                        episode = by_case_repeat.get((failure['case_id'], failure['repeat']), {})
                        evaluation = episode.get('evaluation', {})
                        job = {'experiment': experiment, 'model': name, 'view': view,
                               'repeat': failure['repeat'], 'case_id': failure['case_id']}
                        failure_rows.append({'experiment': experiment, 'model': name, 'view': view,
                            **{k: failure[k] for k in ('case_id','repeat')}, 'status': episode.get('status'),
                            'failure_category': failure['category'],
                            'raw_failure_category': evaluation.get('failure_category'),
                            'episode_success': evaluation.get('episode_success'),
                            'episode_file': str(job_path(read_root, job) / 'episode.json')})
                diagnostics_rows.append({'experiment':experiment, 'model':name, 'view':view,
                                         **{k:v.get('rate') for k,v in metrics.get('diagnostics',{}).items()}})
                metrics_by_view[view] = metrics
                row = {'model': name, 'view': view, 'cases': len(cases), 'repeats': args.repeats}
                row.update({k: metrics.get(k) for k in CORE_COLUMNS[experiment]})
                if experiment in {'main','ablation','counterfactual'}:
                    row.update(CoveredAcc=metrics.get('denominators',{}).get('Acc',{}).get('covered_rate'),
                               DecisionCoverage=metrics.get('denominators',{}).get('Acc',{}).get('coverage'),
                               CoveredMacroF1=metrics.get('classification',{}).get('covered_macro_f1'),
                               CoveredMicroESR=metrics.get('denominators',{}).get('MicroESR',{}).get('covered_rate'))
                for key in ('Acc','MicroESR'):
                    if key in CORE_COLUMNS[experiment]:
                        counts = metrics.get('denominators', {}).get(key, {})
                        row.update({f'{key}_{field}': counts.get(field) for field in ('n','N','known','unknown','lower_bound','upper_bound')})
                if experiment in {'main','ablation','counterfactual'}:
                    row['Followup_eligible_N'] = metrics.get('denominators', {}).get('FollowupAcc', {}).get('N')
                    row['Gold_interrupt_N'] = metrics.get('denominators', {}).get('MIR', {}).get('N')
                rows.append(row)
                all_rows.append({'experiment': experiment, **row})
                if experiment == 'counterfactual':
                    for kind, result in metrics.get('counterfactual', {}).get('types', {}).items():
                        type_path = root / experiment / name / f'{view}_{kind}.json'
                        write_json(type_path, result)
                        cf_rows[kind].append({'model': name, 'view': view, 'type': kind,
                                              **{k: v for k, v in result.items() if not isinstance(v, (dict, list))},
                                              **{k:result.get('common_metrics',{}).get(k) for k in CORE_COLUMNS['main']},
                                              'case_N': result['case_counts']['N'],
                                              'group_N': result['group_counts']['N'],
                                              'group_known': result['group_counts']['known'],
                                              'group_coverage': result['group_counts']['coverage']})
            write_json(root / experiment / name / 'metrics.json', metrics_by_view)
        write_csv(root / experiment / 'comparison.csv', rows)
        if experiment == 'counterfactual':
            for kind, type_rows in cf_rows.items():
                write_csv(root / experiment / (kind + '_comparison.csv'), type_rows)
    write_csv(root / 'all_results.csv', all_rows)
    from paired_online_v2 import paired_coverage
    write_csv(root/'paired_coverage.csv',paired_coverage(read_root,selected,models,args.repeats))
    write_csv(root / 'diagnostics.csv', diagnostics_rows)
    write_csv(root / 'failure_ledger.csv', failure_rows, ['experiment', 'model', 'view', 'repeat', 'case_id', 'status',
                                                       'failure_category', 'raw_failure_category', 'episode_success', 'episode_file'])
    # Retain all candidate traces, explicitly labelled success/failure/unverified.
    # A correct plan followed by failed execution must not disappear from Case D.
    (root / 'case_studies.jsonl').write_text(''.join(json.dumps(c, ensure_ascii=False) + '\n' for c in case_studies), encoding='utf-8')
    write_json(root / 'status.json', status(read_root))


class LimitedModel:
    def __init__(self, model, run_root=None, name=None):
        self.model = model
        self.lock = asyncio.Lock()
        self.run_root, self.name = run_root, name

    @property
    def usage(self):
        return self.model.usage

    async def call(self, messages):
        async with self.lock:
            before = len(self.model.usage)
            started = now()
            row = {'configured_name': self.name, 'started_at': started, 'prompt_sha256': sha(messages),
                   'api_ok': False}
            try:
                result = await asyncio.to_thread(self.model.call, messages)
                row.update(api_ok=True, response_sha256=sha(result))
                return result
            finally:
                row['finished_at'] = now()
                if len(self.model.usage) > before:
                    row.update(self.model.usage[-1])
                if self.run_root:
                    with (self.run_root / 'model_call_usage.jsonl').open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def checkpoint_key(case, repeat):
    # State interventions needing actual setup must have separate factual runs.
    group = case.get('checkpoint_group_id', case.get('group_id', case['case_id']))
    if case.get('group_type') == 'state':
        group = case['case_id']
    return str(group) + f'_repeat_{repeat}'


async def obtain_checkpoint(case, repeat, selected_cases, root, prep_model, opts, locks):
    from eventarena.online_v2_engine import prepare_checkpoint, prepare_state_pair
    if case.get('group_type') == 'state':
        key = f"state_pair_{case['group_id']}_repeat_{repeat}"
        async with locks.setdefault(key, asyncio.Lock()):
            directory = root / 'checkpoints' / key
            pair = read_json(directory / 'state_pair.json')
            if not pair:
                members = [c for c in selected_cases if c.get('group_type') == 'state'
                           and c['group_id'] == case['group_id']]
                pair = await prepare_state_pair(members, prep_model, directory, opts)
                write_json(directory / 'state_pair.json', pair)
            checkpoint = copy.deepcopy(pair.get('checkpoints', {}).get(case['case_id'], {}))
            return pair, checkpoint, key
    key = checkpoint_key(case, repeat)
    async with locks.setdefault(key, asyncio.Lock()):
        directory = root / 'checkpoints' / key
        prepared = read_json(directory / 'episode.json')
        if not prepared:
            prep_case = copy.deepcopy(case)
            source_id = case.get('cf', {}).get('checkpoint_source_case')
            source = next((c for c in selected_cases if c['case_id'] == source_id), case)
            prep_case = copy.deepcopy(source)
            prep_case['goal'] = case.get('checkpoint_navigation_goal', source['goal'])
            prepared = await prepare_checkpoint(prep_case, prep_model, directory, opts)
        checkpoint = copy.deepcopy(prepared.get('checkpoint', {}))
        return prepared, checkpoint, key


async def execute(args, root, selected, names):
    from eventarena.providers import Model
    from eventarena.online_v2_engine import run_episode, run_checkpoint_episode
    from eventarena.online_v2_eval import evaluate_episode
    models = {name: LimitedModel(Model(name, args.config), root, name)
              for name in set(names + [args.prep_model, args.judge_model]) if name}
    locks, done = {}, 0
    retried_shared = set()
    manifest = read_json(root / 'manifest.json')
    cases = {(experiment, case['case_id']): case for experiment, rows in selected.items() for case in rows}
    single_cases = read_cases(args.data_dir / FILES['main'])
    original_by_id = {c['case_id']: c for c in single_cases}
    queue = asyncio.Queue()
    if args.retry_infra:
        # One explicit recovery of failed preparation per invocation. Preserve
        # every old browser trace instead of overwriting it for each backbone.
        checkpoint_root = root / 'checkpoints'
        if checkpoint_root.exists():
            stamp = now().replace(':', '').replace('+', '_')
            for directory in list(checkpoint_root.iterdir()):
                if not directory.is_dir() or '_failed_' in directory.name:
                    continue
                saved = read_json(directory / 'state_pair.json') or read_json(directory / 'episode.json')
                if saved and saved.get('status') != 'checkpoint_ready':
                    directory.rename(directory.with_name(directory.name + '_failed_' + stamp))
    eligibility = None
    if getattr(args,'preparation_mode','on-demand') == 'before-actors':
        from prepare_online_v2 import prepare_pool
        eligibility = await prepare_pool(args,root,selected,models,locks,obtain_checkpoint)
        if args.command == 'prepare':
            return
    for job in manifest['jobs']:
        queue.put_nowait(job)

    async def worker():
        nonlocal done
        while not queue.empty():
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            folder = job_path(root, job)
            folder.mkdir(parents=True, exist_ok=True)
            old_job = read_json(folder / 'job.json', {})
            old_episode = read_json(folder / 'episode.json')
            retryable = old_episode and old_episode.get('evaluation', {}).get('failure_category') in {
                'provider_failure', 'environment_invalid', 'evaluation_unverified', 'provider_error',
                'website_unavailable', 'browser_error', 'checkpoint_setup_unverified', 'judge_or_outcome_unverified'}
            retryable = retryable or bool(old_episode and old_episode.get('status') in {'provider_error', 'checkpoint_unavailable'})
            if args.resume and old_job.get('state') == 'finished' and not (args.retry_infra and retryable):
                done += 1
                print(f"[{done}/{len(manifest['jobs'])}] CACHED {job['model']} {job['experiment']}/{job['view']} repeat={job['repeat']} {job['case_id']}", flush=True)
                queue.task_done()
                continue
            attempt = old_job.get('attempt', 0) + 1
            attempt_folder = folder / 'attempts' / f'attempt_{attempt:03d}'
            attempt_folder.mkdir(parents=True, exist_ok=True)
            if old_episode:
                write_json(folder / 'previous_attempts' / f'episode_{attempt-1}.json', old_episode)
            state = {**job, 'state': 'running', 'attempt': attempt, 'started_at': now(), 'updated_at': now()}
            write_json(folder / 'job.json', state)

            def progress(info):
                state.update(updated_at=now(), progress=info)
                write_json(folder / 'job.json', state)
                with (root / 'progress.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'timestamp': now(), **job, **info}, ensure_ascii=False) + '\n')
                print(f"  {job['model']} {job['case_id']} {job['experiment']}/{job['view']} {info.get('stage','')} {info.get('state','')}", flush=True)

            judge_records = []

            async def judge(messages):
                row = {'messages': messages, 'api_ok': False}
                progress({'stage': 'judge', 'state': 'calling'})
                try:
                    row['raw'] = await models[args.judge_model].call(messages)
                    row['api_ok'] = True
                    return row['raw']
                except Exception as error:
                    row['error_type'] = type(error).__name__
                    raise
                finally:
                    judge_records.append(row)
                    write_json(attempt_folder / 'judge_responses.json', judge_records)
                    progress({'stage': 'judge', 'state': 'returned', 'api_ok': row['api_ok']})

            opts = {k: getattr(args, k) for k in ('headless', 'max_steps', 'pre_steps', 'setup_steps',
                                                 'max_context_chars', 'loop_limit', 'format_retries')}
            opts.update(repeat=job['repeat'], progress=progress, judge=judge if args.judge_model else None)
            case = cases[(job['experiment'], job['case_id'])]
            try:
                if job['experiment'] in {'main', 'ablation', 'counterfactual', 'baseline', 'multi'}:
                    source_case = original_by_id[case['original_case_id']] if job['experiment'] == 'baseline' else case
                    source_pool = selected['counterfactual'] if job['experiment'] == 'counterfactual' else single_cases
                    prepared, cp, key = await obtain_checkpoint(source_case, job['repeat'], source_pool,
                                                              root, models[args.prep_model], opts, locks)
                    gate = eligibility.get((source_case['case_id'],job['repeat'])) if eligibility is not None else None
                    if prepared.get('status') != 'checkpoint_ready' or not cp or (gate is not None and not gate['eligible']):
                        episode = {'case_id': case['case_id'], 'repeat': job['repeat'], 'view': job['view'],
                                   'protocol': 'controlled_episode_v2', 'status': 'checkpoint_unavailable',
                                   'decisions': {}, 'follow_ups': {}, 'responses': [],
                                   'preparation_status': prepared.get('status'),
                                   'preparation_failure_status': prepared.get('preparation_failure_status'),
                                   'preparation_failure_is_model': prepared.get('preparation_failure_is_model'),
                                   'checkpoint_key':key,
                                   'eligibility_status': gate,
                                   'evaluation': {'episode_success': None, 'branch_success': None,
                                                  'environment_valid':False,
                                                  'failure_category': 'checkpoint_preparation_failed',
                                                  'common_preparer_failure_not_tested_actor':True}}
                    else:
                        cp['goal'] = case['goal']
                        cp['events'] = copy.deepcopy(case.get('events', []))
                        cp['event'] = copy.deepcopy(cp['events'][0]) if len(cp['events']) == 1 else None
                        share = job['experiment'] == 'main' or (job['experiment'] == 'ablation' and job['view'] == 'trajectory')
                        if share:
                            cache_key = f"full/{job['model']}/{job['repeat']}/{case['case_id']}"
                            async with locks.setdefault(cache_key, asyncio.Lock()):
                                shared_file = root / 'shared_episodes' / job['model'] / f"repeat_{job['repeat']}" / case['case_id'] / 'episode.json'
                                episode = read_json(shared_file)
                                if args.retry_infra and cache_key not in retried_shared:
                                    # Main and Full Trajectory are two reports of one
                                    # episode. Recovery must not rerun it for each alias.
                                    retried_shared.add(cache_key)
                                    if episode and episode.get('evaluation',{}).get('environment_valid') is False:
                                        archived = shared_file.with_name(f"episode_failed_{attempt:03d}.json")
                                        shared_file.replace(archived)
                                        episode = None
                                if episode is None:
                                    episode = await run_checkpoint_episode(case, models[job['model']], cp, 'trajectory', attempt_folder, opts)
                                    episode['shared_episode_file'] = str(attempt_folder / 'episode.json')
                                    write_json(shared_file, episode)
                                else:
                                    episode = copy.deepcopy(episode)
                        else:
                            episode = await run_checkpoint_episode(case, models[job['model']], cp, job['view'], attempt_folder, opts,
                                                                   suppress_events=job['experiment'] == 'baseline')
                        episode['checkpoint_key'] = key
                else:
                    episode = await run_episode(case, models[job['model']], attempt_folder, opts)
                    if not episode.get('evaluation'):
                        episode['evaluation'] = await evaluate_episode(case, episode, judge if args.judge_model else None)
                episode.update(repeat=job['repeat'], view=job['view'], model=job['model'], experiment=job['experiment'])
                episode['judge_responses'] = judge_records
                episode['attempt_directory'] = str(attempt_folder)
                write_json(folder / 'episode.json', episode)
                write_json(folder / 'metric_record.json', metric_record(episode))
                state.update(state='finished', updated_at=now(), result=episode.get('evaluation', {}).get('episode_success'),
                             result_status=episode.get('status'))
                write_json(folder / 'job.json', state)
                done += 1
                prep_note = f" (preparation={episode['preparation_status']})" if episode.get('preparation_status') else ''
                print(f"[{done}/{len(manifest['jobs'])}] DONE {job['model']} {job['experiment']}/{job['view']} repeat={job['repeat']} {job['case_id']}: {state['result_status']}{prep_note} ESR={state['result']}", flush=True)
            except asyncio.CancelledError:
                state.update(state='interrupted', updated_at=now())
                write_json(folder / 'job.json', state)
                raise
            except Exception as error:
                # Never log provider response bodies, authorization or exception text.
                episode = {'case_id': case['case_id'], 'repeat': job['repeat'], 'view': job['view'],
                           'status': 'runner_error', 'error_type': type(error).__name__, 'responses': [],
                           'evaluation': {'episode_success': None, 'failure_category': 'evaluation_unverified'}}
                write_json(folder / 'episode.json', episode)
                write_json(folder / 'metric_record.json', metric_record(episode))
                state.update(state='finished', updated_at=now(), result_status='runner_error', error_type=type(error).__name__)
                write_json(folder / 'job.json', state)
                done += 1
                print(f"[{done}/{len(manifest['jobs'])}] UNVERIFIED {job['id']} {type(error).__name__}", flush=True)
                # Frame locations identify implementation failures without
                # printing exception bodies, response headers, or local values.
                (attempt_folder / 'error_traceback.txt').write_text(''.join(traceback.format_tb(error.__traceback__)), encoding='utf-8')
                raise RuntimeError(f"Runner stopped after {type(error).__name__}; inspect {attempt_folder / 'error_traceback.txt'}") from None
            finally:
                queue.task_done()
                if done % 20 == 0 or queue.empty():
                    summarize(root, selected, names, args)

    await asyncio.gather(*(worker() for _ in range(args.workers)))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('run', 'prepare', 'status', 'evaluate'))
    p.add_argument('--config', type=Path, default=ROOT / 'models.gateway.json')
    p.add_argument('--data-dir', type=Path, default=ROOT / 'data/online_v2')
    p.add_argument('--run-root', type=Path, required=True)
    p.add_argument('--only', help='Comma-separated configured model names; default enabled models')
    p.add_argument('--experiments', default='all')
    p.add_argument('--single-limit', type=int, default=100)
    p.add_argument('--cf-groups', type=int, default=10)
    p.add_argument('--cf-group-ids', help='Exact registered CF groups, e.g. G07,T07,P07; overrides --cf-groups')
    p.add_argument('--multi-limit', type=int, default=20)
    p.add_argument('--case-ids')
    p.add_argument('--multi-case-ids')
    p.add_argument('--split', choices=('all', 'development', 'heldout_candidate'), default='all')
    p.add_argument('--repeats', type=int, default=1)
    p.add_argument('--seed', type=int, default=20261004)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--prep-model')
    p.add_argument('--judge-model', help='Fixed outcome Judge; use none for deterministic engineering fixtures only')
    p.add_argument('--max-steps', type=int, default=70)
    p.add_argument('--pre-steps', type=int, default=35)
    p.add_argument('--setup-steps', type=int, default=40)
    p.add_argument('--max-context-chars', type=int, default=450000)
    p.add_argument('--loop-limit', type=int, default=4)
    p.add_argument('--format-retries', type=int, choices=(0,1), default=1,
                   help='Actor-only JSON format retry; identical policy for all models, no semantic repair')
    p.add_argument('--headless', action='store_true')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--retry-infra', action='store_true', help='Retry infrastructure/unverified jobs, preserving previous results; never retry scored Agent failures')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--strict-review', action='store_true')
    p.add_argument('--preparation-mode',choices=('before-actors','on-demand'),default='before-actors',
                   help='Freeze shared preparation and browser replay eligibility before any tested actor call')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'prepare' and args.preparation_mode != 'before-actors':
        raise ValueError('prepare requires before-actors; it must never sample tested actors')
    root = args.run_root.expanduser().resolve()
    if args.command == 'status':
        print(json.dumps(status(root), ensure_ascii=False, indent=2))
        return
    if args.command == 'evaluate':
        saved = read_json(root / 'manifest.json')
        if not saved:
            raise ValueError('Run manifest not found')
        if saved.get('code_sha256') != code_fingerprint():
            raise ValueError('Registered code changed; use its frozen version to evaluate and preserve the original scoring protocol')
        for key, value in saved['arguments'].items():
            setattr(args, key, value)
        selected = {e: read_cases(root / 'registered_data' / (e + '.jsonl')) for e in saved['counts']}
        summarize(root, selected, saved['models'], args)
        print('Result tables saved:', root)
        return
    args.config, args.data_dir = args.config.resolve(), args.data_dir.resolve()
    from validate_online_v2 import validate
    validate(args.data_dir, strict_review=args.strict_review)
    args.experiments = list(EXPERIMENTS) if args.experiments == 'all' else args.experiments.split(',')
    if len(set(args.experiments)) != len(args.experiments) or any(e not in EXPERIMENTS for e in args.experiments):
        raise ValueError('Unknown or duplicate experiment')
    if min(args.repeats, args.workers, args.single_limit, args.cf_groups, args.multi_limit,
           args.max_steps, args.pre_steps, args.setup_steps, args.max_context_chars, args.loop_limit) < 1:
        raise ValueError('Limits and budgets must be positive')
    config = read_json(args.config)
    configured = {m['name']: m for m in config['models']}
    names = args.only.split(',') if args.only else [m['name'] for m in config['models'] if m.get('enabled')]
    if not names or len(names) != len(set(names)) or any(n not in configured for n in names):
        raise ValueError('Choose unique configured model names')
    args.prep_model = args.prep_model or names[0]
    args.judge_model = None if args.judge_model == 'none' else args.judge_model or names[0]
    if any(name not in configured for name in [args.prep_model, args.judge_model] if name):
        raise ValueError('Unknown preparation or Judge model')
    selected = select_cases(args)
    jobs = make_jobs(selected, names, args.repeats, args.seed)
    manifest = make_manifest(args, selected, names, configured, jobs)
    print(json.dumps({'protocol': 'v2', 'models': names, 'counts': manifest['counts'],
                      'source_families': manifest['source_families'], 'registered_jobs': len(jobs),
                      'repeats': args.repeats, 'prep_model': args.prep_model, 'judge_model': args.judge_model,
                      'no_api_call': args.dry_run, 'gold_human_reviewed': manifest['gold_human_reviewed']}, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        return
    for name in set(names + [args.prep_model, args.judge_model]):
        if name and not os.environ.get(configured[name]['api_key_env']):
            raise ValueError('Set ' + configured[name]['api_key_env'] + ' before running')
    previous = read_json(root / 'manifest.json')
    if previous:
        if not args.resume:
            raise ValueError('Existing registered run; use --resume or a new run directory')
        if previous != manifest:
            raise ValueError('Dataset, code, config or registered arguments changed; use a new run directory')
    elif root.exists() and any(p.name != 'logs' for p in root.iterdir()):
        raise ValueError('Nonempty directory without a registered manifest')
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / 'manifest.json', manifest)
    for experiment, cases in selected.items():
        folder = root / 'registered_data'
        folder.mkdir(exist_ok=True)
        (folder / (experiment + '.jsonl')).write_text(''.join(json.dumps(c, ensure_ascii=False) + '\n' for c in cases), encoding='utf-8')
    runtime = os.environ.get('EA_M2W_RUNTIME')
    if runtime:
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', runtime + '/browsers')
    asyncio.run(execute(args, root, selected, names))
    if args.command == 'prepare':
        print('Common preparation and reset audit finished; tested actors were not started:', root, flush=True)
        return
    summarize(root, selected, names, args)
    print('All registered jobs finished; results:', root, flush=True)


if __name__ == '__main__':
    main()
