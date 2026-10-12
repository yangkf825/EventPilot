"""Compile authored event specifications against an immutable official corpus."""
from __future__ import annotations
import copy
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from eventarena.storage_io import atomic_write_json
from eventarena.online_v2_engine import match_trigger
from .corpus import DATASET, digest, load_corpus, read_jsonl, task_case, write_jsonl
from .evaluation import normalize_constraints

FILES = ('single_event.jsonl','counterfactual.jsonl','multi_event.jsonl')
LABELS = {'IGNORE','DEFER','INTERRUPT'}
FOLLOWUPS = {'HANDLE','REPLAN','TERMINATE'}
PREDICATES = {'url_host','url_contains','text_contains','text_any','control_present','control_value',
              'action_occurred','task_output_contains','url_path_not_in','event_stage'}
SAFE_ID = re.compile(r'^[A-Za-z0-9_-]+$')


def require(value, message):
    if not value:
        raise ValueError(message)


def source(task):
    return task['source'] | {'task_id':task['task_id'], 'original_goal':task['goal'],
                            'original_url':task['url'], 'same_as_original_interactive_task':True}


def validate_predicate(predicate):
    require(isinstance(predicate,dict),'Invalid trigger predicate')
    if 'all' in predicate or 'any' in predicate:
        terms=predicate.get('all',predicate.get('any'))
        require(isinstance(terms,list) and terms,'Empty compound trigger')
        for term in terms:validate_predicate(term)
    else:
        require(predicate.get('type',predicate.get('kind')) in PREDICATES,'Unknown semantic trigger predicate')


