"""Convert recorded Mind2Web demonstrations without inventing state transitions."""
import hashlib
import json
import random
import re
from collections import Counter
from pathlib import Path

import ijson
from lxml import html

ROOT = Path(__file__).resolve().parents[1]


def jsonl(path):
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def page_text(source):
    """Deterministic page-text projection. Full source is retained separately."""
    if not source:
        return ''
    tree = html.fromstring(source)
    for node in tree.xpath('//script|//style|//noscript'):
        node.drop_tree()
    text = re.sub(r'\s+', ' ', tree.text_content()).strip()
    controls = []
    for node in tree.xpath('//input|//select|//textarea|//button'):
        if node.get('type','').lower() in ('hidden','password'):
            continue
        attrs = {k: node.get(k) for k in ('type', 'name', 'id', 'value', 'placeholder', 'aria-label', 'disabled',
                                         'readonly','checked','aria-checked','aria-selected',
                                         'input_value','text_value','input_checked','backend_node_id')
                 if node.get(k) is not None}
        # Mind2Web serializes runtime DOM properties as these custom attributes.
        # They are actual recorded values, not values inferred from future actions.
        if node.tag=='select':
            selected=[{'label':re.sub(r'\s+',' ',o.text_content()).strip(),'value':o.get('value')}
                      for o in node.xpath('.//option')
                      if o.get('option_selected','').lower()=='true' or o.get('selected') is not None]
            if selected:attrs['recorded_selected_options']=selected
        if attrs:
            controls.append(f'{node.tag}: {json.dumps(attrs, ensure_ascii=False)}')
    return text + ('\nForm controls:\n' + '\n'.join(controls) if controls else '')


