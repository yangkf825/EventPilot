"""Actual-case conditional scores with explicit availability/unknown denominators."""
from __future__ import annotations
from collections import Counter
from urllib.parse import urlsplit
from run_online_v2 import (read_json,read_cases,write_json,write_csv,job_path,job_views,
                          audit_counterfactual_groups,CORE_COLUMNS)
from eventarena.online_v2_metrics import score_cases
from .casewise import POLICY,qualified


def export_availability(root,selected,names,args):
    all_rows=[];site_rows=[];cf_rows={'goal':[],'state':[],'semantic':[]}
    cohort_rows={}
    for experiment,cases in selected.items():
        if experiment=='final_intent':
            # Upfront revised tasks have no event checkpoint. Do not silently
            # exclude their runtime failures with a checkpoint-based filter.
            continue
        eligible=[];checked=0;reasons=Counter()
        for case in cases:
            source_id=case.get('original_case_id',case['case_id'])
            gate=read_json(root/'preparation_eligibility'/'repeat_0'/source_id/'eligibility.json')
            if gate and gate.get('availability_policy')==POLICY:
                checked+=1
                if qualified(gate):eligible.append(case)
                else:reasons[gate.get('reason','unknown_preparation_reason')]+=1
        eligible_ids={c['case_id'] for c in eligible}
        cohort_rows[experiment]={'registered':len(cases),'qualification_checked':checked,
             'qualified':len(eligible),'initially_unavailable':checked-len(eligible),
             'qualification_pending':len(cases)-checked,
             'runnable_coverage':len(eligible)/len(cases) if cases else None,
             'qualification_coverage':checked/len(cases) if cases else None,'failure_reasons':dict(reasons),
             'source_tasks':len({c.get('source_task_a',{}).get('task_id') for c in cases}),
             'qualified_source_tasks':len({c.get('source_task_a',{}).get('task_id') for c in eligible}),
             'source_websites':len({urlsplit(c['url']).hostname for c in cases}),
             'qualified_source_websites':len({urlsplit(c['url']).hostname for c in eligible})}
        cohort_rows[experiment]['eligible_label_counts']=dict(Counter(
            e['gold']['decision'] for c in eligible for e in c.get('events',[])))
        cohort_rows[experiment]['eligible_difficulty_counts']=dict(Counter(c.get('difficulty') for c in eligible))
        for name in names:
            for view in job_views(experiment):
                episodes=[]
                for c in eligible:
                    job={'experiment':experiment,'model':name,'view':view,'repeat':0,'case_id':c['case_id']}
                    episode=read_json(job_path(root,job)/'metric_record.json')
                    if episode:episodes.append(episode)
                if experiment=='counterfactual':audit_counterfactual_groups(eligible,episodes)
                metrics=score_cases(eligible,episodes,experiment,view)
                write_json(root/experiment/name/f'{view}_runnable_metrics.json',metrics)
                outcomes=metrics.get('denominators',{}).get('MicroESR',{})
                tasks=metrics.get('denominators',{}).get('TaskCompletion',{})
                row={'experiment':experiment,'model':name,'view':view,'score_scope':'common_initially_qualified_cases',
                     'RegisteredCases':len(cases),'QualificationChecked':checked,'EligibleCases':len(eligible),
                     'QualificationPending':len(cases)-checked,'RunnableCoverage':cohort_rows[experiment]['runnable_coverage'],
                     **{k:metrics.get(k) for k in CORE_COLUMNS[experiment]},
                     'SuccessfulEligibleCases':outcomes.get('successes',outcomes.get('n')),
                     'FailedEligibleCases':outcomes.get('failures'),'UnknownEligibleOutcomes':outcomes.get('unknown'),
                     'OutcomeCoverage':outcomes.get('coverage'),'EligibleSuccessRate':outcomes.get('rate'),
                     'DeterminateEligibleSuccessRate':outcomes.get('covered_rate'),
                     'EligibleSuccessLowerBound':outcomes.get('lower_bound'),'EligibleSuccessUpperBound':outcomes.get('upper_bound'),
                     'EligibleTaskCompletionRate':tasks.get('rate'),
                     'DeterminateEligibleTaskCompletionRate':tasks.get('covered_rate'),
                     'TaskCompletionN':tasks.get('N'),'UnknownTaskCompletions':tasks.get('unknown')}
                all_rows.append(row)
                if experiment=='counterfactual':
                    for kind,result in metrics.get('counterfactual',{}).get('types',{}).items():
                        cf_rows[kind].append({'model':name,'view':view,'type':kind,
                            **{k:v for k,v in result.items() if not isinstance(v,(dict,list))},
                            **{k:result.get('common_metrics',{}).get(k) for k in CORE_COLUMNS['main']},
                            'EligibleCases':result['case_counts']['N'],'EligibleGroups':result['group_counts']['N'],
                            'GroupCoverage':result['group_counts']['coverage']})
                for host in sorted({urlsplit(c['url']).hostname for c in cases}):
                    registered_site=[c for c in cases if urlsplit(c['url']).hostname==host]
                    site=[c for c in eligible if urlsplit(c['url']).hostname==host];ids={c['case_id'] for c in site}
                    m=score_cases(site,[e for e in episodes if e['case_id'] in ids],experiment,view)
                    o=m.get('denominators',{}).get('MicroESR',{})
                    site_rows.append({'experiment':experiment,'model':name,'view':view,'website':host,
                        'RegisteredCases':len(registered_site),'EligibleCases':len(site),
                        'RunnableCoverage':len(site)/len(registered_site),
                        'successes':o.get('successes',o.get('n')),'failures':o.get('failures'),'unknown':o.get('unknown'),
                        'EligibleSuccessRate':o.get('rate'),'DeterminateEligibleSuccessRate':o.get('covered_rate'),
                        'OutcomeCoverage':o.get('coverage')})
    write_csv(root/'runnable_comparison.csv',all_rows)
    write_csv(root/'runnable_website_results.csv',site_rows)
    for kind,rows in cf_rows.items():write_csv(root/f'runnable_counterfactual_{kind}.csv',rows)
    write_json(root/'availability_report.json',{'policy':POLICY,'cohorts':cohort_rows,
        'initial_eligibility_shared_across_all_models_and_views':True,'selection_reads_tested_model_scores':False,
        'definitions':{'RunnableCoverage':'initially qualified / all registered cases',
          'EligibleSuccessRate':'successes / all initially qualified cases; null if some runtime outcomes are unknown',
          'DeterminateEligibleSuccessRate':'successes / known outcomes among initially qualified cases; must report OutcomeCoverage',
          'OutcomeCoverage':'known outcomes / initially qualified cases'},
        'final_intent':'all registered upfront-revised tasks remain in its original table; no checkpoint filter',
        'interpretation':'Conditional performance on this verified task cohort, not all-web performance. Runtime unknowns are retained.'})
    return all_rows