def compile_case(spec, index):
    task = index[spec['task_a_id']]
    case = task_case(task)
    cid = spec['case_id']
    require(isinstance(cid,str) and SAFE_ID.fullmatch(cid), 'Unsafe case ID')
    require(not any(spec.get(k) for k in ('trajectory','prefix_actions','prior_results','state_override')) and
            not spec.get('cf',{}).get('state_override'),cid+': authored execution/state logs are forbidden')
    case.update(case_id=cid, rules=copy.deepcopy(spec.get('rules', [])),
                task_family_id='official:' + task['task_id'], source_family_id='official:' + task['task_id'],
                trigger=copy.deepcopy(spec.get('trigger')), events=[],
                difficulty=spec.get('difficulty','unreviewed'),
                difficulty_mechanisms=copy.deepcopy(spec.get('difficulty_mechanisms',[])),
                split=spec.get('split','heldout_candidate'),
                annotation={'human_reviewed':spec.get('human_reviewed') is True,
                            'reviewers':spec.get('reviewers',[]), 'review_evidence':spec.get('review_evidence',[]),
                            'full_trajectory_origin':'runtime_browser_observations_and_actions_only'})
    require(case['trigger'], cid + ': author must register an observed semantic trigger')
    require(case['trigger'].get('min_actor_progress_actions',0) >= 1,
            cid+': trigger must require actual task progress, not the initial homepage')
    terms=case['trigger'].get('all',[])
    require(any(t.get('type') in {'text_any','text_contains','control_value'} for t in terms),
            cid+': task-bearing text/control condition required')
    validate_predicate(case['trigger'])
    # Execute predicate parsing against an empty real-state envelope to catch
    # unknown predicate names, without fabricating an observed checkpoint.
    match_trigger(case['trigger'], {'url':task['url'],'text':'','candidates':[]})
    if spec.get('checkpoint_audit_policy'):
        from eventarena.browser import validate_checkpoint_policy
        case['checkpoint_audit_policy']=copy.deepcopy(validate_checkpoint_policy(spec['checkpoint_audit_policy']))
    if spec.get('verification_checks'):
        case['verification']['CURRENT_TASK']['checks']=copy.deepcopy(spec['verification_checks'])
    decisions = {}
    for event_spec in spec['events']:
        eid = event_spec['id']
        require(SAFE_ID.fullmatch(eid) and eid!='A',cid+': unsafe event ID')
        gold = copy.deepcopy(event_spec.get('gold',{}))
        require(gold.get('decision') in LABELS, cid+': author must provide Gold')
        require((gold.get('follow_up') in FOLLOWUPS) if gold['decision']=='INTERRUPT'
                else gold.get('follow_up') is None, cid+': follow-up is conditional on INTERRUPT')
        require(event_spec.get('label_reason'),cid+': missing label rationale')
        event={'id':eid, 'source':event_spec['source'], 'text':event_spec['text'],
               'gold':gold, 'label_reason':event_spec['label_reason'],
               'authored_event':True, 'provenance':{'type':'author_added_event','official_event':False}}
        kind=event_spec['kind']
        if kind=='research_task':
            other=index[event_spec['task_b_id']]
            require(other['task_id']!=task['task_id'],cid+': B must be another official task')
            event.update(source_task=source(other))
            event['execution']={'kind':kind,'goal':other['goal'],'url':other['url'],
                                'source_task_id':other['task_id'],
                                'verification':{'method':'llm_judge','goal':other['goal'],
                                                'checks':copy.deepcopy(event_spec.get('verification_checks',[]))}}
            event['provenance']['task_b_id']=other['task_id']
            event['relation']=event_spec.get('relation','author_declared')
            case['verification'][eid]=copy.deepcopy(event['execution']['verification'])
        elif kind=='update_goal':
            # Revisions are author extensions, not a second unchanged official A.
            updated=event_spec.get('updated_goal')
            require(isinstance(updated,str) and updated.strip() and updated!=task['goal'],cid+': revision missing')
            constraints=event_spec.get('effective_constraints',[])
            normalize_constraints(constraints)  # Validate before any paid/browser work; retain registered bytes.
            require({v.get('origin') for v in constraints}>={'new','retained'},cid+': register new and retained constraints')
            verifier={'method':'llm_judge','goal':updated,'checks':copy.deepcopy(event_spec.get('verification_checks',[])),
                      'effective_constraints':copy.deepcopy(constraints)}
            event['execution']={'kind':kind,'updated_goal':updated,'updated_verification':verifier}
            case['verification']['CURRENT_TASK_UPDATED']=copy.deepcopy(verifier)
            require(gold=={'decision':'INTERRUPT','follow_up':'REPLAN'},cid+': revision Gold inconsistent')
        elif kind=='cancel':
            event['execution']={'kind':kind}
            require(gold=={'decision':'INTERRUPT','follow_up':'TERMINATE'},cid+': cancellation Gold inconsistent')
        elif kind=='none':
            event['execution']={'kind':kind}
            require(gold['decision']=='IGNORE',cid+': no executable obligation requires IGNORE')
        else:
            raise ValueError(cid+': unsupported event kind')
        if event_spec.get('trigger'):
            validate_predicate(event_spec['trigger'])
            event['trigger']=copy.deepcopy(event_spec['trigger'])
        case['events'].append(event)
        require(eid not in decisions,cid+': duplicate event')
        decisions[eid]=gold
    require(bool(decisions),cid+': no events')
    case['gold']={'decisions':decisions,
                  'precedence':copy.deepcopy(spec.get('precedence',[])),
                  'required_nodes':copy.deepcopy(spec.get('required_nodes',[]))}
    case['baseline_verification']=copy.deepcopy(case['verification'])
    case['website_validity']={'execution_verified':False,'basis':'requires_live_A_B_and_trigger_validation'}
    case['evaluation_registration']={'eligibility_must_not_use_actor_scores':True,
                                    'task_goal_policy':'unchanged_official_A_until_authorized_event_revision'}
    case['author_spec']=copy.deepcopy(spec)
    if spec.get('checkpoint_group_id'):
        require(SAFE_ID.fullmatch(spec['checkpoint_group_id']),cid+': unsafe checkpoint group')
        case['checkpoint_group_id']=spec['checkpoint_group_id']
    if spec.get('group_type'):
        case.update(group_type=spec['group_type'],group_id=spec['group_id'],cf=copy.deepcopy(spec.get('cf',{})))
        for key in ('checkpoint_group_id','checkpoint_navigation_goal','controlled_user_history','state_variant'):
            if key in spec:
                case[key]=copy.deepcopy(spec[key])
        require(not spec.get('controlled_user_history'),cid+': fabricated instruction history is unsupported')
        case['setup_tasks']=[]
        for setup in spec.get('setup_tasks',[]):
            require(setup.get('task_id') in index and SAFE_ID.fullmatch(setup.get('id','')),
                    cid+': setup must reference an exact official task')
            t=index[setup['task_id']]
            case['setup_tasks'].append({'id':setup['id'],'goal':t['goal'],'url':t['url'],
                                       'source_task':source(t),'alias_event_id':setup['alias_event_id'],
                                       'verification':{'method':'llm_judge','goal':t['goal'],'checks':[]}})
    if spec.get('multi_arrival_mode'):
        case['multi_arrival_mode']=spec['multi_arrival_mode']
        require(case['multi_arrival_mode'] in {'batch','stream'}, cid+': unknown arrival mode')
        if case['multi_arrival_mode']=='stream':
            require(any(e.get('trigger',{}).get('type')=='event_stage' for e in case['events']),
                    cid+': streaming cases require registered later event triggers')
        case['runtime_protocol']={'multi_case':True,'report_all_known_events':True,
                                  'future_event_goals_not_visible':True}
    return case


