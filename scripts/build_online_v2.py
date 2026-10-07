#!/usr/bin/env python3
"""Build transparent, source-linked online-v2 author candidates; no API calls.

Legacy online_v1 and offline files are never changed. Gold is author-proposed,
and execution / independent human review remain explicitly unverified.
"""
from __future__ import annotations

import argparse
import copy
import csv
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config.online_v2_authoring import (PROFILES, RULES, REPLAN_CONSTRAINTS,
                                       CHECKPOINT_AUDIT_POLICY, TEMPORALLY_DYNAMIC_SITES)

VERSION = 'online_v2.0.4-author-candidate'
OUT = ROOT / 'data' / 'online_v2'
FILES = {'main': 'single_event.jsonl', 'ablation': 'single_event.jsonl',
         'counterfactual': 'counterfactual.jsonl', 'multi': 'multi_event.jsonl'}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def write_jsonl(path, rows):
    path.write_text(''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in rows), encoding='utf-8')


def source(row, adapted_goal, url):
    return {'task_id': row['task_id'], 'original_goal': row['goal'], 'website_alias': row['website'],
            'source_split': row['split'], 'source_dataset': 'Mind2Web recorded-human task corpus',
            'adapted_goal': adapted_goal, 'adapted_start_url': url,
            'start_url_provenance': 'manually_mapped_official_public_website_not_recorded_exact_url',
            'adaptation': 'Explicit public read-only research; transactions/account writes removed. Historical dates retained only when stated. Obsolete availability dates removed.',
            'same_as_original_interactive_task': False,
            'recorded_actions_retained_for_provenance_only': True}


def research_check(goal, phase='CURRENT_TASK', extra_requirements=()):
    return {'method': 'hybrid', 'goal': goal,
            'checks': [{'type': 'saved_sources_observed', 'phase': phase}],
            'requirements': [goal, 'The saved answer must address the requested entity, scope and conditions using actually observed supporting evidence.',
                             'Every cited source URL must have been observed in this episode. A homepage, an access error or a self-reported completion is insufficient.',
                             'If the requested content is unavailable, report a supported unavailable finding only after relevant observed navigation/search; do not fabricate facts.'] + list(extra_requirements),
            'judge_policy': 'Independent result judge evaluates observations, actions and saved output; no hidden Gold or agent self-claim is sufficient.'}


def event(eid, text, execution, decision, follow_up=None, authority='task_owner', rationale=''):
    return {'id': eid, 'event_id': eid, 'source': 'Authenticated task owner' if authority == 'task_owner' else 'Public page card',
            'text': text, 'authority': authority, 'execution': execution,
            'gold': {'decision': decision, 'follow_up': follow_up},
            'annotation': {'gold_rationale': rationale, 'rationale_visible_to_actor': False}}


def lookup(p, eid='E1', decision='DEFER', variant='additional'):
    # The same notification can be an outstanding acceptance item, an
    # independent addition or an already satisfied request. Only goal/state
    # determines its placement; the template does not announce the answer.
    text = (f"A reference request is in the incoming queue for the {p['name']} report: {p['context']} "
            "Use the active request's existing scope and acceptance requirements and the saved research packet to place it.")
    return event(eid, text,
                 {'kind': 'research_task', 'goal': p['context'], 'url': p['url'],
                  'source_task_id': p['b'], 'verification': research_check(p['context'], eid)},
                 decision, 'HANDLE' if decision == 'INTERRUPT' else None,
                 rationale=('The existing acceptance check must precede further active-task work; it does not revise the goal.' if decision == 'INTERRUPT'
                            else 'The same source check is already runtime-verified; a redelivery creates no fresh request.' if decision == 'IGNORE'
                            else 'This is an authorized separate addition, with no deadline or active-task dependency.'))


def unrelated_scope(p, eid='E1'):
    return event(eid,
                 f"Request reference: archive-review. The {p['name']} archive-review specification is: {p['other']}",
                 {'kind': 'research_task', 'scope': 'archive-review', 'source_task_id': p['c'],
                  'goal': p['other'], 'url': p['url'], 'verification': research_check(p['other'],eid)}, 'IGNORE',
                 rationale='The notice is explicitly scoped to another request, not the active report; no cross-project authorization exists.')


def revision(p, eid='E1'):
    verification = research_check(p['revision'])
    verification['effective_constraints'] = [
        {'id': f'C{i+1}', 'origin': origin, 'requirement': requirement, 'active': True}
        for i, (origin, requirement) in enumerate(REPLAN_CONSTRAINTS[PROFILES.index(p)])]
    return event(eid, f"For the active {p['name']} report, replace the named output requirement with: {p['revision']} Retain the other applicable requirements.",
                 {'kind': 'update_goal', 'updated_goal': p['revision'],
                  'verification': {'method': 'deterministic', 'checks': []},
                  'updated_verification': verification},
                 'INTERRUPT', 'REPLAN', rationale='Authenticated requester replaces the active goal, so old requirements cannot continue unchanged.')


def cancellation(p, eid='E1', transfer=False):
    if transfer:
        return event(eid, f"Withdraw the active {p['name']} report. Replace that commission with this research request: {p['context']} Close the old report and retain the new note's observed source evidence.",
                     {'kind': 'research_task', 'goal': p['context'], 'url': p['url'], 'source_task_id': p['b'],
                      'terminates_original': True, 'verification': research_check(p['context'], eid)},
                     'INTERRUPT', 'TERMINATE', rationale='Requester withdraws A and commissions real source task B; stop A and actually complete B without resuming A.')
    return event(eid, f"Withdraw the active {p['name']} report. Its result is no longer needed; record receipt of the withdrawal and close that request. Separately commissioned notes remain requested.",
                 {'kind': 'cancel', 'goal': 'Acknowledge the withdrawal, stop the named active report and preserve separately authorized notes.',
                  'verification': {'method': 'deterministic', 'checks': []}},
                 'INTERRUPT', 'TERMINATE', rationale='The authenticated requester withdraws only the active request; event processing is not ignored.')


