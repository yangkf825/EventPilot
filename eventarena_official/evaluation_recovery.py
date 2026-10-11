"""Narrow, recorded repair of v5 constraint spelling and completed Actor traces."""
from __future__ import annotations
import copy
import json
from pathlib import Path

from .evaluation import POLICY, digest, evaluate_episode
from .preparation_import import atomic_write_bytes, digest as byte_digest

KNOWN_SOURCE_CODE = '1c747c22a62fe52029411019e8a416f5f716b6605809c8fd8903a524c93d29de'
REPAIR_NAME = 'constraint_schema_v1'


def pending_evaluation(episode):
    """Only a finished, audited factual trace can bypass Actor resampling."""
    if not isinstance(episode, dict) or episode.get('status') != 'agent_finished':
        return False
    if not all(isinstance(episode.get(k), dict) for k in ('task_states', 'task_outputs', 'events')):
        return False
    if not episode.get('responses') or not episode.get('observations') or not episode.get('actions'):
        return False
    if episode.get('protocol') == 'controlled_episode_v2':
        audit = episode.get('checkpoint_replay_audit', {})
        if audit.get('matches') is not True or audit.get('available', True) is not True:
            return False
        if episode.get('checkpoint_replay_cleanup_verified') is not True:
            return False
    return all(isinstance(row, dict) and isinstance(row.get('url'), str) and
               isinstance(row.get('text'), str) for row in episode['observations'])


def descriptor(path):
    episode = json.loads(path.read_text())
    if not pending_evaluation(episode):
        raise ValueError('Completed Actor evidence is not intact; cannot perform Judge-only repair')
    judges = path.with_name('judge_responses.json')
    return {'policy': POLICY, 'actor_episode_file': str(path.resolve()),
            'actor_episode_sha256': byte_digest(path.read_bytes()),
            'judge_responses_file': str(judges.resolve()) if judges.exists() else None,
            'judge_responses_sha256': byte_digest(judges.read_bytes()) if judges.exists() else None,
            'actor_resampled': False}


def save_evaluation_failure(folder, attempt_folder, state, error):
    from run_online_v2 import read_json, write_json, now
    path = attempt_folder / 'episode.json'
    if not path.exists() or not pending_evaluation(read_json(path)):
        return False
    episode = read_json(path)
    episode.update(evaluation={'episode_success': None, 'branch_success': None,
                              'environment_valid': True, 'failure_category': 'evaluation_unverified'},
                   error_type=type(error).__name__)
    write_json(folder / 'episode.json', episode)
    state.update(state='evaluation_pending', updated_at=now(), error_type=type(error).__name__,
                 result_status='evaluation_pending', evaluation_recovery=descriptor(path))
    write_json(folder / 'job.json', state)
    return True


async def recover_evaluation(case, job, folder, judge):
    from run_online_v2 import ACTOR_FACT_FIELDS
    recovery = job.get('evaluation_recovery', {})
    path = Path(recovery['actor_episode_file'])
    if byte_digest(path.read_bytes()) != recovery['actor_episode_sha256']:
        raise ValueError('Saved Actor episode changed after failure')
    episode = json.loads(path.read_text())
    if not pending_evaluation(episode) or episode['case_id'] != case['case_id']:
        raise ValueError('Actor episode is incomplete or belongs to another case')
    records = []
    if recovery.get('judge_responses_file'):
        judges = Path(recovery['judge_responses_file'])
        if byte_digest(judges.read_bytes()) != recovery['judge_responses_sha256']:
            raise ValueError('Saved Judge responses changed after failure')
        records = json.loads(judges.read_text())
    # Reuse only an exact successful request. Valid false/null verdicts remain
    # frozen, and format repairs still use the unchanged registered policy.
    async def cached_judge(messages):
        for row in records:
            if row.get('api_ok') is True and row.get('messages') == messages and 'raw' in row:
                return row['raw']
        if judge is None:
            raise ValueError('A fixed Judge is required for missing constraint scores')
        return await judge(messages)
    facts = {k: copy.deepcopy(episode[k]) for k in ACTOR_FACT_FIELDS if k in episode}
    episode['evaluation'] = await evaluate_episode(case, episode, cached_judge)
    if facts != {k: episode[k] for k in ACTOR_FACT_FIELDS if k in episode}:
        raise ValueError('Evaluation repair changed Actor facts')
    episode['actual_schedule'] = episode['evaluation'].get('actual_schedule', [])
    episode['judge_responses'] = records
    episode.setdefault('infrastructure_recovery', []).append({
        **recovery, 'mode': 'evaluation_only', 'actor_facts_sha256': digest(facts)})
    return episode