def compile_extension(corpus_dir, spec_path, output):
    tasks,manifest=load_corpus(corpus_dir)
    specs=json.loads(Path(spec_path).read_text(encoding='utf-8'))
    require(isinstance(specs,list) and specs,'Author specs must be a nonempty array')
    index={t['task_id']:t for t in tasks}
    compiled={filename:[] for filename in FILES}
    ids=set()
    for spec in specs:
        require(spec.get('task_a_id') in index,'Unknown official task A')
        require(spec.get('experiment','main') in {'main','counterfactual','multi'},'Unknown authoring experiment')
        for e in spec.get('events',[]):
            require(e.get('kind')!='research_task' or e.get('task_b_id') in index,'Unknown official task B')
        case=compile_case(spec,index)
        require(case['case_id'] not in ids,'Duplicate case ID');ids.add(case['case_id'])
        filename={'main':'single_event.jsonl','counterfactual':'counterfactual.jsonl','multi':'multi_event.jsonl'}[spec.get('experiment','main')]
        require((len(case['events'])>1) if filename=='multi_event.jsonl' else len(case['events'])==1,
                'Event count inconsistent with experiment')
        compiled[filename].append(case)
    output=Path(output)
    require(not (output/'manifest.json').exists(),'Choose a new extension directory; do not silently overwrite registrations')
    for filename,rows in compiled.items():
        write_jsonl(output/filename,rows)
    write_jsonl(output/'source_task_index.jsonl',tasks)
    atomic_write_json(output/'author_specs.json',specs)
    registration={'schema_version':'official_online_extension_v1','source':manifest,
                  'files':{filename:{'sha256':digest((output/filename).read_bytes()),'count':len(rows)}
                           for filename,rows in compiled.items()},
                  'source_task_index_sha256':digest((output/'source_task_index.jsonl').read_bytes()),
                  'author_specs_sha256':digest((output/'author_specs.json').read_bytes()),
                  'gold_human_reviewed':all(s.get('human_reviewed') is True for s in specs),
                  'readiness':'author_candidate_not_validated_execution',
                  'official_goals_preserved':True,'official_leaderboard_comparable':False}
    atomic_write_json(output/'manifest.json',registration)
    validate_extension(output)
    return registration