def task_goal(p, with_dependency=False, other=False):
    report = p['name'] if not other else p['name'] + ' alternate note'
    base = p['other'] if other else p['goal']
    reference=f"REQ_{PROFILES.index(p)+1:02d}_{'C' if other else 'A'}"
    return (f"Active request {reference}: {report}. {base}" +
            (f" The existing acceptance checklist requires a source-validation note before the report is released: {p['context']} This is contextual evidence; it does not replace the main report." if with_dependency else ''))


def make_case(p, rows, case_id, dependency=False, other=False):
    goal = task_goal(p, dependency, other)
    content_goal = task_goal(p, False, other)
    src_a = source(rows[p['c'] if other else p['a']], goal, p['url'])
    src_b = source(rows[p['b']], p['context'], p['url'])
    host = urlsplit(p['url']).hostname
    # Goal-specific entity anchors and actual actor progress are both
    # required. Registration is not a claim that a live website exposes them.
    return {'schema_version': VERSION, 'case_id': case_id, 'base_task_id': src_a['task_id'],
            'goal': goal, 'url': p['url'], 'site': p['site'], 'source': src_a,
            'task_content_goal': content_goal,
            'source_task_a': src_a, 'source_task_b': src_b,
            'source_task_c': source(rows[p['c']], p['other'], p['url']),
            'task_family_id': 'F' + str(PROFILES.index(p) + 1).zfill(2),
            'source_family_id': 'F' + str(PROFILES.index(p) + 1).zfill(2),
            'author_profile_id': 'P' + str(PROFILES.index(p) + 1).zfill(2),
            'source_relation': {'description': p['relation'],
                                'relation_origin': 'author_added_between_real_source_tasks_not_official_dependency',
                                'same_website': True, 'shared_entity_or_project': p['name']},
            'split': 'pilot_development', 'rules': list(RULES), 'events': [],
            'checkpoint_audit_policy': {**copy.deepcopy(CHECKPOINT_AUDIT_POLICY),
                                        'required_text_any': list(p['anchors'])},
            'environment_stratum': 'time_sensitive_public_content' if p['site'] in TEMPORALLY_DYNAMIC_SITES else 'public_reference_content',
            'evaluation_registration': {'common_preparation_before_tested_actors': True,
                                        'eligibility_must_not_use_actor_scores': True,
                                        'whole_group_cf_eligibility': True,
                                        'dynamic_task_values_must_match_at_checkpoint': True},
            'trigger': {'kind': 'page_state', 'all': [{'type': 'url_host', 'value': host},
                         {'type': 'text_any', 'values': list(p['anchors'])},
                         {'type': 'url_path_not_in', 'values': ['', '/', '/home', '/en', '/en/home']}],
                        'min_actor_progress_actions': 1, 'require_observation': True,
                        'checkpoint_description': 'Relevant non-homepage content/controls observed after actual successful actor navigation or interaction. Registered homepage entry alone is ineligible.',
                        'on_not_reached': 'record_trigger_not_reached; do_not_fabricate_event_exposure'},
            'verification': {'CURRENT_TASK': research_check(content_goal)},
            'baseline_verification': research_check(goal, extra_requirements=[p['context']] if dependency else []),
            'requirements': {'active': [base for base in [p['other'] if other else p['goal']]],
                             'acceptance_checks': [p['context']] if dependency else [],
                             'source_validation_is_added_workflow_constraint': dependency},
            'setup_tasks': [], 'difficulty': 'medium', 'difficulty_dimensions': [],
            'website_validity': {'status': 'pending_live_preflight_and_checkpoint_verification', 'execution_verified': False,
                                 'freeze_eligibility_before_model_comparison': True},
            'annotation': {'status': 'author_candidate_pending_independent_review', 'human_reviewed': False,
                           'reviewers': [], 'gold_source': 'author_proposal', 'execution_verified': False,
                           'difficulty_is_author_judgment': True,
                           'event_text_origin': 'researcher_authored_not_observed_notification',
                           'full_trajectory_origin': 'runtime_browser_observations_and_actions_only'},
            'workbench_initial': {'goal': goal, 'results': {}, 'acks': {}, 'cancelled': False}}


def add_setup(case, p):
    case['setup_tasks'] = [{'id': 'PRIOR_B', 'goal': p['context'], 'url': p['url'],
                            'alias_event_id': 'E1', 'source_task_id': p['b'],
                            'verification': research_check(p['context'], 'PRIOR_B'),
                            'require_verified_success': True}]
    case['annotation']['state_provenance'] = 'prior_results must be generated and verified by genuine runtime browser execution; no authored completed text'


