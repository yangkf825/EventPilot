#!/usr/bin/env python3
"""Family-cluster confidence intervals and paired differences for registered v2 runs."""
from __future__ import annotations
import argparse
import csv
import itertools
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from run_online_v2 import (CORE_COLUMNS, audit_counterfactual_groups, job_path,
                           job_views, read_cases, read_json, write_csv, write_json)
from eventarena.online_v2_metrics import score_cases


def replicate(cases, episodes, draw):
    """Resample entire source families including every condition and repeat."""
    by_family = {}
    by_case = {}
    for case in cases:
        by_family.setdefault(case['task_family_id'], []).append(case)
    for episode in episodes:
        by_case.setdefault(episode['case_id'], []).append(episode)
    selected_cases, selected_episodes = [], []
    for index, family in enumerate(draw):
        prefix = f'bootstrap_{index}_'
        for case in by_family[family]:
            clone = {**case, 'case_id': prefix + case['case_id'], 'task_family_id': prefix + family}
            for key in ('group_id', 'cf_group_id'):
                if key in clone:
                    clone[key] = prefix + clone[key]
            selected_cases.append(clone)
            selected_episodes.extend({**e, 'case_id': clone['case_id']} for e in by_case.get(case['case_id'], []))
    return selected_cases, selected_episodes


def quantile(values, probability):
    values = sorted(values)
    position = probability * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def interval(point, values, clusters, confidence):
    if point is None or any(v is None for v in values):
        return {'estimate': point, 'lower': None, 'upper': None, 'status': 'unknown_outcomes_or_resample'}
    if clusters < 2:
        return {'estimate': point, 'lower': None, 'upper': None, 'status': 'insufficient_independent_families'}
    tail = (1 - confidence) / 2
    return {'estimate': point, 'lower': quantile(values, tail), 'upper': quantile(values, 1-tail), 'status': 'ok'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', type=Path, required=True)
    parser.add_argument('--resamples', type=int, default=1000)
    parser.add_argument('--seed', type=int, default=20261004)
    parser.add_argument('--confidence', type=float, default=.95)
    args = parser.parse_args()
    if args.resamples < 1 or not 0 < args.confidence < 1:
        parser.error('Positive resamples and confidence between 0 and 1 required')
    root = args.run_root.resolve()
    manifest = read_json(root / 'manifest.json')
    if not manifest:
        parser.error('Registered manifest not found')
    all_rows, paired_rows = [], []
    results = {}
    for experiment in manifest['counts']:
        cases = read_cases(root / 'registered_data' / (experiment + '.jsonl'))
        families = sorted({c['task_family_id'] for c in cases})
        rng = random.Random(args.seed)
        draws = [rng.choices(families, k=len(families)) for _ in range(args.resamples)]
        if not families:
            continue
        for view in job_views(experiment):
            model_values = {}
            for model in manifest['models']:
                episodes = []
                for repeat in range(manifest['arguments']['repeats']):
                    for case in cases:
                        job = {'experiment': experiment, 'model': model, 'view': view,
                               'repeat': repeat, 'case_id': case['case_id']}
                        episode = read_json(job_path(root, job) / 'metric_record.json') or {
                            'case_id': case['case_id'], 'repeat': repeat, 'view': view,
                            'status': 'registered_pending', 'evaluation': {}}
                        if experiment == 'multi' and 'decision_stages' not in episode:
                            full = read_json(job_path(root, job) / 'episode.json', {})
                            if 'decision_stages' in full:
                                episode['decision_stages'] = full['decision_stages']
                        episodes.append(episode)
                if experiment == 'counterfactual':
                    audit_counterfactual_groups(cases, episodes)
                point = score_cases(cases, episodes, experiment=experiment, view=view)
                # Do not pretend unavailable registered outcomes have a formal CI.
                columns = [k for k in CORE_COLUMNS[experiment] if point.get(k) is not None]
                values = {k: [] for k in columns}
                for index, draw in enumerate(draws if columns else []):
                    sample_cases, sample_episodes = replicate(cases, episodes, draw)
                    sample = score_cases(sample_cases, sample_episodes, experiment=experiment, view=view)
                    for key in columns:
                        values[key].append(sample.get(key))
                    if (index + 1) % 100 == 0:
                        print(f'{experiment}/{model}/{view}: bootstrap {index+1}/{args.resamples}', flush=True)
                intervals = {k: interval(point.get(k), values.get(k, [None]), len(families), args.confidence)
                             for k in CORE_COLUMNS[experiment]}
                results[f'{experiment}/{model}/{view}'] = {'families': len(families), 'intervals': intervals}
                for key, result in intervals.items():
                    all_rows.append({'experiment': experiment, 'model': model, 'view': view,
                                     'metric': key, 'families': len(families), **result})
                model_values[model] = (point, values)
            for left, right in itertools.combinations(manifest['models'], 2):
                p_left, v_left = model_values[left]
                p_right, v_right = model_values[right]
                for key in CORE_COLUMNS[experiment]:
                    point = None if p_left.get(key) is None or p_right.get(key) is None else p_left[key] - p_right[key]
                    a, b = v_left.get(key), v_right.get(key)
                    values = [None if x is None or y is None else x-y for x, y in zip(a,b)] if a and b else [None]
                    result = interval(point, values, len(families), args.confidence)
                    paired_rows.append({'experiment': experiment, 'view': view, 'left': left, 'right': right,
                                        'difference': 'left_minus_right', 'metric': key,
                                        'families': len(families), **result})
    output = root / 'statistics'
    write_json(output / 'cluster_confidence_intervals.json', {
        'method': 'percentile_source_family_cluster_bootstrap', 'resamples': args.resamples,
        'seed': args.seed, 'confidence': args.confidence,
        'replicate_unit': 'all cases, counterfactual members and repeats within a source family',
        'caution': 'Author-candidate pilot; intervals do not establish out-of-family generalization. No automatic significance claims or score-based exclusions.',
        'results': results})
    write_csv(output / 'confidence_intervals.csv', all_rows)
    write_csv(output / 'paired_model_differences.csv', paired_rows)
    print('Statistics saved:', output)


if __name__ == '__main__':
    main()
