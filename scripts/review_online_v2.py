#!/usr/bin/env python3
"""Export blinded review forms and import real independent reviewer decisions.

This tool cannot create human reviews. Disagreements with proposed Gold remain
unresolved; changing labels or task contracts requires a new authored version.
"""
from __future__ import annotations
import argparse
import copy
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from validate_online_v2 import FILES, read_cases
from run_online_v2 import write_csv, write_json


def load_dataset(folder):
    return {filename: read_cases(folder / filename) for filename in sorted(set(FILES.values()))}


def export_form(folder, output, reviewer_id):
    rows = []
    for filename, cases in load_dataset(folder).items():
        for case in cases:
            for event in case['events']:
                rows.append({'case_id': case['case_id'], 'event_id': event['id'],
                    'reviewer_id': reviewer_id, 'task_goal': case['goal'],
                    'common_rules': json.dumps(case['rules'], ensure_ascii=False),
                    'state_condition_to_validate': json.dumps(case.get('setup_tasks', []), ensure_ascii=False),
                    'trigger': json.dumps(case['trigger'], ensure_ascii=False),
                    'event_source': event['source'], 'event_text': event['text'],
                    'original_source_a': case['source_task_a']['original_goal'],
                    'original_source_b': case['source_task_b']['original_goal'],
                    'original_source_c': case['source_task_c']['original_goal'],
                    'event_source_task_id': (event.get('source_task') or {}).get('task_id', ''),
                    'event_original_task': (event.get('source_task') or {}).get('original_goal', ''),
                    'event_adapted_task_goal': event['execution'].get('goal', event['execution'].get('updated_goal', '')),
                    'decision': '', 'follow_up': '', 'rationale': '',
                    'source_adaptation_valid': '', 'control_design_valid': '',
                    'independent_review': '', 'notes': ''})
    write_csv(output, rows)
    print(f'Blinded form saved: {output}; {len(rows)} event rows. No Gold/model predictions included.')


def apply_reviews(folder, files, output):
    if folder.resolve() == output.resolve():
        raise ValueError('Write a separate reviewed copy; preserve the candidate dataset')
    if output.exists():
        raise ValueError('Choose a new output directory')
    dataset = load_dataset(folder)
    registered = {(c['case_id'], e['id']) for cases in dataset.values() for c in cases for e in c['events']}
    reviewed, evidence = {}, []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        evidence.append({'filename': path.name, 'sha256': digest})
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                key = row['case_id'], row['event_id']
                if key not in registered:
                    raise ValueError('Unknown review case/event: ' + str(key))
                reviewer = row.get('reviewer_id', '').strip()
                if not reviewer:
                    continue
                target = reviewed.setdefault(key, {})
                if reviewer in target:
                    raise ValueError('Duplicate review by the same reviewer: ' + str(key))
                row['reviewer_id'], row['review_file_sha256'] = reviewer, digest
                target[reviewer] = row
    report = []
    for cases in dataset.values():
        for case in cases:
            approved, reviewer_ids = True, set()
            for event in case['events']:
                key = case['case_id'], event['id']
                rows = list(reviewed.get(key, {}).values())
                label = event['gold']
                usable = [r for r in rows if r.get('independent_review', '').strip().lower() in {'true','yes','1'}
                          and r.get('source_adaptation_valid', '').strip().lower() in {'true','yes','1'}
                          and r.get('control_design_valid', '').strip().lower() in {'true','yes','1'}
                          and r.get('rationale', '').strip()]
                expected = label['decision'], label.get('follow_up') or ''
                predictions = {(r.get('decision','').strip().upper(), r.get('follow_up','').strip().upper()) for r in usable}
                valid = len(usable) >= 2 and predictions == {expected}
                reviewer_ids.update(r['reviewer_id'] for r in usable)
                approved &= valid
                report.append({'case_id': key[0], 'event_id': key[1], 'independent_reviews': len(usable),
                               'matches_proposed_gold': valid, 'status': 'agreed' if valid else 'missing_or_disputed',
                               'reviews': usable})
            case['annotation'].update(human_reviewed=bool(approved), reviewers=sorted(reviewer_ids),
                review_evidence=evidence, status='independently_reviewed' if approved else 'review_incomplete_or_disputed')
    shutil.copytree(folder, output)
    for filename, cases in dataset.items():
        (output / filename).write_text(''.join(json.dumps(c, ensure_ascii=False, sort_keys=True)+'\n' for c in cases), encoding='utf-8')
    review_folder = output / 'review_evidence'
    review_folder.mkdir()
    for index, path in enumerate(files):
        shutil.copy2(path, review_folder / f'{index:02d}_{path.name}')
    write_json(output / 'independent_review_report.json', report)
    manifest = json.loads((output / 'manifest.json').read_text())
    for filename in dataset:
        manifest['files'][filename]['sha256'] = hashlib.sha256((output / filename).read_bytes()).hexdigest()
    manifest.update(human_reviewed=all(c['annotation']['human_reviewed'] for cases in dataset.values() for c in cases),
                    review_evidence=evidence, reviewed_copy_of=str(folder.resolve()))
    write_json(output / 'manifest.json', manifest)
    print(json.dumps({'output':str(output),'human_reviewed':manifest['human_reviewed'],
                      'agreed_event_rows':sum(r['matches_proposed_gold'] for r in report),'registered_event_rows':len(report)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('export','apply'))
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data/online_v2')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reviewer-id', default='')
    parser.add_argument('--review-files', nargs='+', type=Path)
    args = parser.parse_args()
    if args.command == 'export':
        export_form(args.data_dir,args.output,args.reviewer_id)
    else:
        if not args.review_files:
            parser.error('Supply actual independently completed --review-files')
        apply_reviews(args.data_dir,args.review_files,args.output)


if __name__ == '__main__':
    main()