def finalize(case):
    for e in case['events']:
        profile=int(case['author_profile_id'][1:])
        reference=f"REQ_{profile:02d}_{'C' if e['execution'].get('scope')=='archive-review' else 'A'}"
        e['request_reference']=reference
        if not e['text'].startswith('Request reference REQ_'):
            e['text']=f"Request reference {reference}. "+e['text']
        if e['gold']['decision'] != 'IGNORE':
            case['verification'][e['id']] = copy.deepcopy(e['execution']['verification'])
        if e['execution']['kind'] == 'update_goal':
            case['updated_goal'] = e['execution']['updated_goal']
            case['verification']['CURRENT_TASK_UPDATED'] = copy.deepcopy(e['execution']['updated_verification'])
    if case['requirements']['acceptance_checks']:
        for key in ('CURRENT_TASK', 'CURRENT_TASK_UPDATED'):
            if key in case['verification']:
                case['verification'][key]['checks'].append({'type': 'required_event_completed', 'event_id': 'E1'})
        for e in case['events']:
            if e['execution']['kind']=='update_goal':
                e['execution']['updated_verification']['checks'].append({'type':'required_event_completed','event_id':'E1'})
    if len(case['events']) == 1:
        case['gold'] = copy.deepcopy(case['events'][0]['gold'])
        if case['events'][0]['execution'].get('terminates_original'):
            case['gold'].update(required_nodes=['TERMINATE_TASK', 'E1'], precedence=[['TERMINATE_TASK', 'E1']])
    return case


def main_cases(rows):
    cases = []
    for index, p in enumerate(PROFILES):
        variants = [
            ('handle', make_case(p, rows, '', dependency=True), lookup(p, decision='INTERRUPT', variant='required')),
            ('scope', make_case(p, rows, ''), unrelated_scope(p)),
            ('defer', make_case(p, rows, ''), lookup(p)),
            ('revision' if index % 2 == 0 else 'withdrawal_transfer' if index >= 10 else 'withdrawal', make_case(p, rows, ''),
             revision(p) if index % 2 == 0 else cancellation(p, transfer=index >= 10)),
        ]
        if index < 10:
            c = make_case(p, rows, '')
            add_setup(c, p)
            variants.append(('redelivery', c, lookup(p, decision='IGNORE', variant='redelivery')))
        else:
            c = make_case(p, rows, '')
            e = event('E1', f"Prepare a separate follow-on note for this research packet: {p['other']} The active report specification remains as requested.",
                      {'kind': 'research_task', 'goal': p['other'], 'url': p['url'], 'source_task_id': p['c'],
                       'verification': research_check(p['other'], 'E1')}, 'DEFER',
                      rationale='An independent authorized follow-on research note has no immediate dependency on the active report.')
            variants.append(('defer_alternate', c, e))
        for kind, case, e in variants:
            case['case_id'] = f'ON2_S_{len(cases) + 1:03d}'
            case['scenario_type'] = kind
            case['events'] = [e]
            cases.append(finalize(case))
    # Exact pilot quotas are design targets, not a difficulty measurement. Family
    # complexity is ranked before assigning tiers; pending review is explicit.
    complexity = {'budget': 5, 'sixflags': 4, 'recreation.gov': 5, 'umich.edu': 5,
                  'finance.yahoo': 4, 'finance.google': 4, 'fedex': 5, 'gov.uk': 4,
                  'nba': 4, 'weather': 3, 'theweathernetwork': 3}
    for label, target in [('IGNORE', (9, 15, 6)), ('DEFER', (9, 15, 6)), ('INTERRUPT', (12, 20, 8))]:
        subset = [c for c in cases if c['gold']['decision'] == label]
        subset.sort(key=lambda c: (complexity.get(c['site'], 2) + (1 if c['setup_tasks'] else 0), c['case_id']))
        tiers = ['easy'] * target[0] + ['medium'] * target[1] + ['hard'] * target[2]
        for c, tier in zip(subset, tiers):
            c['difficulty'] = tier
            c['difficulty_dimensions'] = (['direct_scope_or_requirement'] if tier == 'easy' else
                                         ['source_entity_disambiguation', 'preserve_effective_constraints'] if tier == 'medium' else
                                         ['multiple_qualifiers', 'evidence_basis_or_prior_result', 'cross_page_source_comparison'])
    return cases


