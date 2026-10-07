"""Validate pilot cases; require independent human review in strict mode."""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from run_models import validate_cases


def read(path):
    if not path.is_file():
        raise ValueError(f'{path}: event dataset missing. Candidates are not event labels.')
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def validate_directory(folder, strict_review=False):
    for kind,name in [('main','single_event.jsonl'),('counterfactual','counterfactual.jsonl'),('multi','multi_event.jsonl')]:
        rows=read(folder/name)
        validate_cases(kind,rows)
        for case in rows:
            annotation=case.get('annotation',{})
            if strict_review or annotation.get('human_reviewed') is True:
                if annotation.get('human_reviewed') is not True or annotation.get('status')!='approved':
                    raise ValueError(f'{case["case_id"]}: Gold is not reviewed/approved')
                if len(set(annotation.get('reviewers',[])))<2:
                    raise ValueError(f'{case["case_id"]}: two independent reviewer IDs required')
            elif annotation.get('human_reviewed') is False:
                if annotation.get('status')!='draft_review_required' or annotation.get('reviewers'):
                    raise ValueError(f'{case["case_id"]}: pilot review status must be truthful')
            else:
                raise ValueError(f'{case["case_id"]}: human-review status must be explicitly declared')
            if annotation.get('provenance') not in ('recorded_human_demonstration','live_browser_observation'):
                raise ValueError(f'{case["case_id"]}: disclose the source of the actual pre-event history')
            if not annotation.get('evidence_steps') or not annotation.get('rationale'):
                raise ValueError(f'{case["case_id"]}: evidence steps and label rationale required')
            if case['state']['summary']!=case['trajectory'][-1]['observation']:
                raise ValueError(f'{case["case_id"]}: current observation mismatch')
            if annotation.get('ui_executed_after_event') is not False:
                raise ValueError(f'{case["case_id"]}: offline events must disclose that they were not executed')
            if kind=='multi':
                nodes=case['gold']['required_nodes']
                edges=case['gold']['precedence']
                indegree={node:0 for node in nodes}
                successors={node:[] for node in nodes}
                for first,last in edges:
                    indegree[last]+=1;successors[first].append(last)
                queue=[n for n in nodes if indegree[n]==0]
                visited=0
                while queue:
                    node=queue.pop();visited+=1
                    for successor in successors[node]:
                        indegree[successor]-=1
                        if indegree[successor]==0:queue.append(successor)
                if visited!=len(nodes):raise ValueError(f'{case["case_id"]}: cyclic schedule constraints')
        if kind=='counterfactual':
            groups=defaultdict(list)
            for case in rows:groups[(case['group_type'],case['group_id'])].append(case)
            for (group_type,uid),members in groups.items():
                if len(members)!=(3 if group_type=='semantic' else 2):
                    raise ValueError(f'{uid}: incomplete controlled group')
                first=members[0]
                if any(m.get('rules',[])!=first.get('rules',[]) for m in members):
                    raise ValueError(f'{uid}: workflow rules must remain identical within a controlled group')
                if group_type=='goal':
                    if len({m['goal'] for m in members})!=2 or any(m['trajectory']!=first['trajectory'] or m['event']!=first['event'] or m['state']!=first['state'] for m in members):
                        raise ValueError(f'{uid}: only the goal may change')
                elif group_type=='state':
                    if any(m['goal']!=first['goal'] or m['event']!=first['event'] for m in members):
                        raise ValueError(f'{uid}: goal/event must remain identical')
                else:
                    if any(m['goal']!=first['goal'] or m['trajectory']!=first['trajectory'] or m['state']!=first['state'] or m['gold']['decision']!=first['gold']['decision'] for m in members):
                        raise ValueError(f'{uid}: semantic paraphrases must retain context and label')
                    if any(m['event']['source']!=first['event']['source'] for m in members):
                        raise ValueError(f'{uid}: semantic paraphrases must retain the event source')
                if group_type!='semantic' and len({m['gold']['decision'] for m in members})!=2:
                    raise ValueError(f'{uid}: goal/state contrast must flip the label')
        print(f'Validated {kind}: {len(rows)} cases ({"strict review" if strict_review else "pilot mode; human review not asserted"})')
