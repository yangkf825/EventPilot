"""Expose matched coverage; never subtract success rates on different subsets."""
import itertools


def paired_coverage(source,cases_by_experiment,models,repeats):
    from run_online_v2 import read_json,job_path,VIEWS
    rows=[]
    cases=cases_by_experiment.get('main',cases_by_experiment.get('ablation',[]))
    for model in models:
        def result(experiment,view,cid,repeat,outcome='episode_success'):
            job={'experiment':experiment,'view':view,'model':model,'case_id':cid,'repeat':repeat}
            e=read_json(job_path(source,job)/'metric_record.json',{})
            value=e.get('evaluation',{}).get(outcome)
            return value if isinstance(value,bool) and e.get('evaluation',{}).get('environment_valid') is not False else None
        if 'ablation' in cases_by_experiment:
            maps={view:{(c['case_id'],r):result('ablation',view,c['case_id'],r)
                        for c in cases_by_experiment['ablation'] for r in range(repeats)} for view in VIEWS}
            for left,right in itertools.combinations(VIEWS,2):
                shared=[k for k in maps[left] if maps[left][k] is not None and maps[right][k] is not None]
                rows.append({'model':model,'comparison':'ablation','left':left,'right':right,
                             'registered_pairs':len(maps[left]),'shared_known_pairs':len(shared),
                             'success_delta_left_minus_right':sum(int(maps[left][k])-int(maps[right][k]) for k in shared)/len(shared) if shared else None,
                             'scope':'descriptive_matched_subset_only_not_overall_causal_effect'})
            shared=[k for k in maps[VIEWS[0]] if all(maps[v][k] is not None for v in VIEWS)]
            rows.append({'model':model,'comparison':'ablation_all_four','registered_pairs':len(maps[VIEWS[0]]),
                         'shared_known_pairs':len(shared),'scope':'require_all_four_before_comparing_information_views'})
        if 'main' in cases_by_experiment and 'baseline' in cases_by_experiment:
            # Revised/withdrawn targets are not the same outcome as original A.
            compatible=[c for c in cases if c.get('gold',{}).get('follow_up') not in {'REPLAN','TERMINATE'}]
            pairs=[]
            for c in compatible:
                for repeat in range(repeats):
                    left=result('main','trajectory',c['case_id'],repeat,'task_success')
                    right=result('baseline','trajectory',c['case_id']+'_AONLY',repeat,'task_success')
                    if left is not None and right is not None:pairs.append((left,right,c['task_family_id']))
            rows.append({'model':model,'comparison':'main_no_event_unchanged_target','registered_pairs':len(compatible)*repeats,
                         'shared_known_pairs':len(pairs),'known_source_families':len({p[2] for p in pairs}),
                         'success_delta_left_minus_right':sum(int(p[0])-int(p[1]) for p in pairs)/len(pairs) if pairs else None,
                         'scope':'same_original_task_success_only_excludes_revised_or_withdrawn_targets'})
    return rows
