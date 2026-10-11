"""Gold-only constraint field compatibility; unchanged frozen outcome semantics."""
from __future__ import annotations
import copy
import hashlib
import json

from eventarena.online_v2_eval import evaluate_episode as frozen_evaluate_episode

POLICY = 'exact_text_requirement_alias_v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def normalize_constraints(constraints):
    if not isinstance(constraints, list):
        raise ValueError('effective_constraints must be a list')
    normalized, ids = [], set()
    for value in constraints:
        if not isinstance(value, dict):
            raise ValueError('Each effective constraint must be an object')
        row = copy.deepcopy(value)
        if not isinstance(row.get('id'), str) or not row['id'].strip() or row['id'] in ids:
            raise ValueError('Effective constraints require unique nonempty IDs')
        ids.add(row['id'])
        if row.get('origin') not in {'new', 'retained'}:
            raise ValueError('Constraint origin must be new or retained')
        requirement, text = row.get('requirement'), row.get('text')
        if requirement is not None and text is not None and requirement != text:
            raise ValueError('Conflicting text and requirement fields')
        requirement = requirement if requirement is not None else text
        if not isinstance(requirement, str) or not requirement.strip():
            raise ValueError('Each constraint requires nonempty requirement or text')
        if 'verification' in row and not isinstance(row['verification'], dict):
            raise ValueError('Constraint verification must be an object')
        row['requirement'] = requirement  # Exact alias; no semantic rewrite.
        normalized.append(row)
    return normalized


def normalized_case(case):
    """Normalize only the private verification contract, never Actor input."""
    result, cache_aliases = copy.deepcopy(case), {}
    verifiers = list(result.get('verification', {}).values())
    for event in result.get('events', []) or ([result['event']] if result.get('event') else []):
        execution = event.get('execution', {})
        verifiers.extend(execution[k] for k in ('verification', 'updated_verification') if k in execution)
    for verifier in verifiers:
        if not isinstance(verifier, dict):
            raise ValueError('Task verification must be an object')
        if 'effective_constraints' in verifier:
            before = digest(verifier)
            verifier['effective_constraints'] = normalize_constraints(verifier['effective_constraints'])
            cache_aliases[before] = digest(verifier)
    return result, cache_aliases


async def evaluate_episode(case, episode, judge=None):
    registered, aliases = normalized_case(case)
    evidence = copy.deepcopy(episode)
    # A task Judge has already evaluated the identical goal, output and sources.
    # The frozen evaluator still checks all of them and source integrity. Only
    # the verifier hash spelling changes; its outcome is never overwritten.
    for cache in evidence.get('verified_tasks', {}).values():
        if isinstance(cache, dict) and cache.get('verification_sha256') in aliases:
            cache['verification_sha256'] = aliases[cache['verification_sha256']]
    return await frozen_evaluate_episode(registered, evidence, judge)


async def rejudge_saved_episode(case, original, judge):
    from run_online_v2 import ACTOR_FACT_FIELDS, INFRA_RETRY_POLICY, now, rejudgeable_episode
    if not rejudgeable_episode(original):
        raise ValueError('Only an intact unknown evaluation can be rejudged')
    episode = copy.deepcopy(original)
    actor_sha = digest({k: original[k] for k in ACTOR_FACT_FIELDS if k in original})
    caches = episode.get('verified_tasks', {})
    invalid = [tid for tid, value in caches.items() if isinstance(value, dict) and
               (not isinstance(value.get('result'), dict) or value['result'].get('success') is None)]
    episode['verified_tasks'] = {tid: value for tid, value in caches.items() if tid not in invalid}
    episode['evaluation'] = await evaluate_episode(case, episode, judge)
    episode['actual_schedule'] = episode['evaluation'].get('actual_schedule', [])
    if actor_sha != digest({k: episode[k] for k in ACTOR_FACT_FIELDS if k in episode}):
        raise ValueError('Rejudging changed Actor facts')
    episode.setdefault('infrastructure_recovery', []).append({
        'policy': INFRA_RETRY_POLICY, 'timestamp': now(), 'mode': 'judge_only',
        'actor_resampled': False, 'actor_facts_sha256': actor_sha,
        'previous_evaluation_sha256': digest(original['evaluation']),
        'invalidated_unknown_verifications': invalid, 'evaluation_schema_policy': POLICY})
    return episode