def normalize():
    raw = ROOT / 'data/raw/Mind2Web'
    files = sorted(raw.glob('**/train_*.json')) + sorted(raw.glob('**/test_*/*.json'))
    if not files:
        raise RuntimeError('No verified Mind2Web JSON files. Run scripts/download_data.py first.')
    out = ROOT / 'data/normalized'
    out.mkdir(parents=True, exist_ok=True)
    counts, seen, revisions = Counter(), set(), []
    with (out / 'tasks.jsonl').open('w', encoding='utf-8') as dest:
        for path in files:
            split = path.parent.name
            revisions.append({'file': str(path.relative_to(raw)), 'bytes': path.stat().st_size})
            with path.open('rb') as stream:
                for task in ijson.items(stream, 'item', use_float=True):
                    uid = task['annotation_id']
                    if not re.fullmatch(r'[A-Za-z0-9_-]+', uid):
                        raise ValueError(f'Unsafe task ID: {uid!r}')
                    if uid in seen:
                        raise ValueError(f'Duplicate task ID: {uid}')
                    seen.add(uid)
                    asset_dir = out / 'snapshots' / uid
                    asset_dir.mkdir(parents=True, exist_ok=True)
                    steps = []
                    for idx, action in enumerate(task['actions']):
                        cleaned = asset_dir / f'{idx:03d}.cleaned.html'
                        original = asset_dir / f'{idx:03d}.raw.html'
                        cleaned.write_text(action['cleaned_html'], encoding='utf-8')
                        original.write_text(action['raw_html'], encoding='utf-8')
                        steps.append({'step': idx, 'action_uid': action['action_uid'],
                                      'cleaned_html': str(cleaned.relative_to(ROOT)),
                                      'raw_html': str(original.relative_to(ROOT)),
                                      'action': task['action_reprs'][idx],
                                      'operation': action['operation'],
                                      'positive_node_ids': [str(c['backend_node_id']) for c in action.get('pos_candidates', [])]})
                    row = {'task_id': uid, 'split': split, 'goal': task['confirmed_task'],
                           'website': task['website'], 'domain': task['domain'],
                           'subdomain': task.get('subdomain'), 'steps': steps,
                           'provenance': 'recorded_human_demonstration', 'interactive_environment': False}
                    dest.write(json.dumps(row, ensure_ascii=False) + '\n')
                    counts[split] += 1
            print(f'Normalized {path.name}; total tasks={len(seen)}', flush=True)
    report = {'counts': dict(counts), 'tasks': len(seen), 'source_files': revisions,
              'note': 'Recorded snapshots are not a branching website simulator.'}
    (out / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def prefix_case(task, checkpoint):
    """The current action and every future action stay outside the visible prefix."""
    if checkpoint < 0 or checkpoint >= len(task['steps']):
        raise ValueError('Checkpoint must identify an observed pre-action snapshot')
    trajectory = []
    # The original JSON has no per-step URLs. Do not fabricate them from website names.
    for step in task['steps'][:checkpoint + 1]:
        source = (ROOT / step['cleaned_html']).read_text(encoding='utf-8')
        trajectory.append({'step': step['step'], 'url': '', 'observation': page_text(source),
                           'action': step['action'] if step['step'] < checkpoint else None,
                           'snapshot_path': step['cleaned_html'],
                           'observation_source': 'recorded_cleaned_html'})
    return {'case_id': f'M2W_{task["task_id"]}_{checkpoint:03d}',
            'base_task_id': task['task_id'], 'goal': task['goal'],
            'site': task['website'], 'domain': task['domain'],
            'state': {'url': '', 'summary': trajectory[-1]['observation'],
                      'recent_action': trajectory[-2]['action'] if checkpoint else None},
            'trajectory': trajectory,
            'annotation': {'status': 'needs_event_authoring_and_review', 'human_reviewed': False,
                           'original_split': task['split'], 'checkpoint': checkpoint,
                           'provenance': 'recorded_human_demonstration',
                           'event_provenance': 'not_yet_authored', 'ui_executed_after_event': False}}


def candidates(limit=100, seed=20261002, split=None):
    path = ROOT / 'data/normalized/tasks.jsonl'
    tasks = [t for t in jsonl(path) if split is None or t['split']==split or (split=='test' and t['split'].startswith('test_'))]
    if not tasks:
        raise ValueError('No tasks in this split. Extract the test archive and run normalize first.')
    rng = random.Random(seed)
    rng.shuffle(tasks)
    # Round-robin domains, preserving task boundaries and original goals.
    grouped = {}
    for task in tasks:
        grouped.setdefault(task['domain'], []).append(task)
    chosen = []
    while len(chosen) < min(limit, len(tasks)):
        for domain in sorted(grouped):
            if grouped[domain] and len(chosen) < limit:
                chosen.append(grouped[domain].pop())
    target = ROOT / 'data/candidates/checkpoints.jsonl'
    with target.open('w', encoding='utf-8') as dest:
        for task in chosen:
            idx = max(0, len(task['steps']) // 2)
            row = prefix_case(task, idx)
            row['event'] = {'source': '', 'text': ''}
            row['gold'] = {'decision': None, 'follow_up': None}
            dest.write(json.dumps(row, ensure_ascii=False) + '\n')
    report = {'candidate_count': len(chosen), 'requested': limit, 'seed': seed, 'split_filter':split,
              'domain_counts': dict(Counter(t['domain'] for t in chosen)),
              'status': 'UNLABELED: not a ready-to-score event benchmark'}
    (target.parent / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def snapshot_actions(task, index):
    """All visible element candidates, never labeled positive/negative groups."""
    source = (ROOT / task['steps'][index]['cleaned_html']).read_text(encoding='utf-8')
    tree = html.fromstring(source)
    candidates = []
    for node in tree.xpath('//*[@backend_node_id]'):
        if node.tag not in ('a', 'button', 'input', 'select', 'textarea') and node.get('role') not in ('button', 'link', 'option', 'textbox', 'combobox'):
            continue
        attrs = {k: node.get(k) for k in ('role', 'type', 'name', 'value', 'placeholder', 'aria-label') if node.get(k)}
        candidates.append({'node_id': node.get('backend_node_id'), 'tag': node.tag,
                           'text': re.sub(r'\s+', ' ', node.text_content()).strip(), 'attributes': attrs})
    return candidates
