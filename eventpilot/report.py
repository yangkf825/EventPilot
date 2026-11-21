"""Complete experiment tables from immutable registration and closed records.

No model APIs or browser access are needed for export. Missing records remain in
the denominators. Main and Event + Goal + State ablation share one record.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
import csv
import os
from pathlib import Path

from .io import read_json, read_jsonl, write_json
from .metrics import (INFRA, VIEWS, cluster_bootstrap, decision_records, native_result,
                      numeric_summary, score_group, stages_for, summarize)

SCOPE = "offline event-conditioned teacher-forced predictions; no backend outcomes"
COMMON = ("Acc", "MacroF1", "MacroF1Supported", "FIR", "MIR", "FollowupAcc", "FollowupF1",
          "FollowupF1Supported", "JointFUAcc", "MicroESR", "MacroESR", "JointReplayESR", "ControlESR",
          "EHS", "TaskCompletion", "I_SR", "D_SR", "H_SR", "R_SR", "T_SR", "PTAR", "RR", "RCR",
          "RAR", "UGS", "ECS", "TermCR", "ICR", "EDR", "OTS", "DER", "DES", "ETS", "CancelCompliance",
          "Coverage", "Exposure", "decision_valid_rate", "api_ok_rate", "BranchAcc", "SetAcceptance", "VerificationAcc", "InspectionCoverage")
COUNTERFACTUAL = ("OverallAcc", "CFA", "SFA", "SemanticRobustAccuracy")
MULTI = ("EventF1", "CompleteCaseAcc", "CompleteBranchCaseAcc", "PriorityAcc", "PriorityCaseAcc",
         "PriorityStageAcc", "CriticalEventRecall", "ActualCriticalEventRecall", "FalseInterruptionRate",
         "ScheduleExactMatch", "DeclaredScheduleExactMatch", "ActualScheduleExactMatch", "ActualPriorityAcc", "ActualPriorityCaseAcc")
NATIVE = ("ElementAcc", "OperationF1", "StepSR", "TaskSR", "AttemptCoverage", "Coverage", "CandidateRecall",
          "AttemptedElementAcc", "AttemptedOperationF1", "AttemptedStepSR")


def atomic_csv(path, rows, fields=None):
    path = Path(path)
    rows = list(rows)
    fields = fields or list(dict.fromkeys(k for row in rows for k in row)) or ["no_registered_opportunities"]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _jsonl(path):
    return list(read_jsonl(path)) if path.exists() else []


def _fallback_scopes(case, sources, delivery):
    """Read-only registration fallback; the engine helper is authoritative."""
    lengths = case.get("private", {}).get("source_lengths", {})
    tasks = case.get("tasks", {})
    private = case.get("private", {})
    gold = private.get("gold", {})
    if delivery in {"batch", "sequential_no_action"}:
        gold = private.get("batch_control", {}).get("gold", gold)
        final = private.get("batch_control", {}).get("final_state", gold.get("final_state", {}))
    else:
        final = gold.get("final_state", {})
    required = set(gold.get("required_roles", final.get("required_tasks", [])))
    if not case.get("events"):
        required = {"A"}
    before_cancel = defaultdict(set)
    for event in case.get("events", []):
        for command in event.get("arrival", {}).get("after_reference_controls", []):
            if command.get("cmd") == "reference_step":
                before_cancel[command["task"]].add(command["source_step"])
    for command in gold.get("reference_suffix_controls", []):
        if command.get("cmd") == "reference_step":
            before_cancel[command["task"]].add(command["source_step"])
    scopes = []
    for role, task in tasks.items():
        tid = task["source_task_id"]
        n = lengths.get(role, len(sources.get(tid, {}).get("steps", [])))
        cp = case.get("checkpoint", 0) if role == "A" else 0
        if role == "A":
            specs = [("A_pre", list(range(cp)), True),
                     ("A_post", list(range(cp, n)) if role in required else sorted(before_cancel[role]), role in required or bool(before_cancel[role])),
                     ("A_full", list(range(n)), role in required)]
        else:
            indexes = list(range(n)) if role in required else sorted(before_cancel[role])
            specs = [(role, indexes, bool(indexes))]
        for name, indexes, applicable in specs:
            if indexes:
                scopes.append({"scope": name, "workspace": role, "source_task_id": tid,
                               "registered_steps": indexes, "steps": [], "required": applicable})
    return scopes


def registered_scopes(case, sources, delivery):
    try:
        from .engine import registered_native_scopes
    except ImportError:
        return _fallback_scopes(case, sources, delivery)
    return registered_native_scopes(case, delivery)


def _case_index(root):
    cases = _jsonl(root / "registered_data/cases.jsonl")
    controls = _jsonl(root / "registered_data/baseline.jsonl")
    if not controls:
        controls = _jsonl(root / "registered_data/baselines.jsonl")
    result = {c["case_id"]: c for c in cases + controls}
    if len(result) != len(cases) + len(controls):
        raise ValueError("Duplicate registered case or baseline identity")
    return result


def _load_records(root, jobs, aliases):
    records, ledger = {}, []
    for job in jobs:
        episode = read_json(root / "records" / (job["key"] + ".json"))
        if episode is not None:
            for field in ("case_id", "model", "experiment", "view", "repeat", "delivery"):
                if field in episode and episode[field] != job.get(field, "sequential" if field == "delivery" else None):
                    raise ValueError("Completed record identity mismatch: " + job["key"] + "/" + field)
            if episode.get("job_key", job["key"]) != job["key"]:
                raise ValueError("Completed record key mismatch: " + job["key"])
            # Requests can contain megabytes of observed page history per
            # episode. Reporting retains only measured facts, never every
            # prompt from every episode at once. Full traces remain in records.
            keep = ("schema_version", "case_id", "model", "experiment", "view", "repeat", "delivery", "status",
                    "job_key", "api_ok", "evaluation", "decision_stages", "native_scopes", "error_diagnostics",
                    "execution_scope", "method", "method_protocol", "method_summary")
            records[job["key"]] = {field: episode[field] for field in keep if field in episode}
            records[job["key"]]['efficiency'] = _efficiency(episode)
        ledger.append({**job, "status": episode.get("status") if episode else "registered_pending",
                       "canonical_job_key": job["key"], "shared_record": False, "execution_scope": SCOPE})
    for alias in aliases:
        target = alias["canonical_job_key"]
        key = alias.get("key", alias.get("alias_job_key"))
        if target in records:
            episode = copy.deepcopy(records[target])
            for field in ("case_id", "model", "experiment", "view", "repeat", "delivery"):
                if field in alias:
                    episode[field] = alias[field]
            episode["shared_from_main"] = True
            episode["job_key"] = key
            records[key] = episode
        ledger.append({**alias, "key": key, "status": records.get(key, {}).get("status", "registered_pending"),
                       "canonical_job_key": target, "shared_record": True, "execution_scope": SCOPE})
    return records, ledger


def _efficiency(episode):
    """Provider-reported tokens, never a tokenizer estimate or a fabricated zero.

    Prefix cache reuse is free here. Transport retries and superseded attempts
    are excluded; these columns are not a provider billing statement.
    """
    calls=episode.get('calls',[])
    usage=[u.get('usage') for u in episode.get('usage',[]) if isinstance(u,dict)]
    billed=sum(c.get('api_called') is True for c in calls)
    result={'model_requests':len(calls),'api_calls':billed,
            'cache_hits':sum(c.get('response_origin')=='identical_prompt_cached_model_prediction' for c in calls),
            'request_seconds':sum(max(0,c.get('finished_unix',c.get('started_unix',0))-c.get('started_unix',0)) for c in calls),
            'stages':dict(Counter(c.get('method_stage','direct') for c in calls))}
    result['stage_efficiency']=[]
    for stage in sorted(result['stages']):
        subset=[c for c in calls if c.get('method_stage','direct')==stage]
        paid=[c for c in subset if c.get('api_called') is True]
        row={'stage':stage,'model_requests':len(subset),'api_calls':len(paid),
             'cache_hits':sum(c.get('response_origin')=='identical_prompt_cached_model_prediction' for c in subset)}
        for target,fields in [('input_tokens',('prompt_tokens','input_tokens')),
                              ('output_tokens',('completion_tokens','output_tokens')),
                              ('total_tokens',('total_tokens',))]:
            values=[next((c['usage'][k] for k in fields if isinstance(c['usage'].get(k),(int,float))
                          and not isinstance(c['usage'][k],bool)),None)
                    if isinstance(c.get('usage'),dict) else None for c in paid]
            row[target]=sum(values) if all(v is not None for v in values) else None
        row['request_seconds']=sum(max(0,c.get('finished_unix',c.get('started_unix',0))-c.get('started_unix',0)) for c in subset)
        result['stage_efficiency'].append(row)
    for target,fields in [('input_tokens',('prompt_tokens','input_tokens')),
                          ('output_tokens',('completion_tokens','output_tokens')),
                          ('total_tokens',('total_tokens',))]:
        values=[next((u[k] for k in fields if isinstance(u.get(k),(int,float)) and not isinstance(u[k],bool)),None)
                if isinstance(u,dict) else None for u in usage]
        result[target]=sum(values) if len(values)==billed and all(v is not None for v in values) else None
    return result


def export(root):
    root = Path(root).expanduser().resolve()
    manifest = read_json(root / "run_manifest.json")
    jobs = read_json(root / "jobs.json", [])
    aliases = read_json(root / "aliases.json", [])
    if not isinstance(manifest, dict):
        raise ValueError("Run manifest is required for registered-denominator export")
    cases = _case_index(root)
    sources = read_json(root / "registered_sources.json", {})
    pair_registry = read_json(root / "pair_registry.json", [])
    records, ledger = _load_records(root, jobs, aliases)
    out = root / "tables"
    cohorts = defaultdict(list)
    for job in jobs + aliases:
        cohorts[(job["model"], job["experiment"], job["view"], job.get("delivery", "sequential"))].append(job)
    scores, comparisons, denominator_rows, ci_rows = [], [], [], []
    native_rows, native_lookup, decision_details = [], {}, []
    for identity, registered_jobs in sorted(cohorts.items()):
        model, exp, view, delivery = identity
        selected_ids = list(dict.fromkeys(j["case_id"] for j in registered_jobs))
        selected_cases = [cases[cid] for cid in selected_ids]
        selected_episodes = [records[j["key"]] for j in registered_jobs if j["key"] in records]
        repeats = manifest.get("repeats", 1)

        def add_score(subcases, episodes, label):
            score = score_group(subcases, episodes, repeats, delivery)
            score.update(model=model, experiment=label, view=view, delivery=delivery,
                         method=manifest.get("method", "direct"),
                         real_website_outcome_evaluated=False, human_reviewed=manifest.get("human_reviewed", False))
            scores.append(score)
            row = {"model": model, "method": manifest.get("method", "direct"), "experiment": label, "view": view, "delivery": delivery,
                   "N_cases": score["registered_cases"], "N_episodes": score["registered_episodes"],
                   "N_events": score["registered_events"], **{m: score.get(m) for m in COMMON + COUNTERFACTUAL + MULTI},
                   "execution_scope": SCOPE}
            comparisons.append(row)
            for metric, denominator in score["denominators"].items():
                denominator_rows.append({"model": model, "experiment": label, "view": view, "delivery": delivery,
                                         "metric": metric, "registered_value": score.get(metric), **denominator,
                                         "applicability": "no_registered_opportunities" if denominator["N"] == 0 else "registered_opportunities",
                                         "execution_scope": SCOPE})
            write_json(out / (label.replace("/", "_") + "_" + model + "_" + view + "_" + delivery + ".json"), score)
            return score

        add_score(selected_cases, selected_episodes, exp)
        if exp == "counterfactual":
            for kind in ("goal", "state", "semantic"):
                subset = [c for c in selected_cases if c.get("cf_type") == kind]
                if subset:
                    ids = {c["case_id"] for c in subset}
                    add_score(subset, [e for e in selected_episodes if e["case_id"] in ids], exp + "/" + kind)
        if exp in {"multi", "context_ablation_multi"}:
            # CAN/final-intent cases belong to Multi-event. They are retained
            # in its denominator and optional subtype diagnostics, never
            # registered or reported as a second independent experiment.
            for subtype in ("ORD", "COND", "RES", "CAN"):
                subset = [c for c in selected_cases if c.get("subtype", "").split("/")[0] == subtype]
                if subset:
                    ids = {c["case_id"] for c in subset}
                    add_score(subset, [e for e in selected_episodes if e["case_id"] in ids], exp + "/" + subtype.lower())

        episode_index = {(e["case_id"], e.get("repeat", 0)): e for e in selected_episodes}
        for repeat in range(repeats):
            for case in selected_cases:
                episode = episode_index.get((case["case_id"], repeat))
                status = (episode or {}).get("status", "registered_pending")
                for event in decision_records(case, episode, delivery):
                    decision_details.append({"model": model, "experiment": exp, "view": view, "delivery": delivery,
                                             "repeat": repeat, "case_id": case["case_id"], "event_id": event["event_id"],
                                             "classification_scope": event["classification_scope"], "gold": event["gold"],
                                             "gold_branch": event["gold_branch"], "predicted": event["predicted"],
                                             "predicted_branch": event["predicted_branch"], "known": event["known"],
                                             "valid": event["valid"], "correct": event["coarse_correct"],
                                             "branch_correct": event["branch_correct"], "verification_passed": event["verification_passed"],
                                             "verification_correct": event["verification_correct"]})
                scopes = (episode or {}).get("native_scopes")
                if scopes is None:
                    scopes = registered_scopes(case, sources, delivery)
                for scope in scopes:
                    if not scope.get("registered_steps"):
                        continue
                    values = native_result(scope, status)
                    row = {"model": model, "experiment": exp, "view": view, "delivery": delivery, "repeat": repeat,
                           "case_id": case["case_id"], "website": case.get("website", "unknown"),
                           "cf_type": case.get("cf_type"), "subtype": case.get("subtype"), "group_id": case.get("group_id"),
                           "scope": scope["scope"], "workspace": scope["workspace"], "source_task_id": scope["source_task_id"],
                           "step_indices": scope["registered_steps"], "required": scope.get("required", False), "status": status,
                           **values}
                    native_rows.append(row)
                    native_lookup[(model, exp, view, delivery, repeat, case["case_id"], scope["workspace"], scope["scope"])] = (row, scope)
        for metric, value_type in (("Acc", "decision"), ("MicroESR", "execution")):
            values = []
            for repeat in range(repeats):
                for case in selected_cases:
                    episode = episode_index.get((case["case_id"], repeat))
                    if value_type == "decision":
                        values.extend({"website": case.get("website", "unknown"), "value": e["coarse_correct"]}
                                      for e in decision_records(case, episode, delivery) if e["classification_scope"] == "unique")
                    else:
                        value = (episode or {}).get("evaluation", {}).get("episode_success") if episode is not None and episode.get("status") not in INFRA else None
                        values.append({"website": case.get("website", "unknown"), "value": value})
            ci_rows.append({"model": model, "experiment": exp, "view": view, "delivery": delivery,
                            "metric": metric, **cluster_bootstrap(values)})

    grouped_native = defaultdict(list)
    for row in native_rows:
        if not row["required"]:
            continue
        grouped_native[(row["model"], row["experiment"], row["view"], row["delivery"], row["scope"])].append(row)
        if row["experiment"] == "counterfactual" and row["cf_type"]:
            grouped_native[(row["model"], "counterfactual/" + row["cf_type"], row["view"], row["delivery"], row["scope"])].append(row)
        if row["workspace"] != "A":
            grouped_native[(row["model"], row["experiment"], row["view"], row["delivery"], "B_all_new_tasks")].append(row)
    native_summary, native_ci = [], []
    for identity, rows in sorted(grouped_native.items()):
        model, exp, view, delivery, phase = identity
        result = {"model": model, "experiment": exp, "view": view, "delivery": delivery, "scope": phase,
                  "registered_tasks": len(rows), "registered_steps": sum(r["registered_steps"] for r in rows),
                  "known_steps": sum(r["known_steps"] for r in rows), "attempted_steps": sum(r["attempted_steps"] for r in rows),
                  "averaging": "macro over registered task-workspaces; omitted model predictions are wrong; infrastructure is unknown"}
        for metric in NATIVE:
            denominator = numeric_summary([r[metric] for r in rows])
            result.update({metric: denominator["rate"], "Covered" + metric: denominator["covered_rate"], metric + "_known_tasks": denominator["known"]})
        result["metric_label"] = "ContinuationSR" if phase == "A_post" else "ReplayTaskSR"
        native_summary.append(result)
        for metric in ("StepSR", "TaskSR"):
            native_ci.append({"model": model, "experiment": exp, "view": view, "delivery": delivery, "scope": phase,
                              "metric": metric, **cluster_bootstrap([{"website": r["website"], "value": r[metric]} for r in rows])})

    paired, paired_summary = _paired_impacts(native_lookup, pair_registry, manifest, cases)
    fields = ["model", "method", "experiment", "view", "delivery", "N_cases", "N_episodes", "N_events", *COMMON, *COUNTERFACTUAL, *MULTI, "execution_scope"]
    atomic_csv(out / "all_experiments.csv", comparisons, fields)
    for exp in ("main", "ablation", "counterfactual", "multi", "baseline",
                "context_ablation_goal", "context_ablation_state", "context_ablation_multi"):
        rows = [r for r in comparisons if r["experiment"] == exp]
        if not exp.startswith("context_ablation_") or rows:
            atomic_csv(out / (exp + "_comparison.csv"), rows, fields)
    for kind in ("goal", "state", "semantic"):
        atomic_csv(out / ("counterfactual_" + kind + "_comparison.csv"), [r for r in comparisons if r["experiment"] == "counterfactual/" + kind], fields)
    for delivery in ("sequential", "batch", "sequential_no_action"):
        atomic_csv(out / ("multi_" + delivery + "_comparison.csv"), [r for r in comparisons if r["experiment"] == "multi" and r["delivery"] == delivery], fields)
    method_diagnostics=[]
    for job in jobs:  # Canonical only; aliases must not double-count failures.
        episode=records.get(job['key'],{})
        if episode.get('method_summary'):
            method_diagnostics.append({**{k:job.get(k) for k in ('key','case_id','model','experiment','view','repeat','delivery')},
                                       'status':episode.get('status'),'method_protocol':episode.get('method_protocol'),
                                       **episode['method_summary']})
    efficiency_rows = [{**{k:j[k] for k in ('key','model','experiment','view','delivery','repeat','case_id')},
                        'status':records.get(j['key'],{}).get('status','registered_pending'),
                        'shared_record':False,**records.get(j['key'],{}).get('efficiency',{})} for j in jobs]
    efficiency_summary=[]
    for identity in sorted({(j['model'],j['experiment'],j['view'],j['delivery']) for j in jobs}):
        selected=[r for r in efficiency_rows if tuple(r[k] for k in ('model','experiment','view','delivery'))==identity]
        known=[r for r in selected if 'model_requests' in r]
        row=dict(zip(('model','experiment','view','delivery'),identity),registered_episodes=len(selected),recorded_episodes=len(known))
        for k in ('model_requests','api_calls','cache_hits','input_tokens','output_tokens','total_tokens','request_seconds'):
            measured=[r[k] for r in known if r.get(k) is not None]
            row[k]=sum(measured) if measured else None
            row[k+'_covered_episodes']=len(measured)
        row['scope']='retained canonical attempts only; duration includes waiting; failed transport and superseded attempts can incur unreported provider cost'
        efficiency_summary.append(row)
    stage_efficiency=[{**{k:r[k] for k in ('key','model','experiment','view','delivery','repeat','case_id')},**s}
                      for r in efficiency_rows for s in r.get('stage_efficiency',[])]
    tables = {"scores": scores, "native_metrics": native_summary, "native_by_case": native_rows,
              'efficiency_by_case':efficiency_rows,'efficiency_summary':efficiency_summary,
              'efficiency_by_stage':stage_efficiency,
              "method_diagnostics": method_diagnostics,
              "paired_impacts": paired, "paired_impact_summary": paired_summary,
              "metrics_with_denominators": denominator_rows, "confidence_intervals": ci_rows,
              "native_confidence_intervals": native_ci, "job_ledger": ledger, "event_predictions": decision_details,
              "control_metrics": [{k: row[k] for k in ("model", "experiment", "view", "delivery", "N_cases", "N_episodes", "ControlESR", "PriorityAcc", "DeclaredScheduleExactMatch", "ActualScheduleExactMatch", "PTAR", "CancelCompliance", "RCR", "RAR", "UGS")} for row in comparisons]}
    for name, rows in tables.items():
        for row in rows:
            row.setdefault('method',manifest.get('method','direct'))
        write_json(out / (name + ".json"), rows)
        if name != "scores":
            atomic_csv(out / (name + ".csv"), rows)
    for kind in ("goal", "state", "multi"):
        label = "context_ablation_" + kind
        if any(row["experiment"] == label for row in comparisons):
            atomic_csv(out / (label + "_native.csv"),
                       [row for row in native_summary if row["experiment"] == label])
    failures = [{"case_id": e["case_id"], "job_key": e.get("job_key"), "model": e["model"],
                 "delivery": e.get("delivery", "sequential"), "repeat": e.get("repeat", 0),
                 "status": e["status"], "evaluation": e.get("evaluation"), "decision_stages": e.get("decision_stages"),
                 "record_path": str(root / "records" / (key + ".json"))}
                for key, e in records.items() if e.get("experiment") in {"multi", "context_ablation_multi"}
                and e.get("evaluation", {}).get("episode_success") is False]
    write_json(out / "case_D_multi_event_failures.json", failures)
    write_json(out / "pair_registry.json", pair_registry)
    canonical_statuses = Counter(records[j["key"]]["status"] for j in jobs if j["key"] in records)
    completed = sum(canonical_statuses.values())
    infra = sum(count for status, count in canonical_statuses.items() if status in INFRA)
    result = {"registered_canonical": len(jobs), "recorded": completed,
              "method": manifest.get("method", "direct"), "method_options": manifest.get("method_options", {}),
              "pending": len(jobs) - completed, "unknown_execution": infra,
              "statuses": dict(canonical_statuses), "logical_jobs": len(jobs) + len(aliases),
              "main_ablation_shared": bool(aliases), "main_view":manifest.get('information_protocol',{}).get('main','trajectory'),
              "human_reviewed": manifest.get("human_reviewed", False),
              "review_method": "single_user_workbook_approval", "annotator_agreement_measured": False,
              "complete_run": completed == len(jobs) and infra == 0,
              "execution_scope": SCOPE, "real_website_outcome_evaluated": False,
              "native_tables_exported": True,
              "note": "Rates are fractions. Unknown and inapplicable values are null, not zero. Review approval does not establish inter-annotator agreement."}
    write_json(out / "summary.json", result)
    # Remove obsolete generated subset tables on re-export as well. Raw
    # episodes and their CAN annotations remain untouched.
    for path in out.glob("final_intent_*"):
        if path.is_file() and path.suffix in {".json", ".csv"}:
            path.unlink()
    obsolete_context = out / "context_ablation_comparison.csv"
    if obsolete_context.exists():
        obsolete_context.unlink()
    return result


def _paired_impacts(lookup, registry, manifest, cases):
    by_case = {r["case_id"]: r for r in registry}
    baseline_lookup = {}
    for key, value in lookup.items():
        model, exp, view, delivery, repeat, cid, workspace, phase = key
        if exp == "baseline" and phase == "A_full":
            baseline_lookup[(model, repeat, cid)] = value
    paired = []
    for key, (event_row, event_scope) in lookup.items():
        model, exp, view, delivery, repeat, cid, workspace, phase = key
        if exp == "baseline" or cid not in by_case or not event_row["required"] or phase == "A_pre":
            continue
        reg = by_case[cid]
        baseline_id = reg.get("A_baseline_id") if workspace == "A" else reg.get("B_baseline_ids", {}).get(workspace)
        if baseline_id is None:
            continue
        control = baseline_lookup.get((model, repeat, baseline_id))
        selected = set(event_scope["registered_steps"])
        baseline_values = None
        if control:
            baseline_row, baseline_scope = control
            if baseline_scope["source_task_id"] != event_scope["source_task_id"] or not selected <= set(baseline_scope["registered_steps"]):
                raise ValueError("Native event/baseline source or step identity mismatch")
            restricted = {**baseline_scope, "registered_steps": sorted(selected),
                          "steps": [r for r in baseline_scope.get("steps", []) if r["source_step"] in selected]}
            baseline_values = native_result(restricted, baseline_row["status"])
        row = {"model": model, "experiment": exp, "view": view, "delivery": delivery, "repeat": repeat,
               "case_id": cid, "website": event_row["website"], "scope": phase, "workspace": workspace,
               "source_task_id": event_row["source_task_id"], "baseline_id": baseline_id,
               "step_indices": sorted(selected), "registered_steps": len(selected),
               "group_id": reg.get("group_id"), "cf_type": reg.get("cf_type"),
               "delta_direction": "event minus no-event; negative indicates degradation"}
        for metric in ("ElementAcc", "OperationF1", "StepSR", "TaskSR"):
            x, y = event_row[metric], baseline_values[metric] if baseline_values else None
            row[metric + "_event"] = x
            row[metric + "_baseline"] = y
            row[metric + "_delta"] = x - y if x is not None and y is not None else None
        paired.append(row)
    groups = defaultdict(list)
    for row in paired:
        groups[(row["model"], row["experiment"], row["view"], row["delivery"], row["scope"])].append(row)
    summary = []
    for identity, rows in sorted(groups.items()):
        model, exp, view, delivery, phase = identity
        result = {"model": model, "experiment": exp, "view": view, "delivery": delivery,
                  "scope": phase, "registered_pairs": len(rows),
                  "delta_direction": "event minus no-event; negative indicates degradation"}
        for metric in ("ElementAcc", "OperationF1", "StepSR", "TaskSR"):
            values = [r[metric + "_delta"] for r in rows]
            known = [x for x in values if x is not None]
            result[metric + "_delta"] = sum(known) / len(values) if values and len(known) == len(values) else None
            result["Covered" + metric + "_delta"] = sum(known) / len(known) if known else None
            result[metric + "_known_pairs"] = len(known)
            ci = cluster_bootstrap([{"website": r["website"], "value": r[metric + "_delta"]} for r in rows])
            result[metric + "_ci_lower"] = ci["lower"]
            result[metric + "_ci_upper"] = ci["upper"]
        summary.append(result)
    return paired, summary