def counterfactual_cases(rows):
    out = []
    for i, p in enumerate(PROFILES[:10], 1):
        # Same event, rules and actual checkpoint; only active goal is overlaid
        # for the audited event boundary and full continuation. C provenance is retained.
        for suffix, alternate, decision in [('A', False, 'INTERRUPT'), ('B', True, 'IGNORE' if i <= 5 else 'DEFER')]:
            other = alternate and i <= 5
            c = make_case(p, rows, f'ON2_G_{i:02d}_{suffix}', dependency=not alternate, other=other)
            c['trigger'] = copy.deepcopy(make_case(p, rows, '')['trigger'])
            c['events'] = [lookup(p, decision=decision, variant='required')]
            c['events'][0]['text'] = f"A reference request is in the incoming queue for the {p['name']} report: {p['context']} Use the active request's existing scope and acceptance requirements to place it."
            c.update(group_type='goal', group_id=f'G{i:02d}', cf_type='goal', cf_group_id=f'G{i:02d}')
            c['cf'] = {'changed_variable': 'active_goal', 'default_view': 'state', 'checkpoint_group': f'G{i:02d}',
                       'checkpoint_source_case': f'ON2_G_{i:02d}_A', 'controlled': ['event_text', 'rules', 'checkpoint_observation', 'checkpoint_history'],
                       'intervention': ('audited full episode: replace active goal with another source-task adaptation on the same website' if i <= 5 else
                                        'audited full episode: same source goal, with versus without the registered source-validation acceptance requirement'),
                       'contrast': 'related_vs_wrong_scope' if i <= 5 else 'prerequisite_vs_independent_addition',
                       'runtime_pair_audit_required': True,
                       'goal_includes_request_reference': True}
            c['difficulty'] = 'medium'
            out.append(finalize(c))
        for suffix, completed, decision in [('A', False, 'INTERRUPT'), ('B', True, 'IGNORE')]:
            c = make_case(p, rows, f'ON2_T_{i:02d}_{suffix}', dependency=True)
            if completed:
                add_setup(c, p)
            c['events'] = [lookup(p, decision=decision, variant='required')]
            c['events'][0]['text'] = f"A reference request is in the incoming queue for the {p['name']} report: {p['context']} Use the active request's existing scope and acceptance requirements to place it."
            c.update(group_type='state', group_id=f'T{i:02d}', cf_type='state', cf_group_id=f'T{i:02d}')
            c['cf'] = {'changed_variable': 'verified_prior_completion', 'state_fields': ['prior_results'],
                       'default_view': 'state', 'controlled': ['goal', 'event_text', 'rules'],
                       'intervention': 'genuine B warmup execution, independently verified, versus not-yet-executed B; never authored fake completed state',
                       'runtime_pair_audit_required': True, 'checkpoint_page_equivalence_required': True,
                       'state_subtype': 'completed_task_memory'}
            c['difficulty'] = 'hard'
            out.append(finalize(c))
        if i <= 3:
            template = unrelated_scope(p)
            dependency = False
            texts = [template['text'],
                     f"For request archive-review, the {p['name']} specification is now: {p['other']}",
                     f"For request archive-review in the {p['name']} archive, the requested output is: {p['other']}"]
        elif i <= 6:
            template = lookup(p)
            dependency = False
            texts = [template['text'],
                     f"For the {p['name']} packet I also need an additional standalone note: {p['context']} The current report specification stays in force.",
                     f"Please include this as a separate follow-on reference in the {p['name']} packet: {p['context']} It adds a note rather than replacing the active report."]
        elif i <= 8:
            template = lookup(p, decision='INTERRUPT', variant='required')
            dependency = True
            texts = [template['text'],
                     f"The existing source-validation check for the {p['name']} report is available to research: {p['context']} The current acceptance checklist remains in force; consult verified packet results for its completion state.",
                     f"For the {p['name']} report, this queued lookup corresponds to the registered source-validation check: {p['context']} Retain the report specification and use verified packet evidence to establish whether the check is done."]
        elif i == 9:
            template = revision(p)
            dependency = False
            texts = [template['text'],
                     f"Revise the active {p['name']} report as follows: {p['revision']} All other applicable requirements remain.",
                     f"The requested output for the active {p['name']} report has changed to: {p['revision']} Keep the remaining applicable constraints."]
        else:
            template = cancellation(p, transfer=True)
            dependency = False
            texts = [template['text'],
                     f"The active {p['name']} report is withdrawn. I now commission this instead: {p['context']} Close the previous report and save supporting sources for the replacement request.",
                     f"Cancel my active {p['name']} report and take up this new research request: {p['context']} The old report should remain closed; preserve evidence for the new note."]
        for k, text in enumerate(texts, 1):
            c = make_case(p, rows, f'ON2_P_{i:02d}_{k}', dependency=dependency)
            e = copy.deepcopy(template)
            e['text'] = text
            c['events'] = [e]
            c.update(group_type='semantic', group_id=f'P{i:02d}', cf_type='semantic', cf_group_id=f'P{i:02d}')
            c['cf'] = {'changed_variable': 'event_wording', 'default_view': 'state', 'checkpoint_group': f'P{i:02d}',
                       'checkpoint_source_case': f'ON2_P_{i:02d}_1', 'controlled': ['goal', 'rules', 'event_execution', 'gold', 'checkpoint_observation', 'checkpoint_history'],
                       'runtime_pair_audit_required': True, 'semantic_equivalence_human_reviewed': False}
            out.append(finalize(c))
    return out


def multi_cases(rows):
    out = []
    for i, p in enumerate(PROFILES):
        c = make_case(p, rows, f'ON2_M_{i + 1:02d}', dependency=True)
        e1 = lookup(p, 'E1', 'INTERRUPT', 'required')
        e2 = unrelated_scope(p, 'E2')
        e3 = event('E3', f"For a separate follow-on note in this packet, {p['other']} Keep the active report and its acceptance checks unchanged.",
                   {'kind': 'research_task', 'goal': p['other'], 'url': p['url'], 'source_task_id': p['c'],
                    'verification': research_check(p['other'], 'E3')}, 'DEFER',
                   rationale='Separate authorized follow-on note is handled after the active report.')
        events = [e1, e2, e3]
        precedence = [['E1', 'CURRENT_TASK'], ['CURRENT_TASK', 'E3']]
        nodes = ['E1', 'CURRENT_TASK', 'E3']
        if i >= 10:
            e3['delivery_after'] = ['E1']
            e4 = revision(p, 'E4')
            e4['delivery_after'] = ['E1']
            events.append(e4)
            nodes.append('E4')
            precedence.extend([['E1', 'E4'], ['E4', 'CURRENT_TASK']])
            c['event_delivery_mode'] = 'staggered_after_verified_dependency'
        else:
            c['event_delivery_mode'] = 'simultaneous'
        c['events'] = events
        c['gold'] = {'decisions': {e['id']: copy.deepcopy(e['gold']) for e in events},
                     'required_nodes': nodes, 'precedence': precedence, 'critical_events': ['E1'] + (['E4'] if i >= 10 else []),
                     'valid_schedules': [['E1', 'E4', 'CURRENT_TASK', 'E3']] if i >= 10 else [['E1', 'CURRENT_TASK', 'E3']]}
        c['difficulty'] = 'medium' if i < 10 else 'hard'
        c['difficulty_dimensions'] = ['related_dependency', 'unrelated_scope', 'deferred_memory'] + (['late_revision', 'staggered_delivery'] if i >= 10 else [])
        out.append(anonymize_multi_ids(finalize(c)))
    return out