def validate_extension(directory, strict_review=False):
    directory=Path(directory)
    manifest=json.loads((directory/'manifest.json').read_text())
    require(manifest.get('schema_version')=='official_online_extension_v1','Wrong extension protocol')
    src=directory/'source_task_index.jsonl'
    require(digest(src.read_bytes())==manifest['source_task_index_sha256'],'Official task index changed')
    index={t['task_id']:t for t in read_jsonl(src)}
    specs=directory/'author_specs.json'
    require(digest(specs.read_bytes())==manifest['author_specs_sha256'],'Author specifications changed')
    registered={s['case_id']:s for s in json.loads(specs.read_text())}
    all_cases=[]
    for filename in FILES:
        file=directory/filename
        require(digest(file.read_bytes())==manifest['files'][filename]['sha256'],'Extension file hash changed')
        for c in read_jsonl(file):
            require(c==compile_case(registered[c['case_id']],index),'Case diverged from its official source/specification')
            if strict_review:
                ann=c['annotation']
                require(ann['human_reviewed'] and len(set(ann['reviewers']))>=2 and ann['review_evidence'],
                        c['case_id']+': independent Gold reviews missing')
                for evidence in ann['review_evidence']:
                    p=directory/'review_evidence'/evidence['filename']
                    require(p.is_file() and digest(p.read_bytes())==evidence['sha256'],'Review evidence missing/changed')
            all_cases.append(c)
    groups=defaultdict(list)
    for c in all_cases:
        if c.get('group_type'):
            groups[(c['group_type'],c['group_id'])].append(c)
    for (kind,gid),members in groups.items():
        require(kind in {'goal','state','semantic'},gid+': invalid counterfactual type')
        require(len(members)==(3 if kind=='semantic' else 2),gid+': incomplete counterfactual group')
        require(len({c['url'] for c in members})==1,gid+': start URL must stay fixed')
        require(all(c['rules']==members[0]['rules'] and c['trigger']==members[0]['trigger'] and
                    c.get('checkpoint_audit_policy')==members[0].get('checkpoint_audit_policy') for c in members),
                gid+': rules/trigger/audit contract changed across counterfactual members')
        require(all(c['cf'].get('runtime_pair_audit_required') is True for c in members),gid+': runtime invariance audit missing')
        if kind=='goal':
            require(len({c['goal'] for c in members})==2,gid+': goal intervention must change the goal')
            require(members[0]['events'][0]['text']==members[1]['events'][0]['text'] and
                    members[0]['events'][0]['execution']==members[1]['events'][0]['execution'],gid+': goal pair changed event')
        elif kind=='state':
            require(members[0]['goal']==members[1]['goal'] and members[0]['events'][0]['text']==members[1]['events'][0]['text'] and
                    members[0]['events'][0]['execution']==members[1]['events'][0]['execution'],gid+': state pair changed goal/event')
            hard_pair={c.get('state_variant') for c in members}=={'target_completed','related_completed'}
            require(all(c.get('setup_tasks') for c in members) if hard_pair else
                    sum(bool(c.get('setup_tasks')) for c in members)==1,gid+': state pair requires genuine controlled prior lookups')
            require(all(c['cf'].get('state_fields')==['prior_results'] for c in members),gid+': unsupported state intervention')
        else:
            require(len({c['goal'] for c in members})==1 and all(c['gold']==members[0]['gold'] and
                    c['events'][0]['execution']==members[0]['events'][0]['execution'] for c in members),
                    gid+': semantic group changed goal/Gold/execution')
            require(len({c['events'][0]['text'] for c in members})==3,gid+': semantic wording must vary')
    return {'cases':len(all_cases),'counts':{k:v['count'] for k,v in manifest['files'].items()},
            'human_reviewed':manifest['gold_human_reviewed'],'source_dataset':DATASET}


def pair_candidates(tasks, available_urls=None):
    """Same-host candidates are review leads, never inferred dependencies or Gold."""
    grouped=defaultdict(list)
    for t in tasks:
        if available_urls is None or t['url'] in available_urls:
            grouped[urlsplit(t['url']).hostname.removeprefix('www.')].append(t)
    candidates=[]
    stop={'the','and','for','with','that','from','find','show','browse','page','open','most','first','available','current'}
    def words(task):
        return {w for w in re.findall(r'[a-z0-9]+',task['goal'].lower()) if len(w)>2 and w not in stop}
    for host,members in sorted(grouped.items()):
        for i,a in enumerate(members):
            for b in members[i+1:]:
                overlap=sorted(words(a)&words(b))
                candidates.append({'task_a_id':a['task_id'],'task_b_id':b['task_id'],'host':host,
                                   'goal_a':a['goal'],'goal_b':b['goal'],'shared_terms':overlap,
                                   'relation':'same_website_candidate_needs_author_review',
                                   'dependency_verified':False,'gold':None})
    return sorted(candidates,key=lambda c:(-len(c['shared_terms']),c['task_a_id'],c['task_b_id']))
