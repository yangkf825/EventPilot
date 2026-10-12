"""Reuse factual v4 preparation in a new v5 run, always auditing resets again.

No eligibility rows or tested-actor scores are imported. The original run remains
immutable and its recorded HAR files remain referenced by SHA256.
"""
import hashlib
import json
import os
from pathlib import Path

from eventarena.storage_io import retry_io, atomic_write_json

# The frozen v4 source delivered on 2026-10-10 and verified before this repair.
KNOWN_SOURCE_CODES = {'8c1ec74a82e47e787be5628551bddb0edbdcbc4607ccca93747a1e6240159c61'}
KNOWN_V6_CODES={'1ac7ad31aac3680c6849ae82a17d960aad156e5cf4f594d4b914ee93deb12681'}
PREPARATION_ARGUMENTS = ('repeats', 'prep_model', 'judge_model', 'pre_steps', 'setup_steps',
                         'max_context_chars', 'loop_limit', 'format_retries', 'headless')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    def commit():
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    retry_io(commit, path)


def _captures(value):
    if isinstance(value, dict):
        if value.get('mode') == 'recorded_prefix_then_live' and 'har_path' in value:
            yield value
        for child in value.values():
            yield from _captures(child)
    elif isinstance(value, list):
        for child in value:
            yield from _captures(child)


def plan_import(source, target_manifest):
    from run_online_v2 import job_path
    source = Path(source).expanduser().resolve()
    manifest_path = source / 'manifest.json'
    original_bytes = manifest_path.read_bytes()
    original = json.loads(original_bytes)
    legacy_source=original.get('protocol_version')=='official_online_eventarena_v4' and original.get('code_sha256') in KNOWN_SOURCE_CODES
    v6_source=original.get('protocol_version')=='official_online_eventarena_v6' and original.get('code_sha256') in KNOWN_V6_CODES
    same_casewise=original.get('protocol_version')=='official_online_eventarena_v6_casewise' and original.get('code_sha256')==target_manifest.get('code_sha256')
    if not (legacy_source or v6_source or same_casewise):
        raise ValueError('Preparation import requires a registered compatible frozen source')
    for key in ('dataset_sha256', 'official_source', 'context_access_policy', 'context_read_limits'):
        if original.get(key) != target_manifest.get(key):
            raise ValueError('Preparation import differs in ' + key)
    for key in PREPARATION_ARGUMENTS:
        if original['arguments'].get(key) != target_manifest['arguments'].get(key):
            raise ValueError('Preparation import differs in ' + key)
    for name in {target_manifest['arguments'][key] for key in ('prep_model', 'judge_model')}:
        if original['model_configs'].get(name) != target_manifest['model_configs'].get(name):
            raise ValueError('Preparation import requires identical fixed preparation/Judge models')
    for job in original['jobs']:
        folder = job_path(source, job)
        if (folder / 'job.json').exists() or (folder / 'episode.json').exists():
            raise ValueError('Preparation import requires a source with no tested actors started')
    files, archives = {}, {}
    for p in sorted((source / 'checkpoints').glob('*/*.json')):
        if p.name not in {'episode.json', 'state_pair.json', 'checkpoint_case.json'}:
            continue
        data = p.read_bytes()
        record=json.loads(data)
        # Only terminal preparations can be imported from a partially running
        # source. A started browser capture is not a durable checkpoint.
        if record.get('status') not in {'checkpoint_ready','website_unavailable','checkpoint_setup_failed','checkpoint_setup_unverified','checkpoint_restoration_mismatch','checkpoint_capture_unavailable','provider_error','browser_error','invalid_output','context_overflow','loop_guard_exhausted','step_budget_exhausted'}:
            continue
        if p.name=='checkpoint_case.json':continue
        captures=list(_captures(record))
        if record.get('status')=='checkpoint_ready' and not captures:
            continue
        files[str(p.relative_to(source))] = digest(data)
        for capture in captures:
            har = Path(capture['har_path']).resolve()
            if not har.is_relative_to(source / 'checkpoints'):
                raise ValueError('Recorded HAR must belong to the source preparation run')
            if str(har) not in archives:
                actual = digest(har.read_bytes())
                if actual != capture['sha256']:
                    raise ValueError('Source preparation archive was altered')
                archives[str(har)] = actual
            elif archives[str(har)] != capture['sha256']:
                raise ValueError('Source archive has conflicting registrations')
    if not files or not archives:
        raise ValueError('No factual captured preparation available to import')
    reuse_eligibility=bool(v6_source or same_casewise)
    if reuse_eligibility:
        registered={c['case_id']:c for f in (source/'registered_data').glob('*.jsonl')
                    for line in f.read_text().splitlines() if line.strip() for c in [json.loads(line)]
                    if not c.get('original_case_id')}
        # Main/Ablation definitions are equal; skip transformed baselines.
        for p in sorted((source/'preparation_eligibility').glob('repeat_*/*/eligibility.json')):
            data=p.read_bytes();row=json.loads(data);case=registered.get(row['case_id'])
            if case is None or row.get('case_sha256')!=digest(json.dumps(case,sort_keys=True,ensure_ascii=False).encode()):
                raise ValueError('Source qualification does not bind the exact registered case')
            if row.get('checkpoint_key') and not any(name.startswith('checkpoints/'+row['checkpoint_key']+'/') for name in files):
                continue
            if row.get('eligible') is True and (row.get('reset_audit',{}).get('matches') is not True or row.get('reset_audit',{}).get('available') is not True):
                raise ValueError('Source qualification lacks a successful independent reset audit')
            files[str(p.relative_to(source))]=digest(data)
    return {'policy': 'compatible_factual_prefixes_and_verified_qualification_no_actor_scores_v2' if reuse_eligibility else 'verified_v4_factual_prefix_new_v5_reset_audit_v1', 'source_run': str(source),
            'source_manifest_sha256': digest(original_bytes), 'source_code_sha256': original['code_sha256'],
            'files': files, 'archives': archives, 'reuse_eligibility': reuse_eligibility,
            'reuse_tested_actor_results': False, 'all_reset_audits_required':not reuse_eligibility,
            'reused_reset_audits_must_bind_identical_cases_and_policies':reuse_eligibility}


def materialize_import(root, plan):
    root = Path(root)
    if root.resolve() == Path(plan['source_run']):
        raise ValueError('Imported preparation needs a new run directory')
    marker = root / 'preparation_import.json'
    if marker.exists():
        if json.loads(marker.read_text()) != plan:
            raise ValueError('Existing preparation import differs')
        return
    for relative, expected in plan['files'].items():
        data = (Path(plan['source_run']) / relative).read_bytes()
        if digest(data) != expected:
            raise ValueError('Preparation changed after registration')
        destination = root / relative
        if destination.exists() and destination.read_bytes() != data:
            raise ValueError('New run already contains different preparation')
        atomic_write_bytes(destination, data)
    atomic_write_json(marker, plan)
