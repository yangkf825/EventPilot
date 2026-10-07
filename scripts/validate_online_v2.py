#!/usr/bin/env python3
"""Validate source linkage, labels and controlled-variable registration in v2.

Structural validation does not certify independent human review or live website
execution. Formal-release checks deliberately fail on author candidates.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
FILES = {'main': 'single_event.jsonl', 'ablation': 'single_event.jsonl',
         'counterfactual': 'counterfactual.jsonl', 'multi': 'multi_event.jsonl'}
DECISIONS = {'IGNORE', 'DEFER', 'INTERRUPT'}
FOLLOW_UPS = {'HANDLE', 'REPLAN', 'TERMINATE'}
KINDS = {'research_task', 'update_goal', 'cancel', 'none'}
PREDICATES = {'url_host', 'url_contains', 'text_contains', 'text_any', 'control_present',
              'control_value', 'action_occurred', 'task_output_contains', 'url_path_not_in'}


def read_cases(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_trigger(trigger, cid):
    require(isinstance(trigger, dict), f'{cid}: missing semantic trigger')
    require(max(trigger.get('min_non_navigation_actions', 0), trigger.get('min_actor_progress_actions', 0)) >= 1, f'{cid}: homepage-only trigger is forbidden')
    predicates = trigger.get('all', []) + trigger.get('any', [])
    require(predicates and all(p.get('type') in PREDICATES for p in predicates), f'{cid}: unsupported trigger predicate')
    require(any(p.get('type') == 'text_any' and p.get('values') for p in predicates), f'{cid}: missing target-content marker')
    require(any(p.get('type') == 'url_path_not_in' for p in trigger.get('all', [])), f'{cid}: missing non-homepage requirement')
    forbidden = {'menu', 'home', 'search', 'schedule', 'vehicle', 'tickets', 'routes', 'finance', 'stats'}
    for p in predicates:
        if p.get('type') == 'text_any':
            require(not any(str(v).strip().lower() in forbidden for v in p['values']), f'{cid}: generic navigation marker')


def validate(folder, strict_review=False, strict_readiness=False):
    folder = Path(folder)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    source_path = ROOT / 'data/normalized/tasks.jsonl'
    if not source_path.exists():
        source_path = folder / 'source_task_index.jsonl'
        require(hashlib.sha256(source_path.read_bytes()).hexdigest() == manifest['source_task_index']['sha256'],
                'Portable original-task metadata index changed')
    original = {r['task_id']: r for r in read_cases(source_path)}
    evidence_hashes = {hashlib.sha256(p.read_bytes()).hexdigest() for p in (folder / 'review_evidence').glob('*') if p.is_file()} if strict_review else set()
    all_cases, file_rows, ids = [], {}, set()
    for filename in sorted(set(FILES.values())):
        path = folder / filename
        require(hashlib.sha256(path.read_bytes()).hexdigest() == manifest['files'][filename]['sha256'], 'Hash mismatch: ' + filename)
        cases = read_cases(path)
        file_rows[filename] = cases
        for c in cases:
            cid = c['case_id']
            require(cid not in ids, 'Duplicate case ID: ' + cid); ids.add(cid)
            all_cases.append(c)
            require(urlsplit(c['url']).scheme in ('http', 'https'), cid + ': invalid public URL')
            require(c.get('task_family_id') == c.get('source_family_id'), cid + ': inconsistent merged family ID')
            require(c.get('author_profile_id') and c.get('rules'), cid + ': missing profile/policy')
            if manifest.get('schema_version','').startswith('online_v2.0.4'):
                from eventarena.browser import validate_checkpoint_policy
                policy=validate_checkpoint_policy(c.get('checkpoint_audit_policy'))
                require(policy.get('equality')=='full_rendered_task_text_and_controls_preserve_values',cid+': weak checkpoint equality')
                require(policy.get('missing_or_unstable_root')=='unknown_do_not_fallback_to_body',cid+': unsafe checkpoint fallback')
                require(c.get('evaluation_registration',{}).get('eligibility_must_not_use_actor_scores') is True,cid+': score-based eligibility forbidden')
            require(c.get('annotation', {}).get('full_trajectory_origin') == 'runtime_browser_observations_and_actions_only', cid + ': Full Trajectory must be actual runtime history')
            require(not c.get('trajectory') and not c.get('prefix_actions'), cid + ': authored trajectory/prefix is forbidden')
            require(not c.get('cf', {}).get('state_override'), cid + ': author-completed state override is forbidden')
            if strict_review:
                reviewers = c['annotation'].get('reviewers', [])
                require(c['annotation'].get('human_reviewed') is True and len(set(reviewers)) >= 2
                        and c['annotation'].get('review_evidence'), cid + ': two independent human reviews and their evidence are incomplete')
                require(all(e.get('sha256') in evidence_hashes for e in c['annotation']['review_evidence']),
                        cid + ': independent review evidence is missing or changed')
            if strict_readiness:
                require(c.get('website_validity', {}).get('execution_verified') is True, cid + ': A/B execution and trigger verification is incomplete')
            for name in ('source_task_a', 'source_task_b','source_task_c'):
                src = c.get(name, {})
                require(src.get('task_id') in original, cid + ': missing original ' + name)
                require(src.get('original_goal') == original[src['task_id']]['goal'], cid + ': original source goal altered')
                require(src.get('source_split') == original[src['task_id']]['split'], cid + ': original source split altered')
                require(src.get('same_as_original_interactive_task') is False, cid + ': public adaptation cannot be called original task execution')
            validate_trigger(c['trigger'], cid)
            events = {e['id']: e for e in c['events']}
            require(len(events) == len(c['events']) and events, cid + ': duplicate/missing events')
            for e in events.values():
                label = e['gold']; kind = e['execution']['kind']
                require(label.get('decision') in DECISIONS, cid + ': invalid decision')
                require((label.get('follow_up') in FOLLOW_UPS) if label['decision'] == 'INTERRUPT' else label.get('follow_up') is None, cid + ': invalid conditional follow-up')
                require(kind in KINDS, cid + ': unsupported event execution')
                if kind == 'research_task':
                    require(e['execution'].get('source_task_id') in original, cid + ': real event task source missing')
                    require(e['execution'].get('goal') and urlsplit(e['execution'].get('url', '')).scheme in ('https', 'http'), cid + ': unexecutable event lookup')
                    src = e.get('source_task') or {}
                    require(src.get('task_id') == e['execution']['source_task_id'] and
                            src.get('original_goal') == original[e['execution']['source_task_id']]['goal'] and
                            src.get('adapted_goal') == e['execution']['goal'], cid + ': actual event source provenance mismatch')
                if kind == 'update_goal':
                    require('CURRENT_TASK_UPDATED' in c['verification'] and e['execution'].get('updated_verification'), cid + ': revision result verifier missing')
                    require(c['verification']['CURRENT_TASK']['goal'] == c.get('task_content_goal',c['goal']), cid + ': initial verifier overwritten by revision')
                    constraints = e['execution']['updated_verification'].get('effective_constraints', [])
                    require(len(constraints) >= 2 and {v.get('origin') for v in constraints} >= {'new','retained'},
                            cid + ': independently verifiable new/retained constraints missing')
                if label['decision'] != 'IGNORE':
                    require(e['id'] in c['verification'], cid + ': required event verifier missing')
                require(not any(token in e['text'].upper() for token in ('LABEL: IGNORE', 'GOLD:', 'NO ACTION REQUIRED', 'JUST A NOTIFICATION')), cid + ': answer hint in public text')
                require(not e.get('trigger') or isinstance(e['trigger'], dict), cid + ': malformed event trigger')
                require(set(e.get('delivery_after', [])) <= set(events), cid + ': unknown delivery dependency')
                require(e['id'] not in e.get('delivery_after',[]),cid+': self-dependent delivery')
            for setup in c.get('setup_tasks', []):
                require(setup.get('require_verified_success') is True and setup.get('verification'), cid + ': unverified fabricated setup')
                require(setup.get('source_task_id') in original, cid + ': setup source missing')
                require(setup.get('alias_event_id') in events, cid + ': setup alias missing')
            gold = c['gold']
            if len(events) == 1:
                require(all(gold.get(k) == next(iter(events.values()))['gold'].get(k) for k in ('decision', 'follow_up')), cid + ': case/event Gold disagreement')
            else:
                require(gold.get('decisions') == {eid: e['gold'] for eid, e in events.items()}, cid + ': multi Gold disagreement')
                nodes = set(gold.get('required_nodes', [])); edges = gold.get('precedence', [])
                require(nodes <= (set(events) | {'CURRENT_TASK', 'TERMINATE_TASK'}), cid + ': unknown schedule node')
                require(all(a in nodes and b in nodes and a != b for a, b in edges), cid + ': bad dependency edge')
                pending = set(nodes)
                while pending:
                    ready = {n for n in pending if not any(b == n and a in pending for a, b in edges)}
                    require(bool(ready), cid + ': cyclic dependency graph')
                    pending -= ready
                for schedule in gold.get('valid_schedules', []):
                    require(set(schedule) == nodes and len(schedule) == len(nodes), cid + ': incomplete valid schedule')
                    require(all(schedule.index(a) < schedule.index(b) for a, b in edges), cid + ': invalid registered schedule')
                delivered={eid for eid,e in events.items() if not e.get('delivery_after')}
                while True:
                    new={eid for eid,e in events.items() if set(e.get('delivery_after',[]))<=delivered}
                    if new<=delivered:break
                    delivered|=new
                require(delivered==set(events),cid+': delivery dependency deadlock')
    single = file_rows['single_event.jsonl']
    require(len(single) == 100, 'Main must contain 100 registered cases')
    labels = dict(Counter(c['gold']['decision'] for c in single))
    follow = dict(Counter(c['gold']['follow_up'] for c in single if c['gold']['decision'] == 'INTERRUPT'))
    difficulty = dict(Counter(c['difficulty'] for c in single))
    require(labels == {'IGNORE': 30, 'DEFER': 30, 'INTERRUPT': 40}, 'Main label quota mismatch')
    require(follow == {'HANDLE': 20, 'REPLAN': 10, 'TERMINATE': 10}, 'Follow-up quota mismatch')
    require(difficulty == {'easy': 30, 'medium': 50, 'hard': 20}, 'Difficulty quota mismatch')
    for tier in ('easy', 'medium', 'hard'):
        require({c['gold']['decision'] for c in single if c['difficulty'] == tier} == DECISIONS, 'Difficulty tier missing class')
    require(len(file_rows['counterfactual.jsonl']) == 70 and len(file_rows['multi_event.jsonl']) == 20, 'CF/Multi case quota mismatch')
    shortcuts = {'public_event_id': defaultdict(Counter), 'stored_position': defaultdict(Counter),
                 'source': defaultdict(Counter)}
    for c in file_rows['multi_event.jsonl']:
        for position,e in enumerate(c['events']):
            label=e['gold']['decision']
            shortcuts['public_event_id'][e['id']][label]+=1
            shortcuts['stored_position'][str(position)][label]+=1
            shortcuts['source'][e['source']][label]+=1
        if manifest.get('schema_version','').startswith('online_v2.0.4'):
            registration=c.get('identifier_registration',{})
            require(registration.get('label_independent') is True,c['case_id']+': identifier assignment depends on labels')
            require(set(registration.get('author_to_public_id',{}).values())=={e['id'] for e in c['events']},c['case_id']+': identifier map is not a bijection')
    if manifest.get('schema_version','').startswith('online_v2.0.4'):
        for name,table in shortcuts.items():
            require(all(len(counts)>1 for counts in table.values()),f'Multi-event {name} perfectly reveals a class')
    groups = defaultdict(list)
    for c in file_rows['counterfactual.jsonl']:
        groups[(c['group_type'], c['group_id'])].append(c)
        require(c['group_type'] == c['cf_type'] and c['group_id'] == c['cf_group_id'], c['case_id'] + ': CF aliases differ')
    for (kind, gid), members in groups.items():
        require(len(members) == (3 if kind == 'semantic' else 2), 'Incomplete CF group ' + gid)
        a = members[0]
        for b in members[1:]:
            require(a['rules'] == b['rules'] and a['url'] == b['url'] and a['trigger'] == b['trigger'], 'Uncontrolled rules/entry/trigger in ' + gid)
            if kind != 'goal':
                require(a['goal'] == b['goal'], 'Uncontrolled goal in ' + gid)
            if kind != 'semantic':
                require([(e['source'], e['text']) for e in a['events']] == [(e['source'], e['text']) for e in b['events']], 'Uncontrolled event text in ' + gid)
            else:
                require(a['gold'] == b['gold'] and a['events'][0]['execution'] == b['events'][0]['execution'], 'Semantic meaning/Gold changed in ' + gid)
        if kind in ('goal', 'state'):
            require(len({m['gold']['decision'] for m in members}) == 2, 'CF does not contrast Gold ' + gid)
        if kind == 'state':
            require(sorted(bool(m['setup_tasks']) for m in members) == [False, True], 'State comparison lacks genuine setup contrast ' + gid)
    require(dict(Counter(kind for kind, _ in groups)) == {'goal': 10, 'state': 10, 'semantic': 10}, 'CF group quota mismatch')
    semantic_labels = Counter(members[0]['gold']['decision'] for (kind, _), members in groups.items() if kind == 'semantic')
    require(dict(semantic_labels) == {'IGNORE': 3, 'DEFER': 3, 'INTERRUPT': 4}, 'Semantic groups must not be one-class constant-label test')
    # No reused source IDs across development/heldout, including C tasks hidden
    # by a particular episode but registered in its source profile.
    profile_data = json.loads((folder / 'source_profiles.json').read_text())
    profile_cluster = json.loads((folder / 'split_manifest.json').read_text())['profile_cluster']
    source_clusters = defaultdict(set)
    for i, p in enumerate(profile_data, 1):
        for key in ('a', 'b', 'c'):
            source_clusters[p[key]].add(profile_cluster[f'P{i:02d}'])
    require(all(len(v) == 1 for v in source_clusters.values()), 'Shared source task crosses cluster groups')
    family_splits = defaultdict(set)
    for c in all_cases:
        family_splits[c['task_family_id']].add(c['split'])
    require(all(len(v) == 1 for v in family_splits.values()), 'Family leaks across development/heldout')
    return {'schema_valid': True, 'counts': {f: len(r) for f, r in file_rows.items()},
            'main_labels': labels, 'follow_up_labels': follow, 'difficulty': difficulty,
            'counterfactual_groups': dict(Counter(kind for kind, _ in groups)),
            'semantic_group_labels': dict(semantic_labels), 'multi_event_count': sum(len(c['events']) for c in file_rows['multi_event.jsonl']),
            'author_profile_count': len(profile_data), 'source_family_count': len(family_splits),
            'unique_source_tasks': len(source_clusters),
            'multi_shortcut_contingency': {name:{key:dict(counts) for key,counts in table.items()} for name,table in shortcuts.items()},
            'human_reviewed': all(c['annotation'].get('human_reviewed') is True for c in all_cases),
            'all_execution_verified': all(c.get('website_validity', {}).get('execution_verified') is True for c in all_cases),
            'gold_status': 'independently_reviewed' if all(c['annotation'].get('human_reviewed') is True for c in all_cases) else 'author_candidate_pending_independent_review',
            'validation_scope': 'structure/source_linkage/registered_controls; not factual website execution certification'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', type=Path, default=ROOT / 'data/online_v2')
    p.add_argument('--strict-review', action='store_true')
    p.add_argument('--strict-readiness', action='store_true')
    a = p.parse_args()
    print(json.dumps(validate(a.data_dir, a.strict_review, a.strict_readiness), ensure_ascii=False, indent=2))
