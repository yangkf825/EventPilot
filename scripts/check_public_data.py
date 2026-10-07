#!/usr/bin/env python3
"""Validate published author definitions without downloading source tasks."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {'single_event.jsonl': (100, 100), 'counterfactual.jsonl': (70, 70),
            'multi_event.jsonl': (20, 70)}
FORBIDDEN_FIELDS = {'original_goal', 'source_a', 'source_b', 'source_c',
                    'raw_html', 'cleaned_html', 'action_reprs', 'entry_readiness'}


def check_projection(value, location='case'):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_FIELDS:
                raise ValueError(f'Private source/runtime field in public definitions: {location}.{key}')
            check_projection(item, f'{location}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_projection(item, f'{location}[{index}]')


def check(root=ROOT):
    root = Path(root)
    folder = root / 'data/author_candidates'
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('runtime_dataset') is not False or manifest.get('original_source_texts_included') is not False:
        raise ValueError('Public manifest must distinguish candidates from private runtime data')
    registry = json.loads((root / 'data/source_metadata/source_task_ids.json').read_text(encoding='utf-8'))
    metadata = json.loads((root / 'data/source_metadata/mind2web_metadata.json').read_text(encoding='utf-8'))
    if registry['revision'] != metadata['sha']:
        raise ValueError('Official source revision mismatch')
    sources = {row['task_id']: row for row in registry['tasks']}
    if len(sources) != 54 or len(registry['tasks']) != 54:
        raise ValueError('Expected exactly 54 unique registered source tasks')
    for source in sources.values():
        if set(source) != {'task_id', 'website', 'split', 'original_goal_sha256'}:
            raise ValueError('Source registry must contain identifiers and hashes only')
        if not re.fullmatch('[0-9a-f]{64}', source['original_goal_sha256']):
            raise ValueError('Invalid registered source goal hash')
    if not re.fullmatch('[0-9a-f]{64}', registry['expected_private_source_index_sha256']):
        raise ValueError('Missing frozen local source index hash')
    datasets = {}
    for name, (case_count, event_count) in EXPECTED.items():
        raw = (folder / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest['files'][name]['sha256']:
            raise ValueError('Published definition hash mismatch: ' + name)
        cases = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        count = sum(len(case['events']) for case in cases)
        if (len(cases), count) != (case_count, event_count):
            raise ValueError('Unexpected case/event counts: ' + name)
        if (manifest['files'][name]['cases'], manifest['files'][name]['events']) != (case_count, event_count):
            raise ValueError('Public manifest counts disagree: ' + name)
        if len({case['case_id'] for case in cases}) != len(cases):
            raise ValueError('Duplicate public case IDs: ' + name)
        for case in cases:
            check_projection(case, case['case_id'])
            for key in ('source', 'source_task_a', 'source_task_b', 'source_task_c'):
                source = case.get(key)
                if not source:
                    continue
                if source['task_id'] not in sources:
                    raise ValueError('Unregistered source ID: ' + case['case_id'])
                if source['source_split'] != sources[source['task_id']]['split']:
                    raise ValueError('Source split mismatch: ' + case['case_id'])
            for event in case['events']:
                if event['gold']['decision'] not in {'IGNORE', 'DEFER', 'INTERRUPT'}:
                    raise ValueError('Invalid event decision: ' + case['case_id'])
        datasets[name] = cases
    single = datasets['single_event.jsonl']
    if Counter(c['gold']['decision'] for c in single) != {'IGNORE': 30, 'DEFER': 30, 'INTERRUPT': 40}:
        raise ValueError('Registered single-event class balance changed')
    if Counter(c['gold']['follow_up'] for c in single if c['gold']['decision'] == 'INTERRUPT') != {
            'HANDLE': 20, 'REPLAN': 10, 'TERMINATE': 10}:
        raise ValueError('Registered follow-up class balance changed')
    if Counter(c['difficulty'] for c in single) != {'easy': 30, 'medium': 50, 'hard': 20}:
        raise ValueError('Registered single-event difficulty mix changed')
    cf = datasets['counterfactual.jsonl']
    expected_members = {'goal': 2, 'state': 2, 'semantic': 3}
    if Counter(c['cf_type'] for c in cf) != {'goal': 20, 'state': 20, 'semantic': 30}:
        raise ValueError('Counterfactual type counts changed')
    for kind, members in expected_members.items():
        groups = Counter(c['group_id'] for c in cf if c['cf_type'] == kind)
        if len(groups) != 10 or set(groups.values()) != {members}:
            raise ValueError('Counterfactual group membership changed: ' + kind)
    return {'status': 'ok', 'scope': 'public author definitions; no source download or live execution',
            'datasets': {name: {'cases': len(cases), 'events': sum(len(c['events']) for c in cases)}
                         for name, cases in datasets.items()},
            'registered_source_tasks': len(sources), 'source_revision': registry['revision'],
            'human_reviewed': manifest['human_reviewed']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.root), ensure_ascii=False, indent=2))
    except (ValueError, KeyError, FileNotFoundError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
