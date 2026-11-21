"""Registered-denominator metrics for reviewed offline event evaluation.

These functions never infer a website outcome from a model's completion claim.
Only independent engine evaluation fields and scored native predictions are used.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
import math
import random

DECISIONS = ("IGNORE", "DEFER", "INTERRUPT")
FOLLOW_UPS = ("HANDLE", "REPLAN", "TERMINATE")
BRANCHES = ("IGNORE", "DEFER", *FOLLOW_UPS)
INFRA = {"provider_error", "environment_error", "source_unavailable", "framework_error",
         "registered_pending", "interrupted", "input_budget_exceeded"}
VIEWS = ("event-only", "goal", "event-state", "state", "trajectory")


def coarse(branch):
    return "INTERRUPT" if branch in FOLLOW_UPS else branch if branch in DECISIONS else None


def tri_and(values):
    """A known contradiction establishes failure; otherwise retain uncertainty."""
    values = list(values)
    if any(x is False for x in values):
        return False
    return None if any(x is None for x in values) else True


def summarize(values):
    values = [x if isinstance(x, bool) else None for x in values]
    n = sum(x is True for x in values)
    known = sum(x is not None for x in values)
    N = len(values)
    return {"rate": n / N if N and known == N else None, "n": n, "N": N,
            "known": known, "unknown": N - known,
            "coverage": known / N if N else None,
            "covered_rate": n / known if known else None,
            "lower_bound": n / N if N else None,
            "upper_bound": (n + N - known) / N if N else None}


def numeric_summary(values):
    values = [float(x) if isinstance(x, (int, float)) and not isinstance(x, bool)
              and math.isfinite(x) else float(x) if isinstance(x, bool) else None for x in values]
    known = [x for x in values if x is not None]
    N, K = len(values), len(known)
    total = sum(known)
    return {"rate": total / N if N and K == N else None, "n": total, "N": N,
            "known": K, "unknown": N - K, "coverage": K / N if N else None,
            "covered_rate": total / K if K else None,
            "lower_bound": total / N if N else None,
            "upper_bound": (total + N - K) / N if N else None}


def fixed_macro_f1(gold, predicted, classes=DECISIONS):
    if len(gold) != len(predicted):
        raise ValueError("Classification lengths differ")
    if any(x not in classes for x in gold):
        raise ValueError("Unregistered classification target")
    by_class = {}
    for label in classes:
        tp = sum(g == label and p == label for g, p in zip(gold, predicted))
        fp = sum(g != label and p == label for g, p in zip(gold, predicted))
        fn = sum(g == label and p != label for g, p in zip(gold, predicted))
        denominator = 2 * tp + fp + fn
        by_class[label] = {"tp": tp, "fp": fp, "fn": fn,
                           "support": sum(g == label for g in gold),
                           "f1": 2 * tp / denominator if denominator else 0.0}
    score = sum(x["f1"] for x in by_class.values()) / len(classes) if gold else None
    supported = [x["f1"] for x in by_class.values() if x["support"]]
    return {"macro_f1": score, "supported_macro_f1": sum(supported) / len(supported) if supported else None,
            "classes": by_class, "N": len(gold), "policy": "fixed registered classes; invalid output is a false negative"}


def schedule_valid(schedule, required, edges, completed=()):
    """Accept every topological ordering, rather than one arbitrary oracle order."""
    if not isinstance(schedule, list) or any(not isinstance(x, str) for x in schedule):
        return False
    pending = set(required) - set(completed)
    if len(schedule) != len(set(schedule)) or set(schedule) != pending:
        return False
    positions = {task: i for i, task in enumerate(schedule)}
    return all(x not in pending or y not in pending or positions[x] < positions[y] for x, y in edges)


def stages_for(case, delivery="sequential"):
    private = case.get("private", {})
    if delivery in {"batch", "sequential_no_action", "sequential-no-action"}:
        batch = private.get("batch_control", {})
        if batch.get("stages") is not None:
            return batch["stages"]
    return private.get("stages", [])


def _branches(stage):
    allowed = stage.get("allowed_branches", [])
    if isinstance(allowed, str):
        allowed = [allowed]
    return [x for x in allowed if x in BRANCHES]


def _scope(stage):
    explicit = stage.get("classification_scope", stage.get("coarse_score_scope", ""))
    if "verification" in explicit or (stage.get("condition") or {}).get("resolved") == "Unknown":
        return "verification_required"
    return "unique" if len(_branches(stage)) == 1 else "set_valued"


def _predicted(stage):
    branch = stage.get("predicted_branch")
    if branch in BRANCHES:
        return branch
    report = stage.get("reported", {})
    if not isinstance(report, Mapping):
        return None
    branch = report.get("branch", report.get("decision"))
    if branch == "INTERRUPT":
        return report.get("follow_up", report.get("followup"))
    return branch if branch in BRANCHES else None


def _predicted_coarse(stage):
    value = coarse(_predicted(stage))
    if value is not None:
        return value
    report = stage.get("reported", {})
    if isinstance(report, Mapping):
        decision = report.get("decision")
        if decision is None and isinstance(report.get("decisions"), Mapping):
            decision = report["decisions"].get(stage.get("event_id"))
        if isinstance(decision, Mapping):
            decision = decision.get("decision")
        return decision if decision in DECISIONS else None
    return None


def _terminal(episode):
    return episode is not None and episode.get("status") not in INFRA


def _stage_outcome(record, key):
    """A closed model/method failure is false; unfinished infrastructure is unknown."""
    value = (record.get("observed") or {}).get(key)
    if isinstance(value, bool):
        return value
    return False if record["terminal_episode"] else None


def _verification_correct(expected, observed, known):
    if not known:
        return None
    if observed is None or observed.get("verification_passed") is False:
        return False
    if observed.get("verification_passed") is not True:
        return None
    gold = expected.get("post_verification_gold")
    if isinstance(gold, Mapping):
        branch = gold.get("follow_up") if gold.get("decision") == "INTERRUPT" else gold.get("decision")
        return _predicted(observed) == branch
    if isinstance(observed.get("decision_correct"), bool):
        return observed["decision_correct"]
    allowed = _branches(observed)
    return _predicted(observed) in allowed if allowed else False


def decision_records(case, episode, delivery="sequential"):
    """One row for every registered event, including unseen and invalid events."""
    registered = stages_for(case, delivery)
    if not registered:
        labels = case.get("private", {}).get("gold", {}).get("decisions", {})
        registered = [{"event_id": e["id"], "allowed_branches": [
            labels.get(e["id"], {}).get("follow_up") or labels.get(e["id"], {}).get("decision")]} for e in case.get("events", [])]
    actual = {x.get("event_id"): x for x in (episode or {}).get("decision_stages", [])}
    result = []
    event_meta = {e["id"]: e for e in case.get("events", [])}
    for expected in registered:
        eid = expected["event_id"]
        observed = actual.get(eid)
        allowed = _branches(expected)
        scope = _scope(expected)
        prediction = _predicted(observed or {})
        known = observed is not None or _terminal(episode)
        gold = coarse(allowed[0]) if scope == "unique" and allowed else None
        value = prediction in allowed if known else None
        result.append({"case_id": case["case_id"], "event_id": eid, "website": case.get("website", "unknown"),
                       "allowed_branches": allowed, "classification_scope": scope,
                       "gold": gold, "gold_branch": allowed[0] if scope == "unique" and allowed else None,
                       "predicted": _predicted_coarse(observed or {}), "predicted_branch": prediction,
                       "known": known, "valid": prediction in BRANCHES if known else None,
                       "coarse_correct": _predicted_coarse(observed or {}) == gold if known and gold else None,
                       "branch_correct": value,
                       "verification_passed": (observed or {}).get("verification_passed") if observed else False if _terminal(episode) else None,
                       "verification_correct": _verification_correct(expected, observed, known) if scope == "verification_required" else None,
                       "critical": expected.get("critical") is True or event_meta.get(eid, {}).get("critical") is True,
                       "observed": observed or {}, "expected": expected, "terminal_episode": _terminal(episode)})
    return result


def _evaluation(episode, name):
    if not _terminal(episode):
        return None
    value = episode.get("evaluation", {}).get(name)
    return value if isinstance(value, (bool, int, float)) else None


def _registered_roles(case):
    private = case.get("private", {})
    gold = private.get("gold", {})
    final = gold.get("final_state", {})
    return gold.get("required_roles", final.get("required_tasks", []))


def _branch_applicable(case, branch, delivery="sequential"):
    for stage in stages_for(case, delivery):
        post = stage.get("post_verification_gold")
        if isinstance(post, Mapping):
            value = post.get("follow_up") if post.get("decision") == "INTERRUPT" else post.get("decision")
        else:
            values = _branches(stage)
            value = values[0] if len(values) == 1 else None
        if value == branch:
            return True
    return False


def _post(stage):
    return stage.get("post_verification_state") or stage.get("post_state", {})


def _pending_edges(stage):
    state = _post(stage)
    pending = set(state.get("required_tasks", [])) - set(state.get("completed_tasks", [])) - set(state.get("cancelled_tasks", []))
    return [(x, y) for x, y in state.get("precedence_constraints", []) if x in pending and y in pending]


def _stage_action_opportunity(case, delivery, index):
    stages = stages_for(case, delivery)
    if not stages:
        return False
    if delivery == "batch" and index != len(stages) - 1:
        return False
    if delivery in {"sequential_no_action", "sequential-no-action"} and index != len(stages) - 1:
        return False
    if index + 1 < len(stages):
        controls = case.get("events", [])[index + 1].get("arrival", {}).get("after_reference_controls", [])
        return any(c.get("cmd") == "reference_step" for c in controls)
    state = _post(stages[index])
    lengths = case.get("private", {}).get("source_lengths", {})
    pending = set(state.get("required_tasks", [])) - set(state.get("completed_tasks", [])) - set(state.get("cancelled_tasks", []))
    pointers = state.get("recorded_pointers", {})
    return any(pointers.get(role, 0) < lengths.get(role, 0) for role in pending)


def _priority_applicable(case, delivery, actual=False):
    stages = stages_for(case, delivery)
    return any(_pending_edges(stage) and (delivery != "batch" or index == len(stages) - 1)
               and (not actual or _stage_action_opportunity(case, delivery, index)) for index, stage in enumerate(stages))


def _cancel_applicable(case, delivery):
    return any(_post(stage).get("cancelled_tasks") for stage in stages_for(case, delivery))


def _has_new_native_opportunity(case, delivery):
    stages = stages_for(case, delivery)
    if not stages:
        return False
    final = _post(stages[-1])
    if any(role != "A" for role in final.get("required_tasks", [])):
        return True
    if delivery != "sequential":
        return False
    return any(command.get("cmd") == "reference_step" and command.get("task") != "A"
               for event in case.get("events", []) for command in event.get("arrival", {}).get("after_reference_controls", []))


def _known_evaluation(episode, key, omitted=False):
    value = _evaluation(episode, key)
    # For an independently registered behavior opportunity, terminating with
    # no measured behavior is an omission. Pending/infra episodes stay unknown.
    return omitted if value is None and _terminal(episode) else value


def score_group(cases, episodes, repeats=1, delivery="sequential"):
    """Score one model/experiment/view/delivery and retain all denominators."""
    episodes = list(episodes)
    index = {}
    for episode in episodes:
        key = (episode.get("case_id"), episode.get("repeat", 0))
        if key in index:
            raise ValueError("Duplicate completed record for one registered case/repeat")
        index[key] = episode
    rows = [(case, index.get((case["case_id"], repeat)), repeat)
            for repeat in range(repeats) for case in cases]
    events = [r for case, episode, _ in rows for r in decision_records(case, episode, delivery)]
    unique = [r for r in events if r["classification_scope"] == "unique"]
    denominators = {}
    result = {"registered_cases": len(cases), "registered_episodes": len(rows),
              "registered_events": len(events), "unique_classification_events": len(unique),
              "set_valued_events": sum(r["classification_scope"] == "set_valued" for r in events),
              "verification_required_events": sum(r["classification_scope"] == "verification_required" for r in events)}

    def binary(name, values):
        denominators[name] = summarize(values)
        result[name] = denominators[name]["rate"]

    def numeric(name, values):
        denominators[name] = numeric_summary(values)
        result[name] = denominators[name]["rate"]

    binary("Acc", [r["coarse_correct"] for r in unique])
    result["EventAcc"] = result["Acc"]
    denominators["EventAcc"] = denominators["Acc"]
    binary("BranchAcc", [r["branch_correct"] for r in events if r["classification_scope"] != "verification_required"])
    binary("SetAcceptance", [r["branch_correct"] for r in events if r["classification_scope"] == "set_valued"])
    binary("VerificationAcc", [r["verification_correct"] for r in events if r["classification_scope"] == "verification_required"])
    binary("InspectionCoverage", [r["verification_passed"] for r in events if r["classification_scope"] == "verification_required"])
    binary("FIR", [r["predicted"] == "INTERRUPT" if r["known"] else None for r in unique if r["gold"] != "INTERRUPT"])
    binary("MIR", [r["predicted"] != "INTERRUPT" if r["known"] else None for r in unique if r["gold"] == "INTERRUPT"])
    binary("decision_valid_rate", [r["valid"] for r in events])
    binary("Exposure", [bool(episode and any(s.get("event_id") == r["event_id"] for s in episode.get("decision_stages", [])))
                        for case, episode, _ in rows for r in decision_records(case, episode, delivery)])
    classification = fixed_macro_f1([r["gold"] for r in unique if r["known"]],
                                    [r["predicted"] for r in unique if r["known"]])
    all_known = all(r["known"] for r in unique)
    result["MacroF1"] = classification["macro_f1"] if all_known else None
    result["MacroF1Supported"] = classification["supported_macro_f1"] if all_known else None
    result["classification"] = {**classification, "registered_N": len(unique), "known": sum(r["known"] for r in unique),
                                "unknown": sum(not r["known"] for r in unique)}
    for name in ("MacroF1", "MacroF1Supported", "EventF1"):
        denominators[name] = {"rate": result.get(name, result["MacroF1"]), "n": None,
                              "N": len(unique), "known": sum(r["known"] for r in unique),
                              "unknown": sum(not r["known"] for r in unique),
                              "coverage": sum(r["known"] for r in unique) / len(unique) if unique else None,
                              "covered_rate": classification["supported_macro_f1"] if name.endswith("Supported") else classification["macro_f1"],
                              "lower_bound": None, "upper_bound": None}
    result["EventF1"] = result["MacroF1"]
    interrupt = [r for r in unique if r["gold"] == "INTERRUPT"]
    conditional = [r for r in interrupt if r["known"] and r["predicted"] == "INTERRUPT"]
    binary("FollowupAcc", [r["branch_correct"] for r in conditional])
    followup = fixed_macro_f1([r["gold_branch"] for r in conditional], [r["predicted_branch"] for r in conditional], FOLLOW_UPS)
    result["FollowupF1"] = followup["macro_f1"]
    result["FollowupF1Supported"] = followup["supported_macro_f1"]
    result["follow_up_classification"] = followup
    for name in ("FollowupF1", "FollowupF1Supported"):
        denominators[name] = {"rate": result[name], "n": None, "N": len(conditional), "known": len(conditional), "unknown": 0,
                              "coverage": 1.0 if conditional else None, "covered_rate": result[name], "lower_bound": None, "upper_bound": None}
    binary("JointFUAcc", [r["branch_correct"] for r in interrupt])
    case_correct = {}
    branch_case_correct = {}
    for case, episode, repeat in rows:
        erows = decision_records(case, episode, delivery)
        # A verification-required event is assessed by the required inspection,
        # not relabelled as a missing unique three-way classification.
        vals = [r["verification_correct"] if r["classification_scope"] == "verification_required"
                else r["coarse_correct"] if r["classification_scope"] == "unique"
                else coarse(r["predicted_branch"]) in {coarse(x) for x in r["allowed_branches"]} if r["known"] else None for r in erows]
        case_correct[(case["case_id"], repeat)] = tri_and(vals) if vals else None
        branch_case_correct[(case["case_id"], repeat)] = tri_and([r["verification_correct"] if r["classification_scope"] == "verification_required" else r["branch_correct"] for r in erows]) if erows else None
    binary("CompleteCaseAcc", list(case_correct.values()) if events else [])
    binary("CompleteBranchCaseAcc", list(branch_case_correct.values()) if events else [])
    binary("OverallAcc", list(case_correct.values()) if events else [])

    evaluation_metrics = {
        "MicroESR": "episode_success", "JointReplayESR": "episode_success",
        "ControlESR": "control_success", "TaskCompletion": "task_completion",
        "EHS": "event_handling_success", "PTAR": "ptar", "RR": "resumed",
        "RCR": "resume_success", "RAR": "resume_correct_position", "UGS": "updated_goal_success",
        "ECS": "effective_constraints", "TermCR": "termination_compliant",
        "ScheduleExactMatch": "declared_schedule_correct", "DeclaredScheduleExactMatch": "declared_schedule_correct",
        "ActualScheduleExactMatch": "actual_schedule_correct", "PriorityCaseAcc": "priority_correct",
        "ActualPriorityCaseAcc": "actual_priority_correct", "ICR": "ignore_compliant", "EDR": "event_deferred_correctly",
        "OTS": "original_before_deferred", "DER": "deferred_event_resumed", "DES": "deferred_event_success",
        "ETS": "event_timely", "CancelCompliance": "cancel_compliant"}
    optional = {"PTAR": "TERMINATE", "RR": "HANDLE", "RCR": "HANDLE", "RAR": "HANDLE", "UGS": "REPLAN",
                "TermCR": "TERMINATE", "ICR": "IGNORE", "EDR": "DEFER", "OTS": "DEFER", "DER": "DEFER", "DES": "DEFER"}
    for name, key in evaluation_metrics.items():
        candidates = [(case, episode) for case, episode, _ in rows
                      if name not in optional or _branch_applicable(case, optional[name], delivery)]
        if name == "EHS":
            candidates = [(case, episode) for case, episode in candidates if case.get("events")]
        if name in {"ScheduleExactMatch", "DeclaredScheduleExactMatch", "ActualScheduleExactMatch"}:
            candidates = [(case, episode) for case, episode in candidates if case.get("events")]
        elif name == "PriorityCaseAcc":
            candidates = [(case, episode) for case, episode in candidates if _priority_applicable(case, delivery)]
        elif name == "ActualPriorityCaseAcc":
            candidates = [(case, episode) for case, episode in candidates if _priority_applicable(case, delivery, actual=True)]
        elif name == "CancelCompliance":
            candidates = [(case, episode) for case, episode in candidates if _cancel_applicable(case, delivery)]
        elif name in {"ETS", "OTS"}:
            candidates = [(case, episode) for case, episode in candidates if _priority_applicable(case, delivery, actual=True)]
        elif name in {"DER", "DES"}:
            candidates = [(case, episode) for case, episode in candidates if _has_new_native_opportunity(case, delivery)]
        # Remaining optional diagnostics require explicit engine registration.
        elif name not in {"MicroESR", "JointReplayESR", "ControlESR", "TaskCompletion", "EHS"} and name not in optional:
            candidates = [(case, episode) for case, episode in candidates
                          if key in (episode or {}).get("evaluation", {}) or case.get("experiment") == "multi" and name in {"ScheduleExactMatch", "DeclaredScheduleExactMatch", "ActualScheduleExactMatch", "PriorityCaseAcc", "ActualPriorityCaseAcc", "CancelCompliance"}]
        binary(name, [_known_evaluation(episode, key) for _, episode in candidates])
    numeric("PriorityAcc", [_known_evaluation(episode, "priority_accuracy", 0.0) for case, episode, _ in rows if _priority_applicable(case, delivery)])
    numeric("ActualPriorityAcc", [_known_evaluation(episode, "actual_priority_accuracy", 0.0) for case, episode, _ in rows if _priority_applicable(case, delivery, actual=True)])
    stage_successes = [(r["gold_branch"], _stage_outcome(r, "execution_success")) for r in events]
    branch_rates = []
    for branch, name in zip(BRANCHES, ("I_SR", "D_SR", "H_SR", "R_SR", "T_SR")):
        binary(name, [value for gold, value in stage_successes if gold == branch])
        if denominators[name]["N"]:
            branch_rates.append(result[name])
    numeric("MacroESR", branch_rates)
    stage_priority = []
    for case, episode, _ in rows:
        for i, event in enumerate(decision_records(case, episode, delivery)):
            if not _pending_edges(event["expected"]) or not _stage_action_opportunity(case, delivery, i):
                continue
            value = event["observed"].get("priority_correct")
            stage_priority.append(False if value is None and _terminal(episode) else value)
    numeric("PriorityStageAcc", stage_priority)
    critical = [r for r in events if r["critical"]]
    binary("CriticalEventRecall", [r["predicted"] == "INTERRUPT" if r["known"] else None for r in critical])
    binary("ActualCriticalEventRecall", [_stage_outcome(r, "execution_success") for r in critical])
    binary("Coverage", [_terminal(episode) for _, episode, _ in rows])
    api_values = []
    for _, episode, _ in rows:
        if episode is None:
            api_values.append(None)
        elif episode.get("status") == "provider_error":
            api_values.append(False)
        else:
            api_values.append(episode.get("api_ok", True))
    binary("api_ok_rate", api_values)
    binary("FalseInterruptionRate", [r["predicted"] == "INTERRUPT" if r["known"] else None for r in unique if r["gold"] != "INTERRUPT"])
    # Group rates are per registered pair/triple; overall is per case.
    groups = defaultdict(list)
    for case in cases:
        if case.get("cf_type") and case.get("group_id"):
            groups[(case["cf_type"], case["group_id"])].append(case["case_id"])
    for kind, metric, size in (("goal", "CFA", 2), ("state", "SFA", 2), ("semantic", "SemanticRobustAccuracy", 3)):
        values = []
        for (cf_type, group_id), members in groups.items():
            if cf_type != kind:
                continue
            if len(members) != size or len(set(members)) != size:
                raise ValueError("Incomplete or duplicate registered counterfactual group: " + group_id)
            for repeat in range(repeats):
                values.append(tri_and([case_correct[(cid, repeat)] for cid in members]))
        binary(metric, values)
    result["denominators"] = denominators
    result["execution_scope"] = "offline event-conditioned teacher-forced predictions; no backend outcomes"
    return result


def native_result(scope, episode_status="registered_pending"):
    registered = list(scope.get("registered_steps", []))
    if len(registered) != len(set(registered)):
        raise ValueError("Duplicate registered native step")
    by_step = {r["source_step"]: r for r in scope.get("steps", [])}
    values = []
    for i in registered:
        row = by_step.get(i)
        if row is None:
            unknown = episode_status in INFRA
            row = {"source_step": i, "attempted": False, "element_correct": None if unknown else False,
                   "operation_f1": None if unknown else 0.0, "step_correct": None if unknown else False,
                   "candidate_recalled": None, "status": "unobserved_infrastructure" if unknown else "not_attempted_model_stop"}
        values.append(row)
    attempted = [r for r in values if r.get("attempted") is True]
    def avg(field, rs=values):
        return numeric_summary([r.get(field) for r in rs])["rate"]
    result = {"ElementAcc": avg("element_correct"), "OperationF1": avg("operation_f1"), "StepSR": avg("step_correct"),
            "TaskSR": tri_and([r.get("step_correct") for r in values]) if values else None,
            "registered_steps": len(values), "known_steps": sum(r.get("step_correct") is not None for r in values),
            "attempted_steps": len(attempted), "AttemptCoverage": len(attempted) / len(values) if values else None,
            "Coverage": sum(r.get("step_correct") is not None for r in values) / len(values) if values else None,
            "AttemptedElementAcc": avg("element_correct", attempted), "AttemptedOperationF1": avg("operation_f1", attempted),
            "AttemptedStepSR": avg("step_correct", attempted), "CandidateRecall": avg("candidate_recalled", attempted)}
    if episode_status in INFRA:
        # Partial action diagnostics remain inspectable, but interrupted task
        # outcomes are not promoted into completed experiment measurements.
        for key in ("ElementAcc", "OperationF1", "StepSR", "TaskSR"):
            result[key] = None
    return result


def cluster_bootstrap(rows, n_resamples=1000, seed=20261105, confidence=0.95):
    """Website cluster bootstrap; not a measurement of run-to-run variance."""
    groups = defaultdict(list)
    for row in rows:
        groups[row.get("website", "unknown")].append(row.get("value"))
    known = [x for xs in groups.values() for x in xs if x is not None]
    total = sum(len(xs) for xs in groups.values())
    result = {"estimate": sum(known) / len(known) if known and len(known) == total else None,
              "lower": None, "upper": None, "confidence": confidence, "clusters": len(groups), "N": total,
              "cluster_unit": "website", "scope": "registered sample uncertainty, not run-to-run variance"}
    if len(known) != total:
        return {**result, "status": "incomplete_registered_outcomes"}
    if len(groups) < 2:
        return {**result, "status": "fewer_than_two_clusters"}
    rng = random.Random(seed)
    keys = sorted(groups)
    samples = []
    for _ in range(n_resamples):
        values = [x for key in rng.choices(keys, k=len(keys)) for x in groups[key]]
        samples.append(sum(values) / len(values))
    samples.sort()
    alpha = (1 - confidence) / 2
    return {**result, "lower": samples[min(len(samples) - 1, int(alpha * len(samples)))],
            "upper": samples[min(len(samples) - 1, int((1 - alpha) * len(samples)))], "status": "ok"}