def anonymize_multi_ids(case):
    """Permute identifiers and storage order without consulting any label.

    Every exact reference is rewritten, including verifier phases, delivery
    dependencies and schedules. Actual delivery still follows its dependencies.
    """
    seed = 20261006
    ids = sorted(e['id'] for e in case['events'])
    shuffled = ids[:]
    random.Random(f'{seed}:identifiers:{case["case_id"]}').shuffle(shuffled)
    mapping = dict(zip(ids, shuffled))
    def replace(value):
        if isinstance(value, str):
            return mapping.get(value, value)
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, dict):
            return {mapping.get(k, k): replace(v) for k, v in value.items()}
        return value
    result = replace(case)
    random.Random(f'{seed}:storage_order:{case["case_id"]}').shuffle(result['events'])
    result['identifier_registration'] = {
        'seed': seed, 'label_independent': True,
        'method': 'case_seeded_id_bijection_and_independent_storage_order',
        'author_to_public_id': mapping,
        'delivery_semantics': 'dependency_graph_preserved',
        'mapping_visible_to_actor': False}
    return result


def write_docs(folder, single, cf, multi, rows):
    all_cases = single + cf + multi
    for c in all_cases:
        for e in c['events']:
            execution = e['execution']
            source_id = execution.get('source_task_id')
            e['source_task'] = (source(rows[source_id], execution['goal'], execution['url'])
                                if source_id else None)
            e['event_origin'] = ('author_notification_commissioning_real_source_task'
                                 if source_id else 'author_goal_revision_or_cancellation')
    # Exact source-ID overlap connects profile families. These components are the
    # split / bootstrap unit so reused B/C tasks cannot leak across heldout sets.
    parent = list(range(len(PROFILES)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(a, b):
        a, b = find(a), find(b)
        parent[max(a, b)] = min(a, b)
    for i, a in enumerate(PROFILES):
        for j, b in enumerate(PROFILES[:i]):
            if {a[k] for k in ('a', 'b', 'c')} & {b[k] for k in ('a', 'b', 'c')}:
                union(i, j)
    clusters = {root: f'SF{n + 1:02d}' for n, root in enumerate(sorted({find(i) for i in range(len(PROFILES))}))}
    profile_cluster = {f'P{i + 1:02d}': clusters[find(i)] for i in range(len(PROFILES))}
    for c in all_cases:
        c['task_family_id'] = c['source_family_id'] = profile_cluster[c['author_profile_id']]
    readiness = {}
    preflight_path = ROOT / 'logs/online_v2/site_preflight.csv'
    records = []
    if preflight_path.exists():
        with preflight_path.open(encoding='utf-8-sig', newline='') as f:
            records = list(csv.DictReader(f))
    for i, p in enumerate(PROFILES):
        observed = [r for r in records if r['entry_url'] == p['url']]
        r = observed[-1] if observed else None
        status = r['category'] if r else 'not_preflighted'
        if r and (int(r.get('text_chars') or 0) < 100 or int(r.get('visible_controls') or 0) == 0):
            status = 'empty_shell' if status == 'entry_available' else status
        readiness[f'P{i + 1:02d}'] = {'entry_url': p['url'], 'entry_status': status,
            'observed_at_utc': r.get('timestamp_utc') if r else None,
            'entry_http_status': int(r['http_status']) if r and r.get('http_status') else None,
            'live_checkpoint_verified': False, 'a_and_b_execution_verified': False,
            'model_comparison_eligible': False, 'smoke_candidate': status == 'entry_available',
            'evidence': str(preflight_path.relative_to(ROOT)) if r else None,
            'note': 'Entry availability only. All source goals, semantic triggers and outcome checkers still need actual runtime verification.'}
    for c in all_cases:
        c['website_validity']['entry_readiness'] = copy.deepcopy(readiness[c['author_profile_id']])
    write_json(folder / 'live_readiness.json', {'by_profile_id': readiness, 'profile_cluster': profile_cluster,
                'formal_comparison_eligible': False, 'source_cluster_count': len(clusters),
                'warning': 'HTTP/browser entry available is not complete task success or valid trigger evidence.'})
    families = sorted({c['task_family_id'] for c in all_cases})
    random.Random(20261004).shuffle(families)
    assignments = {f: 'development' if i < round(len(families) * .6) else 'heldout_candidate' for i, f in enumerate(families)}
    # Entire A/B/C source profile family stays together, across every experiment.
    for c in all_cases:
        c['split'] = assignments[c['task_family_id']]
    write_json(folder / 'split_manifest.json', {'seed': 20261004, 'grouping_key': 'task_family_id', 'family_assignment': assignments,
               'status': 'author_proposed_not_official_Mind2Web_split', 'pilot_tuning_must_not_use_heldout': True,
               'author_profile_count': len(PROFILES), 'source_cluster_count': len(clusters),
               'profile_cluster': profile_cluster,
               'warning': '100 main variants represent 20 author profiles with source-ID-overlap merged clusters, not 100 independent source tasks. Bootstrap and heldout analyses must respect merged families.'})
    write_json(folder / 'SCHEMA_EXAMPLE.json', single[0])
    input_audits={}
    for view in ('event-only','goal'):
        buckets={}
        for c in single:
            payload={'workspace_rules':c['rules'],
                     'incoming_event':{k:c['events'][0][k] for k in ('id','source','text')}}
            if view=='goal':payload['original_user_task']=c['goal']
            fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            buckets.setdefault(fingerprint,[]).append(c)
        ambiguous=[{'input_sha256':key,'case_ids':[c['case_id'] for c in members],
                    'label_counts':dict(Counter(c['gold']['decision'] for c in members))}
                   for key,members in buckets.items() if len({c['gold']['decision'] for c in members})>1]
        input_audits[view]={'registered_cases':len(single),'distinct_visible_inputs':len(buckets),
                           'ambiguous_input_groups':len(ambiguous),
                           'cases_in_ambiguous_groups':sum(len(x['case_ids']) for x in ambiguous),
                           'stateless_empirical_best_accuracy':sum(max(Counter(c['gold']['decision'] for c in members).values()) for members in buckets.values())/len(single),
                           'ambiguous_groups':ambiguous}
    write_json(folder/'INPUT_IDENTIFIABILITY.json',{
        'scope':'all_100_registered_main_cases_before_environment_eligibility',
        'interpretation':'Input ablations intentionally remove information. Identical supplied inputs with differing labels are underidentified, not evidence of model incapacity. These bounds apply only to this exact registered cohort and stateless case calls.',
        'selection_independent_of_model_scores':True,'views':input_audits})
    write_json(folder / 'source_profiles.json', [dict(p, source_a=rows[p['a']]['goal'], source_b=rows[p['b']]['goal'], source_c=rows[p['c']]['goal']) for p in PROFILES])
    annotations = []
    for c in all_cases:
        for e in c['events']:
            annotations.append({'case_id': c['case_id'], 'event_id': e['id'], 'task_family_id': c['task_family_id'], 'split': c['split'],
                'proposed_decision': e['gold']['decision'], 'proposed_follow_up': e['gold']['follow_up'],
                'reviewer_1': '', 'reviewer_1_decision': '', 'reviewer_1_follow_up': '',
                'reviewer_2': '', 'reviewer_2_decision': '', 'reviewer_2_follow_up': '',
                'adjudicator': '', 'adjudicated_decision': '', 'adjudicated_follow_up': '',
                'source_pair_verified': '', 'trigger_verified': '', 'execution_verified': '',
                'semantic_equivalence_verified': '', 'review_status': 'pending', 'notes': ''})
    with (folder / 'annotation_review.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, list(annotations[0]))
        writer.writeheader(); writer.writerows(annotations)
    write_jsonl(folder / 'annotation_review.jsonl', annotations)
    with (folder / 'cases_100_review.csv').open('w', encoding='utf-8-sig', newline='') as f:
        fields = ['case_id', 'task_family_id', 'split', 'difficulty', 'scenario_type', 'source_task_a', 'source_task_b', 'source_task_c', 'original_task_a', 'original_task_b', 'original_task_c', 'event_source_task_id', 'event_original_task', 'event_adapted_task_goal', 'adapted_goal', 'event', 'gold', 'follow_up', 'gold_rationale', 'review_status']
        writer = csv.DictWriter(f, fields); writer.writeheader()
        for c in single:
            e = c['events'][0]
            writer.writerow(dict(case_id=c['case_id'], task_family_id=c['task_family_id'], split=c['split'], difficulty=c['difficulty'],
                                 scenario_type=c['scenario_type'], source_task_a=c['source_task_a']['task_id'], source_task_b=c['source_task_b']['task_id'], source_task_c=c['source_task_c']['task_id'],
                                 original_task_a=c['source_task_a']['original_goal'], original_task_b=c['source_task_b']['original_goal'],
                                 original_task_c=c['source_task_c']['original_goal'],
                                 event_source_task_id=e['source_task']['task_id'] if e['source_task'] else '',
                                 event_original_task=e['source_task']['original_goal'] if e['source_task'] else '',
                                 event_adapted_task_goal=e['execution'].get('goal', e['execution'].get('updated_goal', '')),
                                 adapted_goal=c['goal'], event=e['text'], gold=e['gold']['decision'], follow_up=e['gold']['follow_up'],
                                 gold_rationale=e['annotation']['gold_rationale'], review_status='pending'))
    lines = ['# Online EventArena v2：100 条单事件候选详情', '', '> 全部是待人工审核与在线执行确认的 author candidates；不是已验证的真实通知。', '']
    for c in single:
        e = c['events'][0]
        lines += [f"## {c['case_id']} · {c['difficulty']} · {c['task_family_id']}",
                  f"- 原任务 A（{c['source_task_a']['task_id']}）：{c['source_task_a']['original_goal']}",
                  f"- 原任务 B（{c['source_task_b']['task_id']}）：{c['source_task_b']['original_goal']}",
                  f"- 原任务 C（{c['source_task_c']['task_id']}）：{c['source_task_c']['original_goal']}",
                  f"- 实际公开网页任务：{c['goal']}", f"- 插入事件：{e['text']}",
                  (f"- 本事件实际来源任务（{e['source_task']['task_id']}）：{e['source_task']['original_goal']}"
                   if e['source_task'] else '- 本事件来源：作者设定的授权修订或撤回；不宣称来源数据存在对应通知。'),
                  f"- 本事件可执行目标：{e['execution'].get('goal', e['execution'].get('updated_goal', ''))}",
                  f"- 候选标签：{e['gold']['decision']} / {e['gold']['follow_up'] or '—'}",
                  f"- 标签理由（不输入模型）：{e['annotation']['gold_rationale']}",
                  f"- 原任务关联：{c['source_relation']['description']}",
                  '- 触发：实际操作后观察到对应内容；GOTO 首页不算触发。', '']
    (folder / 'CASES_100_ZH.md').write_text('\n'.join(lines), encoding='utf-8')
    card = '''# EventArena Online v2 数据卡

## 版本与证据边界
本目录是 online_v2.0.4-author-candidate。已完成结构构建和本地一致性验证；未声称完成 100 条网站执行验证、双人标注或官方 Mind2Web 在线复现。旧版本保持不变。

## v2.0.4 修订
- 共用准备先于被测 actor 执行。资格清单由实际检查点准备与重放审计决定，不读取被测模型的预测、正确率或任务成功。
- 真实准备过程录制 HAR 网络响应和浏览器操作。重放到检查点后解除网络重放，后续访问恢复 live。HAR 缺失、hash 变化、未录制请求或任务正文/控件值不一致仍为 unknown；不得静默回退 live 后放宽审计。
- 检查点按预登记的 main/article/[role=main] 完整正文及控件值比较；只排除全站导航、页脚及明确 cookie 面板。价格、天气、要求和用户输入值保持严格相等。缺失/不稳定正文不使用 homepage/body 兜底。
- Full Trajectory 使用一个跨组件共享引用池，所有动作、观察、边界历史和当前页都能精确解码；不截历史、不编进度或剩余步数。
- 多事件公开编号与数组顺序按固定 seed 独立打乱，并同步改写所有 delivery、verifier、setup 与 schedule 引用。编号/位置/source 的标签分布由 validator 输出，不能形成完美类别捷径。
- 相关 B 通知共用中性文本，不以通知模板区分 HANDLE/DEFER/IGNORE；原目标的接受条件和实际 verified prior results 才决定标签。INPUT_IDENTIFIABILITY.json 报告 event-only 与 goal 的相同输入多标签情况及注册样本的经验上界；缺失必要上下文的错误不能全部解释为推理能力不足。
- Goal counterfactual 的明确 request reference 写在 goal 字符串中；incoming reference 固定，仅改变目标。修订保留原市场趋势信息，六个月历史参考不能充当一年新增区间的证据，SFO 泛型车型信息不能充当机场实时库存。
- A 与事件 B 的答案分别检查；初始接受条件通过 B 的实际完成绑定验证，不强迫将 B 的整份答案复制进 A。无事件 baseline 的组合目标仍需完成其全部要求。
- 反事实汇总保留所有 repeat；多事件计划只按各阶段已见事件评分，完整执行仍按全部注册事件检查。未见/未应用修订不提前改变 A 的内容验证目标。
- v2.0.3 原结果不覆盖。旧轨迹只可另存修正评分；改数据/输入/初始条件后的 v2.0.4 必须使用新目录重新运行。

## 构成
- 单事件 100 条：IGNORE 30、DEFER 30、INTERRUPT 40（HANDLE 20、REPLAN 10、TERMINATE 10）。easy 30、medium 50、hard 20。
- Counterfactual 70 条：10 Goal pairs、10 State pairs、10 Semantic triples。State pairs 用真实浏览器 warmup B 的已验证结果与未执行状态比较，不能伪造 completed 历史。
- Multi-event 20 条、70 个事件：10 个同时到达的三事件案例，10 个在 B 完成后追加事件的四事件案例。
- 100 条是 20 个 author profiles 的情境变体。共享任一原 A/B/C task ID 的 profile 合并为同一 task_family_id / source_family_id（见 split_manifest.json）；该 pilot 不是 100 个独立网站任务，相关变体不能按独立样本扩大显著性。

## 原任务与新增内容
每个来源家族包含真实 Mind2Web A/B/C task ID、原始目标与公开研究适配。B 是来源数据中的另一项实际任务，处理 B 必须浏览相关网页并保存有证据的答案；不以点击 synthetic receipt、draft 或任意 privacy/help 页面代替。
事件中的 source_task 明确标记实际来源，可能为 B 或 C；授权修订和撤回则明确标记为作者设定。范围不相关的 IGNORE 事件也保留真实 C 查询入口，不根据隐藏 Gold 禁止操作，因此错误处理它会留下可测的额外网页行为。重复通知的历史完成结果必须实际执行并验证，不能伪造。
来源网页没有可恢复的实时站点状态。起始 URL 由官方域名映射，购买、预约、申请、投票、消息等改变外部状态的操作改成公开查询；因此不是原任务完全等价复现。
A/B 联系与处理顺序是作者新增且明示的工作流，不能写成 Mind2Web 官方关系。HANDLE 的 checklist 在初始请求中注册；事件只启动尚未完成的原有接受条件，不偷偷修改目标。相关性强弱不同，必须通过独立人工审核。

## 注入与模型输入
事件在相关非主页的真实内容/控件出现、且已有成功的 actor 导航或交互后触发；注册首页入口本身不算进展。运行时生成 Full Trajectory；没有人为填写步骤历史、完成百分比、剩余步数或估计成本。触发条件、Gold、评估规则、作者理由不输入 actor。
Event-only、Event+Goal、Event+Goal+State、Full Trajectory 共享实际 checkpoint，经浏览器重放与语义审计后比较事件注入时的上下文和既往记忆，再继续自主网页执行。后续各组均可见操作所需的当前授权指令与新页面，既往历史仍按各组投影保留。主实验与 Full Trajectory 消融复用同一实际 episode；消融及反事实报告与主实验一致的完整指标。Counterfactual 初始页面、事件与指定变量必须有运行时不变量审计，失败时保留 unknown，不能强行填分。

## 权限与标签
一级 IGNORE / DEFER / INTERRUPT；二级仅在 INTERRUPT 时 HANDLE / REPLAN / TERMINATE。Gold 全部是作者候选。事件有明确请求人和任务范围；重复任务只有真实已验证完成状态才能 IGNORE，单纯自述不够。独立追加请求在原任务后执行，既有依赖先执行，修订改变有效目标，取消停止原任务。

## 难度、划分与审核
难度是作者的结构性分层（主体/范围辨析、来源比较、条件保留、完成记忆、分批到达），不是用模型错误率反推标签，更不是保证降低准确率。source-ID-overlap 合并后的 family-grouped development/heldout_candidate 划分跨全部实验保持一致，并不是原始 Mind2Web 官方 split。live_readiness.json 区分入口可用、checkpoint 验证与 A/B 任务验证：前者不能替代后两者。
annotation_review.csv/jsonl 留空两名审核者与裁决记录；不得把候选标签改写为人工审核结论。正式发布前需验证 A/B 来源、具体触发 checkpoint、网站可访问性、Gold、语义等价与独立 outcome checks；冻结排除清单后再横向比较模型。

## 结果与缺失
任务成功依赖实际观察证据及独立检查。Judge 未配置、访问失败、setup B 未验证、触发未到达都保留具体状态。报告评估覆盖率及缺失原因；不要仅凭模型 completion claim 计为成功，也不能保证得到指定或高分实验结果。

## 许可与发布
保留源任务 ID、适配目标与作者事件。遵循 Mind2Web 原始许可证和使用限制，不公开重新分发解压后的受限制测试 HTML；运行日志发布前检查个人信息和站点内容授权。原始 1750 条记录保持本地不改动。
'''
    card = card.replace('online_v2.0.0-author-candidate', VERSION)
    (folder / 'DATASET_CARD_ZH.md').write_text(card, encoding='utf-8')
    return all_cases


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output-dir', type=Path, default=OUT)
    ap.add_argument('--tasks', type=Path, default=ROOT / 'data/normalized/tasks.jsonl')
    args = ap.parse_args()
    if not args.tasks.exists() and args.tasks == ROOT / 'data/normalized/tasks.jsonl':
        args.tasks = OUT / 'source_task_index.jsonl'
        print('Original corpus absent; rebuilding selected candidates from bundled original-task metadata index.')
    rows = {r['task_id']: r for r in [json.loads(l) for l in args.tasks.read_text().splitlines() if l.strip()]}
    for p in PROFILES:
        for key in ('a', 'b', 'c'):
            if p[key] not in rows:
                raise ValueError(f"Missing original source {p['name']}: {p[key]}")
            if rows[p[key]]['website'] != p['site']:
                raise ValueError('Source website mismatch')
    single, cf, multi = main_cases(rows), counterfactual_cases(rows), multi_cases(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    all_cases = write_docs(args.output_dir, single, cf, multi, rows)
    source_ids = sorted({p[k] for p in PROFILES for k in ('a','b','c')})
    write_jsonl(args.output_dir / 'source_task_index.jsonl', [
        {k: rows[tid][k] for k in ('task_id','goal','website','split')} for tid in source_ids])
    files = {}
    for filename, cases in [('single_event.jsonl', single), ('counterfactual.jsonl', cf), ('multi_event.jsonl', multi)]:
        path = args.output_dir / filename
        write_jsonl(path, cases)
        files[filename] = {'cases': len(cases), 'events': sum(len(c['events']) for c in cases),
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {'schema_version': VERSION, 'created_date': '2026-10-06', 'status': 'author_candidate',
                'files': files, 'main_labels': dict(Counter(c['gold']['decision'] for c in single)),
                'follow_up_labels': dict(Counter(c['gold']['follow_up'] for c in single if c['gold']['decision'] == 'INTERRUPT')),
                'difficulty': dict(Counter(c['difficulty'] for c in single)),
                'counterfactual_groups': {'goal': 10, 'state': 10, 'semantic': 10},
                'author_profile_count': len(PROFILES),
                'source_task_families': len({c['task_family_id'] for c in single}),
                'independent_main_task_family_count': len({c['task_family_id'] for c in single}),
                'unique_original_source_tasks': len({p[k] for p in PROFILES for k in ('a', 'b', 'c')}),
                'full_trajectory_origin': 'runtime_only_no_authored_logs', 'human_reviewed': False,
                'all_cases_execution_verified': False, 'structured_annotation_rows': sum(len(c['events']) for c in all_cases),
                'source_corpus': {'tasks': len(rows), 'path': str(args.tasks.relative_to(ROOT)) if args.tasks.is_relative_to(ROOT) else str(args.tasks)},
                'source_task_index': {'tasks': len(source_ids), 'path': 'source_task_index.jsonl',
                                      'sha256': hashlib.sha256((args.output_dir / 'source_task_index.jsonl').read_bytes()).hexdigest()},
                'limitations': ['Live public websites can change or reject automation.', 'Author-added dependencies are not official Mind2Web relations.',
                               'Case variants share source families; use grouped analysis.', 'All Gold labels require independent human review.']}
    write_json(args.output_dir / 'manifest.json', manifest)
    print(json.dumps({'built': str(args.output_dir), 'counts': {k: v['cases'] for k, v in files.items()},
                      'main_labels': manifest['main_labels'], 'difficulty': manifest['difficulty'], 'human_reviewed': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
