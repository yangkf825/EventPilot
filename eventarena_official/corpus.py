"""Lossless official-task import and provenance; never adapt the official goal."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from eventarena.storage_io import atomic_write_json
from .storage import atomic_write_bytes

DATASET = 'osunlp/Online-Mind2Web'
FILENAME = 'Online_Mind2Web.json'
PUBLIC_SOURCE = 'https://huggingface.co/datasets/' + DATASET


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # storage_io supplies fsync, atomic replace and bounded transient-I/O retry.
    atomic_write_bytes(path, ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode())


def public_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return parsed.scheme in {'https', 'http'} and bool(parsed.hostname) and not (parsed.username or parsed.password)
    except ValueError:
        return False


def normalize(raw, raw_sha256, revision=None):
    if not isinstance(raw, list) or not raw:
        raise ValueError('Official task file must be a nonempty JSON array')
    ids, rows = set(), []
    for task in raw:
        if not isinstance(task, dict):
            raise ValueError('Each official task must be an object')
        uid, goal, url = (task.get(key) for key in ('task_id', 'confirmed_task', 'website'))
        if not isinstance(uid, str) or not uid.strip() or uid in ids:
            raise ValueError('Missing or duplicate official task_id')
        if not isinstance(goal, str) or not goal.strip() or not public_url(url):
            raise ValueError('Invalid official goal or website: ' + uid)
        ids.add(uid)
        rows.append({'task_id': uid, 'goal': goal, 'url': url, 'level': task.get('level'),
                     'source': {'dataset': DATASET, 'filename': FILENAME,
                                'raw_sha256': raw_sha256, 'revision': revision},
                     # Keep all original fields outside the actor projection.
                     'official_record': task})
    return rows


def import_corpus(path, directory, revision=None):
    path, directory = Path(path), Path(directory)
    blob = path.read_bytes()
    rows = normalize(json.loads(blob), digest(blob), revision)
    raw_path = directory / 'raw' / FILENAME
    if raw_path.exists() and raw_path.read_bytes() != blob:
        raise ValueError('A different official version already exists here; choose a new --data-dir')
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(raw_path, blob)
    write_jsonl(directory / 'tasks.jsonl', rows)
    manifest = {'schema': 'official_online_corpus_v1', 'imported_at': now(),
                'source_url': PUBLIC_SOURCE, 'revision': revision, 'raw_sha256': digest(blob),
                'task_count': len(rows), 'unique_entry_urls': len({row['url'] for row in rows}),
                'unique_hosts': len({urlsplit(row['url']).hostname for row in rows}),
                'difficulty': dict(Counter(row['level'] for row in rows)),
                'tasks_sha256': digest((directory / 'tasks.jsonl').read_bytes()),
                'task_goal_policy': 'exact_official_confirmed_task_and_website',
                'reference_length_policy': 'retained_in_source_only_never_in_actor_input'}
    atomic_write_json(directory / 'manifest.json', manifest)
    return manifest


def load_corpus(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    blob = (directory / 'raw' / FILENAME).read_bytes()
    if digest(blob) != manifest['raw_sha256']:
        raise ValueError('Official raw source hash changed')
    expected = normalize(json.loads(blob), digest(blob), manifest['revision'])
    tasks_file = directory / 'tasks.jsonl'
    if digest(tasks_file.read_bytes()) != manifest['tasks_sha256'] or read_jsonl(tasks_file) != expected:
        raise ValueError('Normalized official tasks were altered')
    return expected, manifest


def task_case(task):
    """No-event baseline retains the official goal; no reference length/labels to Actor."""
    return {'case_id': 'OMW_' + task['task_id'], 'goal': task['goal'], 'url': task['url'],
            'site': urlsplit(task['url']).hostname, 'events': [], 'gold': {}, 'rules': [],
            'source_task_a': task['source'] | {'task_id': task['task_id'],
                                              'original_goal': task['goal'], 'original_url': task['url'],
                                              'same_as_original_interactive_task': True},
            'verification': {'CURRENT_TASK': {'method': 'llm_judge',
                                              'goal': task['goal'], 'checks': []}},
            'annotation': {'human_reviewed': False,
                           'full_trajectory_origin': 'runtime_browser_observations_and_actions_only'}}
