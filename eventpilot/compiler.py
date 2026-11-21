"""Compile the approved review into a frozen English event dataset.

The workbook is read only. The compiler never infers labels from event wording:
reviewed structured operations and reviewed Gold are its only annotation source.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = 'eventpilot.reviewed.11'
CJK = re.compile(r'[\u3400-\u9fff]')
ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def jsonl(path, rows):
    Path(path).write_text(''.join(json.dumps(r, ensure_ascii=False, allow_nan=False)+'\n' for r in rows), encoding='utf-8')


def loadl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def read_workbook(path):
    """Read values from a one-sheet XLSX, without an Excel session or mutation."""
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        shared = []
        if 'xl/sharedStrings.xml' in names:
            shared = [''.join(n.itertext()) for n in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('m:si', ns)]
        sheets = [n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)]
        if len(sheets) != 1:
            raise ValueError('The approved review must have one worksheet')
        matrix = []
        for row in ET.fromstring(archive.read(sheets[0])).findall('.//m:sheetData/m:row', ns):
            values = [None]*55
            for cell in row.findall('m:c', ns):
                coordinate = cell.get('r', '')
                letters = re.match('[A-Z]+', coordinate).group()
                index = 0
                for c in letters:
                    index = index*26+ord(c)-64
                kind = cell.get('t')
                node = cell.find('m:v', ns)
                value = node.text if node is not None else None
                if kind == 's':
                    value = shared[int(value)] if value is not None else ''
                elif kind == 'inlineStr':
                    value = ''.join(cell.find('m:is', ns).itertext())
                elif value is not None and kind != 'str':
                    try:
                        value = float(value) if '.' in value else int(value)
                    except ValueError:
                        pass
                if index <= 55:
                    values[index-1] = value
            matrix.append(values)
    return {'headers': matrix[0], 'rows': [r for r in matrix[1:] if r[0]]}


def english_private(value):
    """Retain executable facts exactly; move non-English audit prose to XLSX."""
    if isinstance(value, str):
        if not CJK.search(value):
            return value
        if value.startswith('是否已经 '):
            return 'Whether the agent has already '+value[len('是否已经 '):]
        return 'Additional reviewer commentary is retained in the approved workbook.'
    if isinstance(value, list):
        return [english_private(v) for v in value]
    if isinstance(value, dict):
        return {k: english_private(v) for k, v in value.items()}
    return value


def snapshot(tasks, lengths, checkpoint, required, cancelled=(), edges=()):
    required, cancelled = set(required), set(cancelled)
    completed = {'A'} if checkpoint == lengths['A'] else set()
    registered = required|cancelled|{'A'}
    pointers = {r: checkpoint if r == 'A' else 0 for r in tasks}
    pending = required-cancelled-completed
    return {
        'active_workspace': 'A',
        'task_status': {r: 'completed' if r in completed else 'cancelled' if r in cancelled else 'active' if r=='A' else 'not_started' for r in sorted(registered)},
        'active_tasks': sorted(pending), 'required_tasks': sorted(required),
        'cancelled_tasks': sorted(cancelled-completed), 'cancelled_future_work': sorted(cancelled),
        'completed_tasks': sorted(completed), 'precedence_constraints': [list(e) for e in edges],
        'next_allowed_tasks': sorted(r for r in pending if not any(y==r and x in pending for x,y in edges)),
        'recorded_pointers': pointers,
    }


def single_stage(tasks, lengths, checkpoint, branch, initial_roles):
    initial_edges = [('A', 'B')] if 'B' in initial_roles else []
    pre = snapshot(tasks, lengths, checkpoint, initial_roles, edges=initial_edges)
    ops, cancelled, edges = [], [], []
    if branch == 'IGNORE':
        required = list(initial_roles)
        ops = [{'op': 'noop'}]
        edges = initial_edges
    elif branch == 'DEFER':
        required = ['A', 'B']
        ops = ([] if 'B' in initial_roles else [{'op': 'add', 'task': 'B'}])+[{'op':'order','tasks':['A','B']}]
        edges = [('A','B')]
    elif branch == 'HANDLE':
        required = ['A','B']
        ops = ([] if 'B' in initial_roles else [{'op':'add','task':'B'}])+[{'op':'order','tasks':['B','A']}]
        edges = [('B','A')]
    elif branch == 'REPLAN':
        required = ['B']; cancelled = ['A']
        ops = ([] if 'B' in initial_roles else [{'op':'add','task':'B'}])+[{'op':'cancel','tasks':['A']},{'op':'order','tasks':['B']}]
    elif branch == 'TERMINATE':
        required = []; cancelled = sorted(initial_roles)
        ops = [{'op':'cancel','tasks':cancelled},{'op':'order','tasks':[]}]
    else:
        raise ValueError('Unknown reviewed branch '+str(branch))
    post = snapshot(tasks, lengths, checkpoint, required, cancelled, edges)
    return {'event_id':'E1','pre_state':pre,'post_state':post,'post_verification_state':copy.deepcopy(post),
            'semantic_operations':ops,'allowed_branches':[branch],'unique_coarse_gold':True,
            'condition':None,'coarse_score_scope':'unique','mandatory_first_actions':[]}, required


def public_case(case):
    result = {k:copy.deepcopy(v) for k,v in case.items() if k not in {'private','review','provenance'}}
    # Delivery details may contain private reference controls. Public release
    # records describe messages, not the private solution/reference path.
    for event in result['events']:
        event.pop('arrival', None)
    return result


def assert_english(value, path=''):
    if isinstance(value, str) and CJK.search(value):
        raise ValueError('Non-English CJK text at '+path+': '+value[:100])
    if isinstance(value, dict):
        for key, child in value.items():
            assert_english(key, path+'.<key>')
            assert_english(child, path+'.'+key)
    if isinstance(value, list):
        for i, child in enumerate(value):
            assert_english(child, path+f'[{i}]')


def validate(data_dir=ROOT/'data'):
    data_dir = Path(data_dir)
    manifest = json.loads((data_dir/'manifest.json').read_text(encoding='utf-8'))
    spec = json.loads((data_dir/'authoring/reviewed_spec.en.json').read_text(encoding='utf-8'))
    source_index_hash = spec.get('source_index_sha256')
    if not isinstance(source_index_hash,str) or not re.fullmatch('[a-f0-9]{64}',source_index_hash):
        raise ValueError('A frozen original source-index hash is required')
    if manifest.get('source',{}).get('index_sha256') != source_index_hash:
        raise ValueError('Frozen original source-index identity changed')
    cases, baselines = loadl(data_dir/'cases.jsonl'), loadl(data_dir/'baselines.jsonl')
    sources = json.loads((data_dir/'sources.json').read_text(encoding='utf-8'))
    if len(cases) != 460 or len({c['case_id'] for c in cases}) != 460:
        raise ValueError('Expected exactly 460 unique reviewed cases')
    expected = {'main':200,'goal':50,'state':50,'semantic':60,'multi':100}
    counts = Counter(c['cf_type'] or c['experiment'] for c in cases)
    if counts != expected:
        raise ValueError('Reviewed case allocation changed: '+str(counts))
    if Counter(c['private']['stages'][0]['allowed_branches'][0] for c in cases if c['experiment']=='main') != {b:40 for b in ('IGNORE','DEFER','HANDLE','REPLAN','TERMINATE')}:
        raise ValueError('The reviewed Main class balance changed')
    if Counter(c['private']['stages'][0]['allowed_branches'][0] for c in cases if c['cf_type']=='semantic') != {b:12 for b in ('IGNORE','DEFER','HANDLE','REPLAN','TERMINATE')}:
        raise ValueError('The reviewed Semantic class balance changed')
    groups = defaultdict(list)
    for case in cases:
        assert_english(case, case['case_id'])
        if not 0 <= case['checkpoint'] <= len(sources[case['tasks']['A']['source_task_id']]['steps']):
            raise ValueError('Invalid checkpoint '+case['case_id'])
        for role, task in case['tasks'].items():
            tid = task['source_task_id']
            if task['goal'] != sources[tid]['goal']:
                raise ValueError('Source goal changed: '+case['case_id']+'/'+role)
            source = sources[tid]
            if len(source['audit']['snapshot_hashes']) != len(source['steps']):
                raise ValueError('Incomplete frozen source hashes: '+tid)
        if not set(case['initial_registered_roles']) <= set(case['tasks']):
            raise ValueError('Unknown initial role '+case['case_id'])
        if case['cf_type']:
            groups[(case['cf_type'],case['group_id'])].append(case)
        if len(case['events']) != len(case['private']['stages']):
            raise ValueError('Event/stage mismatch '+case['case_id'])
        for event, stage in zip(case['events'], case['private']['stages']):
            if event['id'] != stage['event_id'] or event['source'] not in {'user','website'}:
                raise ValueError('Event identity/source mismatch '+case['case_id'])
            if not stage['allowed_branches'] or not set(stage['allowed_branches']) <= {'IGNORE','DEFER','HANDLE','REPLAN','TERMINATE'}:
                raise ValueError('Invalid reviewed Gold set '+case['case_id'])
            for name in ('pre_state','post_state','post_verification_state'):
                state=stage.get(name)
                if state is None:
                    continue
                if not set(state['required_tasks']) <= set(case['tasks']):
                    raise ValueError('Unknown required task '+case['case_id'])
                for role,pointer in state['recorded_pointers'].items():
                    if role not in case['tasks'] or not 0 <= pointer <= len(sources[case['tasks'][role]['source_task_id']]['steps']):
                        raise ValueError('Invalid audited state pointer '+case['case_id'])
                pending=set(state['active_tasks']);todo=set(pending)
                while todo:
                    roots={r for r in todo if not any(y==r and x in todo for x,y in state['precedence_constraints'])}
                    if not roots:
                        raise ValueError('Cyclic reviewed task order '+case['case_id'])
                    todo-=roots
    for (kind,group), members in groups.items():
        if len(members) != (3 if kind=='semantic' else 2):
            raise ValueError('Broken counterfactual group '+group)
        if kind in {'goal','state'} and len({c['events'][0]['text'] for c in members}) != 1:
            raise ValueError('Paired event is not identical '+group)
        if kind=='goal' and (len({c['tasks']['A']['source_task_id'] for c in members}) != 2 or len({c['tasks']['B']['source_task_id'] for c in members}) != 1):
            raise ValueError('Goal pair source control changed '+group)
        if kind in {'state','semantic'} and len({digest(c['tasks']) for c in members}) != 1:
            raise ValueError('State/semantic task identity changed '+group)
        if kind=='semantic' and len({digest(c['private']['stages']) for c in members}) != 1:
            raise ValueError('Semantic intent/stage differs '+group)
    payload = {'cases':cases,'baselines':baselines,'sources':sources}
    if digest(payload) != manifest['dataset_digest']:
        raise ValueError('Frozen dataset digest changed')
    if digest(sources) != manifest['sources_sha256']:
        raise ValueError('Frozen source catalog changed')
    for path, expected_hash in manifest['files'].items():
        if file_hash(data_dir/path) != expected_hash:
            raise ValueError('Frozen data file changed: '+path)
    publics = loadl(data_dir/'public_cases.jsonl')
    if publics != [public_case(c) for c in cases]:
        raise ValueError('Public/private projection mismatch')
    if len(baselines) != len({t['source_task_id'] for c in cases for t in c['tasks'].values()}):
        raise ValueError('Missing unique source controls')
    baseline_ids={b['case_id'] for b in baselines}
    if len(baseline_ids)!=len(baselines):
        raise ValueError('Duplicate independent baseline')
    for control in baselines:
        assert_english(control, control['case_id'])
        tid=control['tasks']['A']['source_task_id']
        if control['case_id']!='BASE_'+tid or control['events'] or control['checkpoint']!=0:
            raise ValueError('Invalid independent source control')
    pairs=json.loads((data_dir/'pair_registry.json').read_text(encoding='utf-8'))
    if len(pairs)!=len(cases) or {p['case_id'] for p in pairs}!={c['case_id'] for c in cases}:
        raise ValueError('Incomplete case-to-baseline pairing')
    for pair in pairs:
        if not set(pair['task_baseline_ids'].values())<=baseline_ids:
            raise ValueError('Unknown paired source control')
    audit = {'valid':True,'case_count':len(cases),'baseline_count':len(baselines),'counts':dict(counts),
             'groups':dict(Counter(k for k,_ in groups)),'dataset_digest':manifest['dataset_digest'],
             'english_model_fields':True,'review_method':'single_user_workbook_approval','agreement':None}
    return audit


def build(data_dir=ROOT/'data'):
    """Rebuild from the included frozen English specification, without XLSX/API."""
    data_dir = Path(data_dir)
    spec = json.loads((data_dir/'authoring/reviewed_spec.en.json').read_text(encoding='utf-8'))
    sources = json.loads((data_dir/'sources.json').read_text(encoding='utf-8'))
    cases = spec['cases']
    for case in cases:
        assert_english(case, case['case_id'])
    baseline_sources = sorted({t['source_task_id'] for c in cases for t in c['tasks'].values()})
    baselines = []
    for tid in baseline_sources:
        task = sources[tid]
        baselines.append({'schema_version':SCHEMA,'case_id':'BASE_'+tid,'experiment':'baseline','cf_type':None,
          'group_id':None,'member':'independent','layer':'control','subtype':'no_event','website':task['website'],
          'checkpoint':0,'tasks':{'A':{'source_task_id':tid,'goal':task['goal'],'website':task['website'],'source_split':task['split']}},
          'initial_user_request':task['goal'],'initial_registered_roles':['A'],'events':[],
          'private':{'stages':[],'gold':{'required_roles':['A']},'source_lengths':{'A':len(task['steps'])}},
          'review':{'human_approved':True,'method':'unmodified_source_task_control','agreement':None}})
    pair_registry = []
    for case in cases:
        tids = {r:'BASE_'+t['source_task_id'] for r,t in case['tasks'].items()}
        ever_required = set(case['initial_registered_roles'])
        for stage in case['private']['stages']:
            for name in ('pre_state','post_state','post_verification_state'):
                ever_required.update(stage.get(name,{}).get('required_tasks',[]))
        row = {'case_id':case['case_id'],'experiment':case['experiment'],'cf_type':case['cf_type'],
               'subtype':case['subtype'],'group_id':case['group_id'],'cf_group_id':case['group_id'] if case['cf_type'] else None,
               'A_baseline_id':tids['A'],'task_baseline_ids':tids,
               'B_baseline_ids':{r:b for r,b in tids.items() if r!='A' and r in ever_required},
               'A_delta_applicable':'A' in case['private']['gold'].get('required_roles',[]),
               'reviewed_case_identity':case['case_id']}
        pair_registry.append(row)
    jsonl(data_dir/'cases.jsonl', cases)
    jsonl(data_dir/'public_cases.jsonl', [public_case(c) for c in cases])
    jsonl(data_dir/'baselines.jsonl', baselines)
    dump(data_dir/'pair_registry.json',pair_registry)
    cf_registry = defaultdict(list)
    for case in cases:
        if case['cf_type']:
            cf_registry[case['cf_type']+'/'+case['group_id']].append(case['case_id'])
    dump(data_dir/'counterfactual_groups.json',dict(cf_registry))
    counts = dict(Counter(c['cf_type'] or c['experiment'] for c in cases))
    files = ['cases.jsonl','public_cases.jsonl','baselines.jsonl','pair_registry.json','counterfactual_groups.json',
             'sources.json','authoring/reviewed_spec.en.json','authoring/translation_audit.json']
    manifest = {'schema_version':SCHEMA,'dataset_version':'11.0.0-reviewed-20261105','case_count':460,
      'baseline_count':len(baselines),'experiment_counts':counts,
      'event_count':sum(len(c['events']) for c in cases),'main_ablation_shared':True,
      'multi_delivery_variants':['sequential','batch','sequential_no_action'],
      'final_intent':'CAN subset of Multi-event; counted once in 460 reviewed cases',
      'sources_sha256':digest(sources),'dataset_digest':digest({'cases':cases,'baselines':baselines,'sources':sources}),
      'source':{'index_sha256':spec['source_index_sha256']},
      'workbook_sha256':spec['workbook_sha256'],'source_catalog_origin_sha256':spec['source_catalog_origin_sha256'],
      'source_task_count':len(baseline_sources),'source_catalog_task_count':len(sources),
      'approval':{'human_approved':True,'scope':'reviewed workbook content and annotations',
                  'method':'single_user_workbook_approval','approval_date':'2026-11-05','agreement':None,
                  'english_translations_independently_reviewed':False},
      'language':'English task/event/prompt fields; original DOM assets remain unmodified',
      'execution_scope':'staged_event_conditioned_teacher_forced_actions_and_control',
      'real_website_outcome_evaluated':False,'files':{p:file_hash(data_dir/p) for p in files}}
    dump(data_dir/'manifest.json',manifest)
    audit = validate(data_dir)
    dump(data_dir/'validation.json',audit)
    return manifest
