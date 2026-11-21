"""Read the original recorded DOM and actions, without inventing transitions."""
from collections import Counter
from functools import lru_cache
from pathlib import Path
import re
import hashlib
import io
import zipfile

from lxml import html

from .io import read_jsonl, file_digest


def compact(text):
    return re.sub(r'\s+', ' ', str(text)).strip()


def snippet(node, limit):
    parts, length = [], 0
    for text in node.itertext():
        parts.append(text[:limit + 1]); length += len(parts[-1])
        if length > limit:
            break
    return compact(' '.join(parts))[:limit]


def operation_f1(gold_op, gold_value, predicted_op, predicted_value):
    # Official Mind2Web metric uses whitespace token sets, including operation.
    a = set((gold_op + ' ' + str(gold_value)).split())
    b = set((str(predicted_op) + ' ' + str(predicted_value)).split())
    return 2 * len(a & b) / (len(a) + len(b)) if a or b else 1.0


class Corpus:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.index = self.root / 'data/normalized/tasks.jsonl'
        rows = list(read_jsonl(self.index))
        self.tasks = {t['task_id']: t for t in rows}
        self.expected_snapshot_hashes = {}
        # A single local archive avoids thousands of cloud/removable-drive file
        # opens. It contains the unchanged HTML bytes, not projected/synthetic DOM.
        packed=self.root/'snapshots.zip'
        self.snapshot_archive=zipfile.ZipFile(io.BytesIO(packed.read_bytes())) if packed.is_file() else None
        self.snapshot_members=set(self.snapshot_archive.namelist()) if self.snapshot_archive else set()
        if len(rows) != len(self.tasks):
            raise ValueError('Duplicate source task IDs')

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('Source asset escapes the dataset root')
        return path

    @lru_cache(maxsize=48)
    def page(self, task_id, index, rich=False):
        step = self.tasks[task_id]['steps'][index]
        self.path(step['cleaned_html'])  # Validate the registered relative path.
        raw = self.snapshot_archive.read(step['cleaned_html']) if step['cleaned_html'] in self.snapshot_members else self.path(step['cleaned_html']).read_bytes()
        source = raw.decode('utf-8')
        tree = html.fromstring(source)
        for node in tree.xpath('//script|//style|//noscript'):
            node.drop_tree()
        text = compact(tree.text_content())
        nodes = []
        for node in tree.xpath('//*[@backend_node_id]'):
            if node.tag in {'html', 'head', 'meta', 'link'}:
                continue
            is_control = node.tag in {'input','textarea','select'}
            attrs = {k: node.get(k) for k in (
                'role', 'type', 'name', 'value', 'placeholder', 'aria-label',
                'disabled', 'readonly', 'checked', 'aria-checked', 'aria-selected',
                'input_value', 'text_value', 'input_checked', 'option_selected')
                if node.get(k) is not None} if rich or is_control else {}
            # Public DOM context only. No positive/negative labels affect ranking.
            parents = [{'tag': p.tag, 'text': snippet(p,160)}
                       for p in list(node.iterancestors())[:2] if p.tag not in {'html','body'}] if rich else []
            children = [{'tag': p.tag, 'text': snippet(p,80)}
                        for p in list(node)[:4]] if rich else []
            nodes.append({'node_id': str(node.get('backend_node_id')), 'tag': node.tag,
                          'text': snippet(node,240) if rich or is_control else '', 'attributes': attrs,
                          'parent_context': parents, 'child_context': children})
        # No filtering based on private positive/negative candidate labels.
        unique = {n['node_id']: n for n in nodes}
        controls = [n for n in unique.values() if n['tag'] in {'input', 'textarea', 'select'}]
        actual_hash = hashlib.sha256(raw).hexdigest()
        expected = self.expected_snapshot_hashes.get((task_id, index))
        if expected is not None and actual_hash != expected:
            raise OSError('Frozen source snapshot changed')
        return {'text': text[:6000], 'full_text': text, 'text_characters': len(text),
                'text_projection_truncated': len(text) > 6000,
                'form_controls': controls, 'nodes': list(unique.values()),
                'snapshot_sha256': actual_hash}

    def observation(self, task_id, index):
        page = self.page(task_id, index)
        result = {k: page[k] for k in ('text', 'text_characters', 'text_projection_truncated', 'snapshot_sha256')}
        result['form_controls'] = [{k:n[k] for k in ('node_id','tag','text','attributes')} for n in page['form_controls']]
        return result

    def candidates(self, task_id, index, query='', offset=0, limit=30):
        nodes = self.page(task_id, index, True)['nodes']
        if query:
            query = str(query).casefold()
            nodes = [n for n in nodes if query in compact(str(n)).casefold()]
        return {'total': len(nodes), 'offset': offset,
                'nodes': nodes[offset:offset + limit], 'more': offset + limit < len(nodes)}

    def ranked_candidates(self, task_id, index, query, limit=50):
        """Deterministic public lexical retrieval; retrieval miss remains a miss."""
        tokens = set(re.findall(r'[a-z0-9]{2,}', query.casefold()))
        def rank(node):
            own = set(re.findall(r'[a-z0-9]{2,}', compact(str({k:v for k,v in node.items() if k not in {'parent_context','child_context','node_id'}})).casefold()))
            context = set(re.findall(r'[a-z0-9]{2,}', compact(str(node['parent_context'])).casefold()))
            return (3*len(tokens & own)+len(tokens & context), node['tag'] in {'input','button','a','select','textarea'})
        nodes = sorted(self.page(task_id,index,True)['nodes'], key=rank, reverse=True)
        return {'nodes': nodes[:limit], 'total':len(nodes), 'retrieval_limit':limit,
                'retrieval':'fixed_public_lexical_ranker_no_gold_injection'}

    def prefix(self, task_id, checkpoint):
        t = self.tasks[task_id]
        # Every action is strictly BEFORE the checkpoint; never show its target.
        return [{'origin': 'recorded_human_prefix', 'workspace': 'CURRENT_TASK', 'observation': self.observation(task_id, i),
                 'action': t['steps'][i]['action']} for i in range(checkpoint)]

    def score_action(self, task_id, index, action):
        step = self.tasks[task_id]['steps'][index]
        element = str(action.get('node_id')) in step['positive_node_ids']
        op = str(action.get('op', '')).upper()
        value = action.get('value', '')
        f1 = operation_f1(step['operation']['op'], step['operation'].get('value', ''), op, value)
        public_ids = {node['node_id'] for node in self.page(task_id, index)['nodes']}
        return {'element_correct': element, 'operation_f1': f1,
                'step_correct': element and f1 == 1.0,
                'transition_supported': element and str(action.get('node_id')) in public_ids and op == step['operation']['op']
                                        and isinstance(value, str) and value == step['operation'].get('value', ''),
                'source_task_id': task_id, 'source_step': index}

    def audit(self, task_id):
        task = self.tasks[task_id]
        issues, hashes = [], []
        if not 2 <= len(task['steps']) <= 12:
            return {'usable': False, 'issues': ['trace_length_outside_2_12']}
        for i, step in enumerate(task['steps']):
            try:
                page = self.page(task_id, i)
                ids = {n['node_id'] for n in page['nodes']}
                if not step['positive_node_ids'] or not ids.intersection(step['positive_node_ids']):
                    issues.append({'step': i, 'reason': 'reference_target_missing_from_public_DOM'})
                if step['operation']['op'] not in {'CLICK', 'TYPE', 'SELECT'}:
                    issues.append({'step': i, 'reason': 'unsupported_reference_operation'})
                hashes.append(page['snapshot_sha256'])
            except (OSError, ValueError) as error:
                issues.append({'step': i, 'reason': type(error).__name__})
        return {'usable': not issues, 'issues': issues, 'snapshot_hashes': hashes,
                'steps': len(task['steps']), 'website': task['website'], 'split': task['split']}

    def source_summary(self):
        return {'tasks': len(self.tasks), 'splits': dict(Counter(t['split'] for t in self.tasks.values())),
                'index_sha256': file_digest(self.index)}