def migrate_constraint_schema(root, previous, current):
    """Accept only this exact delivered v5 spelling repair; preserve run inputs."""
    from run_online_v2 import job_path, read_json, write_json, now
    if (previous.get('code_sha256') != KNOWN_SOURCE_CODE or
            previous.get('protocol_version') != 'official_online_eventarena_v5' or
            'evaluation_schema_policy' in previous):
        raise ValueError('This run is not the verified v5 constraint-schema repair source')
    stripped = {k: v for k, v in current.items() if k not in {'code_sha256', 'evaluation_schema_policy'}}
    original = {k: v for k, v in previous.items() if k != 'code_sha256'}
    if stripped != original or current.get('evaluation_schema_policy') != POLICY:
        raise ValueError('Constraint repair requires identical inputs, cases, models, budgets and preparation')
    plans, untouched = [], []
    for job in previous['jobs']:
        folder = job_path(root, job)
        state = read_json(folder / 'job.json')
        if not state:
            continue
        episode = read_json(folder / 'episode.json', {})
        if state.get('state') in {'finished', 'evaluation_pending'} and episode.get('status') == 'runner_error':
            path = folder / 'attempts' / f"attempt_{state['attempt']:03d}" / 'episode.json'
            trace = path.with_name('error_traceback.txt')
            if (episode.get('error_type') != 'KeyError' or not trace.exists() or
                    "requirement = constraint['requirement']" not in trace.read_text()):
                raise ValueError('Unknown runner failure; refusing automatic trace recovery')
            plans.append((folder, state, descriptor(path)))
        elif state.get('state') == 'finished':
            # Old text-only REPLAN episodes cannot have completed this scorer.
            # Reject unexpected preexisting outcomes rather than silently mix.
            if episode.get('evaluation', {}).get('effective_constraint_results'):
                raise ValueError('Scored constraints require an explicit evaluation audit')
            untouched.append({'job_id': job['id'], 'episode_sha256': byte_digest((folder/'episode.json').read_bytes())})
        else:
            raise ValueError('Stop the run before migrating; unexpected unfinished job')
    archive = Path(root) / 'repairs' / REPAIR_NAME
    archive.mkdir(parents=True, exist_ok=True)
    prior = archive / 'manifest_before.json'
    data = (Path(root) / 'manifest.json').read_bytes()
    if prior.exists() and prior.read_bytes() != data:
        raise ValueError('Repair archive already contains a different source manifest')
    if not prior.exists():
        atomic_write_bytes(prior, data)
    for folder, state, recovery in plans:
        for filename in ('job.json', 'episode.json', 'metric_record.json'):
            source = folder / filename
            if source.exists():
                destination = archive / 'jobs' / state['experiment'] / state['model'] / state['view'] / str(state['repeat']) / state['case_id'] / filename
                if not destination.exists(): atomic_write_bytes(destination, source.read_bytes())
        state = {**state, 'state': 'evaluation_pending', 'updated_at': now(),
                 'result_status': 'evaluation_pending', 'evaluation_recovery': recovery}
        write_json(folder / 'job.json', state)
    write_json(archive / 'repair.json', {
        'policy': POLICY, 'timestamp': now(), 'source_code_sha256': previous['code_sha256'],
        'target_code_sha256': current['code_sha256'], 'source_manifest_sha256': byte_digest(data),
        'target_manifest_sha256': digest(current), 'actor_resampled': False,
        'dataset_and_actor_inputs_changed': False, 'preserved_scored_jobs': untouched,
        'pending_evaluations': [recovery for _, _, recovery in plans]})
    write_json(Path(root) / 'manifest.json', current)
    print(f'[REPAIR] Constraint field compatibility recorded; {len(untouched)} scored jobs preserved, {len(plans)} completed traces queued for scoring only.', flush=True)
