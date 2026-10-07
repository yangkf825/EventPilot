"""Registered, denominator-aware metrics for the online v2 benchmark.

Decisions are predictions; execution facts must come from the independent
``evaluation`` block.  A missing provider/environment outcome is unknown, never
a zero.  Every rate includes its registered denominator, coverage and bounds.
Repeated runs are scored against the same cases once per observed repeat ID.
The runner must pass cases for its selected experiment and one model at a time.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
import random
import copy
from typing import Any

DECISIONS = ("IGNORE", "DEFER", "INTERRUPT")
FOLLOW_UPS = ("HANDLE", "REPLAN", "TERMINATE")
BRANCHES = ("IGNORE", "DEFER", "HANDLE", "REPLAN", "TERMINATE")
BRANCH_ABBREVIATIONS = dict(zip(BRANCHES, ("I", "D", "H", "R", "T")))
INFRA_PROVIDER = {"api_error", "provider_error", "provider_unavailable"}
INFRA_ENVIRONMENT = {
    "execution_error", "browser_error", "website_unavailable", "environment_error",
    "trigger_not_reached", "checkpoint_not_reached", "browser_or_setup_error",
    "checkpoint_replay_mismatch", "checkpoint_unavailable", "checkpoint_capture_unavailable", "checkpoint_projection_unavailable", "checkpoint_setup_unverified", "registered_pending", "counterfactual_protocol_invalid",
}


def _ratio(n: int | float, total: int) -> float | None:
    return n / total if total else None


def _bool(value: Any) -> bool | None:
    """Do not silently accept 0/1, strings, or completion claims as truth."""
    return value if isinstance(value, bool) else None


def tri_and(values: Sequence[bool | None]) -> bool | None:
    """False evidence dominates uncertainty; an empty requirement is true."""
    if any(value is False for value in values):
        return False
    return None if any(value is None for value in values) else True


def summarize_tristate(values: Sequence[bool | None]) -> dict[str, Any]:
    values = [_bool(value) for value in values]
    total = len(values)
    passed = sum(value is True for value in values)
    failed = sum(value is False for value in values)
    unknown = total - passed - failed
    known = passed + failed
    return {
        "rate": _ratio(passed, total) if unknown == 0 else None,
        "n": passed, "N": total, "successes": passed, "failures": failed,
        "known": known, "unknown": unknown,
        "coverage": _ratio(known, total),
        "lower_bound": _ratio(passed, total),
        "upper_bound": _ratio(passed + unknown, total),
        "covered_rate": _ratio(passed, known),
    }


def fixed_macro_f1(gold: Sequence[str], predicted: Sequence[str | None],
                   classes: Sequence[str] = DECISIONS) -> tuple[float | None, dict]:
    """Fixed-class F1: an absent class contributes zero, invalid outputs FN."""
    if len(gold) != len(predicted):
        raise ValueError("Gold and prediction lengths differ")
    if any(label not in classes for label in gold):
        raise ValueError("Gold contains an unregistered class")
    by_class = {}
    for label in classes:
        tp = sum(g == label and p == label for g, p in zip(gold, predicted))
        fp = sum(g != label and p == label for g, p in zip(gold, predicted))
        fn = sum(g == label and p != label for g, p in zip(gold, predicted))
        denominator = 2 * tp + fp + fn
        by_class[label] = {"tp": tp, "fp": fp, "fn": fn,
                           "support": sum(g == label for g in gold),
                           "f1": 2 * tp / denominator if denominator else 0.0}
    score = sum(row["f1"] for row in by_class.values()) / len(classes) if gold else None
    return score, by_class


def _case_id(case: Mapping) -> str:
    value = case.get("case_id", case.get("id"))
    if not isinstance(value, str) or not value:
        raise ValueError("Each case must have a nonempty case_id")
    return value


def _events(case: Mapping) -> list[dict]:
    rows = case.get("events")
    if rows is None:
        event = case.get("event")
        rows = [event] if isinstance(event, Mapping) else []
    result = []
    for index, event in enumerate(rows):
        if not isinstance(event, Mapping):
            raise ValueError("Case events must be mappings")
        result.append({**event, "id": str(event.get("id", f"E{index + 1}"))})
    ids = [event["id"] for event in result]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate event IDs in case")
    return result


def _gold(case: Mapping) -> dict[str, dict]:
    gold = case.get("gold", {})
    events = _events(case)
    if not events:
        return {}
    if not isinstance(gold, Mapping):
        gold = {"decision": gold}
    labels = gold.get("decisions")
    if labels is None:
        labels = {events[0]["id"]: gold}
    result = {}
    for event in events:
        label = labels.get(event["id"])
        if isinstance(label, str):
            label = {"decision": label}
        if not isinstance(label, Mapping) or label.get("decision") not in DECISIONS:
            raise ValueError(f"Missing/invalid registered gold for {event['id']}")
        follow_up = label.get("follow_up", label.get("followup"))
        if label["decision"] == "INTERRUPT" and follow_up not in FOLLOW_UPS:
            raise ValueError("INTERRUPT gold requires HANDLE/REPLAN/TERMINATE")
        result[event["id"]] = {"decision": label["decision"], "follow_up": follow_up}
    if set(result) != set(labels):
        raise ValueError("Gold decisions must match registered event IDs")
    return result


def _evaluation(episode: Mapping | None) -> Mapping:
    return episode.get("evaluation", {}) if isinstance(episode, Mapping) else {}


def _execution_evaluation(episode: Mapping | None) -> Mapping:
    """Unavailable execution is not a comparable zero on behavior diagnostics.

    Exposure and raw episode traces remain factual. A field defaulting to false
    because a website/provider failed cannot establish an agent execution error.
    """
    evaluation = _evaluation(episode)
    status = evaluation.get("status", (episode or {}).get("status"))
    if evaluation.get("environment_valid") is not False and status not in INFRA_PROVIDER | INFRA_ENVIRONMENT:
        return evaluation
    masked = dict(evaluation)
    for name in ("episode_success", "branch_success", "task_success", "updated_goal_success", "effective_constraints",
                 "resumed", "resume_completion", "order_correct", "schedule_exact_match", "actual_schedule",
                 "distraction", "cancelled", "stopped", "post_cancel_action_count", "post_cancel_violation",
                 "replan_adopted", "termination_compliant"):
        masked[name] = None
    masked["task_completion_results"] = {"CURRENT_TASK": None}
    masked["phase_results"] = {node: {**facts, "success": None} if isinstance(facts, Mapping) else {"success": None}
                               for node, facts in evaluation.get("phase_results", {}).items()}
    masked["event_results"] = {eid: {**facts, "success": None, "started": None, "timely": None,
                                    "unnecessary_action_count": None}
                               for eid, facts in evaluation.get("event_results", {}).items()}
    constraints = evaluation.get("effective_constraint_results", evaluation.get("constraint_results"))
    if isinstance(constraints, Mapping):
        constraints = list(constraints.values())
    masked["effective_constraint_results"] = [{**value, "success": None, "satisfied": None} if isinstance(value, Mapping)
                                               else None for value in constraints] if isinstance(constraints, list) else None
    return masked


def _prediction(episode: Mapping | None, eid: str, only: bool = False) -> tuple[Any, Any]:
    if not episode:
        return None, None
    decisions = episode.get("decisions", {})
    decision = decisions.get(eid) if isinstance(decisions, Mapping) else decisions if only else None
    follow_ups = episode.get("follow_ups", episode.get("followups", {}))
    follow_up = follow_ups.get(eid) if isinstance(follow_ups, Mapping) else follow_ups if only else None
    if isinstance(decision, Mapping):
        follow_up = decision.get("follow_up", decision.get("followup", follow_up))
        decision = decision.get("decision")
    if only and decision is None:
        prediction = episode.get("prediction", {})
        if isinstance(prediction, Mapping):
            decision = prediction.get("decision", episode.get("decision"))
            follow_up = prediction.get("follow_up", episode.get("follow_up", follow_up))
        else:
            decision = episode.get("decision")
    return decision, follow_up


def _decision_unknown(episode: Mapping | None, prediction: Any) -> bool:
    if episode is None:
        return True
    # Existing valid decisions remain evaluable if a later navigation call fails.
    if prediction is not None:
        return False
    status = _evaluation(episode).get("status", episode.get("status"))
    return episode.get("api_ok") is False or status in INFRA_PROVIDER | INFRA_ENVIRONMENT


def _branch(case: Mapping) -> str:
    labels = _gold(case)
    if not labels:
        return "NO_EVENT"
    if len(labels) != 1:
        return "MULTI"
    label = next(iter(labels.values()))
    return label["follow_up"] if label["decision"] == "INTERRUPT" else label["decision"]


def _event_facts(evaluation: Mapping, eid: str) -> Mapping:
    return evaluation.get("event_results", {}).get(eid, {})


def _phase_success(evaluation: Mapping, node: str) -> bool | None:
    row = evaluation.get("phase_results", {}).get(node, {})
    return _bool(row.get("success")) if isinstance(row, Mapping) else _bool(row)


def _event_success(evaluation: Mapping, eid: str) -> bool | None:
    facts = _event_facts(evaluation, eid)
    if "success" in facts:
        return _bool(facts["success"])
    return _phase_success(evaluation, eid)


def _fact(evaluation: Mapping, name: str, fallback: str | None = None) -> bool | None:
    if name in evaluation:
        return _bool(evaluation[name])
    return _bool(evaluation.get(fallback)) if fallback else None


def _actual_success(case: Mapping, episode: Mapping | None) -> bool | None:
    evaluation = _execution_evaluation(episode)
    if "branch_success" in evaluation:
        return _bool(evaluation["branch_success"])
    if "episode_success" in evaluation:
        return _bool(evaluation["episode_success"])
    # A missing evaluation is not a model failure, even when it claimed done.
    return None


def _decision_records(rows: Sequence[dict]) -> list[dict]:
    result = []
    for row in rows:
        gold = _gold(row["case"])
        for eid, label in gold.items():
            decision, follow_up = _prediction(row["episode"], eid, len(gold) == 1)
            unknown = _decision_unknown(row["episode"], decision)
            state = (row["episode"] or {}).get("events", {}).get(eid, {})
            facts = _event_facts(_evaluation(row["episode"]), eid)
            delivered = _bool(state.get("delivered", facts.get("delivered")))
            if delivered is False:
                unknown = True
            result.append({**row, "event_id": eid, "gold_decision": label["decision"],
                           "gold_follow_up": label["follow_up"], "decision": decision,
                           "follow_up": follow_up, "decision_unknown": unknown,
                           "delivered": delivered,
                           "decision_correct": None if unknown else decision == label["decision"]})
    return result


def _classification(records: Sequence[dict], classes=DECISIONS,
                    gold_key="gold_decision", prediction_key="decision") -> dict:
    known = [row for row in records if not row.get("decision_unknown", False)]
    gold = [row[gold_key] for row in known]
    predicted = [row[prediction_key] for row in known]
    score, by_class = fixed_macro_f1(gold, predicted, classes)
    full = len(known) == len(records)
    return {"macro_f1": score if full else None, "covered_macro_f1": score,
            "classes": by_class, "known": len(known), "N": len(records),
            "unknown": len(records) - len(known), "coverage": _ratio(len(known), len(records))}


def _put(output: dict, name: str, values: Sequence[bool | None], *aliases: str) -> dict:
    stats = summarize_tristate(values)
    output[name] = stats["rate"]
    output["denominators"][name] = stats
    for alias in aliases:
        output[alias] = stats["rate"]
        output["denominators"][alias] = stats
    return stats


def _plan(case: Mapping) -> tuple[list[str], list[tuple[str, str]]]:
    gold = case.get("gold", {})
    labels = _gold(case)
    current = "TERMINATE_TASK" if any(label["follow_up"] == "TERMINATE" for label in labels.values()) else "CURRENT_TASK"
    nodes = gold.get("required_nodes") if isinstance(gold, Mapping) else None
    if nodes is None:
        nodes = [current] + [eid for eid, label in labels.items() if label["decision"] != "IGNORE"]
    nodes = [str(node) for node in nodes]
    edges = gold.get("precedence", []) if isinstance(gold, Mapping) else []
    if not edges:
        edges = [(eid, current) if label["decision"] == "INTERRUPT" else (current, eid)
                 for eid, label in labels.items() if label["decision"] != "IGNORE"]
    edges = [(str(edge[0]), str(edge[1])) for edge in edges]
    if len(nodes) != len(set(nodes)) or any(a == b or a not in nodes or b not in nodes for a, b in edges):
        raise ValueError("Invalid registered schedule nodes/precedence")
    # A benchmark DAG must be acyclic, independently of any submitted schedule.
    pending = set(nodes)
    while pending:
        roots = {node for node in pending if not any(b == node and a in pending for a, b in edges)}
        if not roots:
            raise ValueError("Registered precedence contains a cycle")
        pending -= roots
    return nodes, edges


def is_legal_schedule(case: Mapping, schedule: Any) -> bool:
    """Accept every topological ordering, not just an author's sample order."""
    nodes, edges = _plan(case)
    return _is_legal_plan(nodes, edges, schedule)


def _is_legal_plan(nodes: Sequence[str], edges: Sequence[tuple[str, str]], schedule: Any) -> bool:
    if not isinstance(schedule, (list, tuple)) or any(not isinstance(node, str) for node in schedule):
        return False
    if len(schedule) != len(nodes) or set(schedule) != set(nodes):
        return False
    position = {node: index for index, node in enumerate(schedule)}
    return all(position[a] < position[b] for a, b in edges)


def _schedule_edges(case: Mapping, schedule: Any) -> list[bool]:
    nodes, edges = _plan(case)
    return _plan_edges(nodes, edges, schedule)


def _plan_edges(nodes: Sequence[str], edges: Sequence[tuple[str, str]], schedule: Any,
                *, require_scope: bool = False) -> list[bool]:
    if not isinstance(schedule, (list, tuple)) or any(not isinstance(node, str) for node in schedule):
        return [False] * len(edges)
    # Duplicated work is not a well-defined priority plan.
    if len(schedule) != len(set(schedule)):
        return [False] * len(edges)
    if require_scope and any(node not in nodes for node in schedule):
        return [False] * len(edges)
    position = {node: index for index, node in enumerate(schedule)}
    return [a in position and b in position and position[a] < position[b] for a, b in edges]


def _stage_plan(case: Mapping, known_ids: Sequence[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Project the registered DAG onto events actually visible at this stage.

    Hidden future nodes/edges cannot impose a plan obligation. In particular a
    future cancellation cannot change the visible task node to TERMINATE_TASK.
    This projection is exclusively an evaluator operation, never model input.
    """
    labels = _gold(case)
    if (not isinstance(known_ids, (list, tuple)) or any(not isinstance(eid, str) for eid in known_ids)
            or len(known_ids) != len(set(known_ids)) or not set(known_ids) <= set(labels)):
        raise ValueError("Decision-stage known event IDs must be unique registered IDs")
    nodes, edges = _plan(case)  # Validate the complete registered DAG first.
    known = set(known_ids)
    current = ("TERMINATE_TASK" if any(labels[eid]["follow_up"] == "TERMINATE" for eid in known)
               else "CURRENT_TASK")
    task_nodes = {"CURRENT_TASK", "TERMINATE_TASK"}
    visible_nodes = [current] + [node for node in nodes if node in known]
    visible = set(visible_nodes)
    visible_edges = []
    for a, b in edges:
        a = current if a in task_nodes else a
        b = current if b in task_nodes else b
        if a in visible and b in visible and (a, b) not in visible_edges:
            visible_edges.append((a, b))
    return visible_nodes, visible_edges


def _planning_stages(episode: Mapping | None, records: Sequence[dict]) -> list[dict]:
    """Use retained input scopes; legacy records support the last visible plan.

    Reconstructing earlier scopes from final predictions would turn a model
    claim into exposure evidence. A legacy record therefore contributes only
    its final delivered-event scope, explicitly marked in the report.
    """
    if episode is None:
        return [{"known_event_ids": None, "parsed": None, "api_ok": False, "source": "unavailable"}]
    stages = episode.get("decision_stages")
    if stages:
        if not isinstance(stages, (list, tuple)) or any(not isinstance(stage, Mapping) for stage in stages):
            raise ValueError("Decision stages must be mappings")
        return [{**stage, "source": "decision_stages"} for stage in stages]
    known = [record["event_id"] for record in records if record["delivered"] is True]
    if not known:
        return [{"known_event_ids": None, "parsed": None, "api_ok": False, "source": "unavailable"}]
    return [{"known_event_ids": known,
             "parsed": {"schedule": episode.get("predicted_schedule", episode.get("schedule"))},
             "api_ok": not all(record["decision_unknown"] for record in records if record["event_id"] in known),
             "source": "legacy_last_visible_plan"}]


def _records(cases: Sequence[Mapping], episodes: Sequence[Mapping], experiment: str, view: str) -> tuple[list[dict], int]:
    by_case = {_case_id(case): case for case in cases}
    if len(by_case) != len(cases):
        raise ValueError("Registered case IDs must be unique")
    selected = []
    for episode in episodes:
        if not isinstance(episode, Mapping):
            raise ValueError("Episodes must be mappings")
        if episode.get("experiment", experiment) != experiment:
            continue
        if episode.get("view", episode.get("setting", view)) != view:
            continue
        selected.append(episode)
    repeats = sorted({episode.get("repeat", 0) for episode in selected} or {0}, key=str)
    by_key = {}
    orphan = 0
    for episode in selected:
        cid = episode.get("case_id")
        if cid not in by_case:
            orphan += 1
            continue
        key = (cid, episode.get("repeat", 0))
        if key in by_key:
            raise ValueError("Duplicate episode for a case/repeat; score each model separately")
        by_key[key] = None if episode.get("status") == "registered_pending" else episode
    rows = [{"case": case, "case_id": cid, "repeat": repeat,
             "task_family_id": case.get("task_family_id", case.get("source_task_id", cid)),
             "episode": by_key.get((cid, repeat))}
            for repeat in repeats for cid, case in by_case.items()]
    return rows, orphan


def _failure_category(row: dict, event_records: Sequence[dict]) -> str:
    episode = row["episode"]
    if episode is None:
        return "missing_episode"
    evaluation = _evaluation(episode)
    status = evaluation.get("status", episode.get("status"))
    success = _actual_success(row["case"], episode)
    if success is None:
        if status in INFRA_PROVIDER:
            return "provider_error"
        if status in INFRA_ENVIRONMENT or evaluation.get("environment_valid") is False:
            return "environment_error"
        if episode.get("api_ok") is False:
            return "provider_error"
        return "unverified"
    if success is True:
        return "success"
    selected = [record for record in event_records if record["case_id"] == row["case_id"] and record["repeat"] == row["repeat"]]
    if any(record["decision_correct"] is False for record in selected):
        return "decision_error"
    if any(record["gold_decision"] == "INTERRUPT" and record["decision"] == "INTERRUPT"
           and record["follow_up"] != record["gold_follow_up"] for record in selected):
        return "follow_up_error"
    return "execution_failure"


def _execution_scores(output: dict, rows: Sequence[dict], records: Sequence[dict]) -> None:
    branch_values = {branch: [] for branch in BRANCHES}
    all_values, event_values, task_values = [], [], []
    for row in rows:
        case, episode = row["case"], row["episode"]
        evaluation = _execution_evaluation(episode)
        value = _actual_success(case, episode)
        all_values.append(value)
        branch = _branch(case)
        if branch in branch_values:
            branch_values[branch].append(value)
        for eid, label in _gold(case).items():
            if label["decision"] != "IGNORE":
                event_values.append(_event_success(evaluation, eid))
        # The evaluator registers which factual task outputs belong to the
        # effective goal; cancellation and superseded goals are not failures.
        registered_tasks = evaluation.get("task_completion_results")
        cancelled = branch == "TERMINATE" or any(label["follow_up"] == "TERMINATE" for label in _gold(case).values())
        if not cancelled:
            if isinstance(registered_tasks, Mapping) and "CURRENT_TASK" in registered_tasks:
                result = registered_tasks["CURRENT_TASK"]
                task_values.append(_bool(result.get("success")) if isinstance(result, Mapping) else _bool(result))
            elif branch == "REPLAN":
                task_values.append(_fact(evaluation, "updated_goal_success"))
            else:
                task_values.append(_fact(evaluation, "task_success"))
    branch_stats = {}
    for branch, values in branch_values.items():
        stats = _put(output, f"{BRANCH_ABBREVIATIONS[branch]}_SR", values)
        branch_stats[branch] = stats
    # Equal weight to all five branches. Missing/unknown branches cannot quietly
    # disappear from the macro denominator.
    output["MacroESR"] = (sum(stats["rate"] for stats in branch_stats.values()) / len(BRANCHES)
                          if all(stats["rate"] is not None for stats in branch_stats.values()) else None)
    available = [stats for stats in branch_stats.values() if stats["N"]]
    output["denominators"]["MacroESR"] = {
        "N": len(BRANCHES), "known": sum(stats["rate"] is not None for stats in branch_stats.values()),
        "branches": branch_stats,
        "lower_bound": sum(stats["lower_bound"] or 0.0 for stats in branch_stats.values()) / len(BRANCHES),
        "upper_bound": sum(stats["upper_bound"] if stats["N"] else 1.0 for stats in branch_stats.values()) / len(BRANCHES),
        "covered_branch_mean": sum(stats["covered_rate"] for stats in available if stats["covered_rate"] is not None)
                               / sum(stats["covered_rate"] is not None for stats in available)
                               if any(stats["covered_rate"] is not None for stats in available) else None,
    }
    micro = _put(output, "MicroESR", all_values, "episode_success_rate")
    _put(output, "EHS", event_values, "event_handling_success")
    _put(output, "TaskCompletion", task_values, "task_completion_rate")
    output["Coverage"] = micro["coverage"]
    output["coverage"] = micro["coverage"]
    output["denominators"]["Coverage"] = {"n": micro["known"], "N": micro["N"], "rate": micro["coverage"]}
    categories = Counter(_failure_category(row, records) for row in rows)
    names = ("success", "decision_error", "follow_up_error", "execution_failure",
             "provider_error", "environment_error", "unverified", "missing_episode")
    output["failure_ledger"] = {"N": len(rows), "counts": {name: categories[name] for name in names},
                                "mutually_exclusive": True,
                                "rows": [{"case_id": row["case_id"], "repeat": row["repeat"],
                                          "category": _failure_category(row, records)} for row in rows]}


def _counterfactual_scores(output: dict, rows: Sequence[dict], records: Sequence[dict]) -> None:
    output["raw_decision_accuracy"] = output["accuracy"]
    output["raw_macro_f1"] = output["macro_f1"]
    record_by_key = defaultdict(list)
    for record in records:
        record_by_key[(record["case_id"], record["repeat"])].append(record)
    raw_correct = {(row["case_id"], row["repeat"]): tri_and([record["decision_correct"] for record in record_by_key[(row["case_id"], row["repeat"])]])
               for row in rows}
    correct = {key: value for key, value in raw_correct.items()}
    protocol_values = []
    for row in rows:
        valid = _bool((row["episode"] or {}).get("counterfactual_protocol_valid"))
        protocol_values.append(valid)
        if valid is not True:
            correct[(row["case_id"], row["repeat"])] = None
    groups = defaultdict(list)
    type_rows = defaultdict(list)
    aliases = {"goal-aware": "goal", "goal_aware": "goal", "state-aware": "state", "state_aware": "state",
               "semantic-aware": "semantic", "semantic_aware": "semantic"}
    for row in rows:
        case = row["case"]
        kind = case.get("cf_type", case.get("group_type", case.get("counterfactual_type")))
        kind = aliases.get(kind, kind)
        if kind not in {"goal", "state", "semantic"}:
            continue
        group = case.get("cf_group_id", case.get("group_id"))
        if group is None:
            raise ValueError("Counterfactual case requires a group ID")
        type_rows[kind].append(row)
        groups[(kind, str(group), row["repeat"])].append(row)
    group_results = defaultdict(list)
    details = []
    for (kind, group, repeat), members in groups.items():
        expected = 3 if kind == "semantic" else 2
        if len(members) > expected:
            raise ValueError("Counterfactual group exceeds its registered pair/triple size")
        group_valid = len(members) == expected and all(
            (row["episode"] or {}).get("counterfactual_protocol_valid") is True for row in members)
        if not group_valid:
            for row in members:
                correct[(row["case_id"], repeat)] = None
        values = [correct[(row["case_id"], repeat)] for row in members]
        value = tri_and(values) if group_valid else None
        group_results[kind].append(value)
        details.append({"type": kind, "group_id": group, "repeat": repeat,
                        "case_ids": [row["case_id"] for row in members],
                        "registered_size": expected, "selected_size": len(members),
                        "protocol_valid": group_valid, "correct": value})
    overall = _put(output, "overall_accuracy", list(correct.values()), "OverallAcc")
    tables = {}
    for kind, metric in (("goal", "CFA"), ("state", "SFA"), ("semantic", "SemanticRobustAccuracy")):
        stats = _put(output, metric, group_results[kind])
        case_stats = summarize_tristate([correct[(row["case_id"], row["repeat"])] for row in type_rows[kind]])
        member_records = [{**record, "decision_unknown": record["decision_unknown"] or correct[(record["case_id"], record["repeat"])] is None}
                          for record in records if any(record["case_id"] == row["case_id"] and record["repeat"] == row["repeat"] for row in type_rows[kind])]
        f1 = _classification(member_records)
        tables[kind] = {"case_accuracy": case_stats["rate"], "group_accuracy": stats["rate"],
                        "metric": metric, "case_counts": case_stats, "group_counts": stats,
                        "macro_f1": f1["macro_f1"], "class_counts": f1}
    output["counterfactual"] = {"overall": overall, "types": tables, "groups": details,
                                "raw_case_accuracy": summarize_tristate(list(raw_correct.values())),
                                "protocol_validity": summarize_tristate(protocol_values)}
    common_names = ('Acc','MacroF1','FIR','MIR','FollowupAcc','FollowupF1','JointFUAcc',
                    'I_SR','D_SR','H_SR','R_SR','T_SR','MacroESR','MicroESR','EHS','TaskCompletion','Coverage',
                    'accuracy','decision_accuracy','macro_f1','decision_valid_rate',
                    'false_interruption_rate','missed_interruption_rate','follow_up_accuracy','FollowUpAcc',
                    'follow_up_macro_f1','joint_follow_up_accuracy','episode_success_rate',
                    'event_handling_success','task_completion_rate','coverage')
    plain_cases = [{k: copy.deepcopy(v) for k,v in row['case'].items()
                    if k not in {'group_type','cf_type','counterfactual_type'}} for row in rows]
    # Collapse case repeats; registered denominators remain intact in score_cases.
    plain_cases = list({_case_id(c): c for c in plain_cases}.values())
    formal_episodes = []
    invalid_keys = {(cid, detail['repeat']) for detail in details if not detail['protocol_valid'] for cid in detail['case_ids']}
    for row in rows:
        episode = copy.deepcopy(row['episode'])
        if episode is None:
            # Keep every registered repeat even if all its members are missing.
            formal_episodes.append({'case_id': row['case_id'], 'repeat': row['repeat'],
                                    'experiment': 'main', 'view': output['view'],
                                    'status': 'registered_pending'})
            continue
        # Internal common scoring uses main, so relabel the COPY explicitly.
        # Otherwise _records filters genuine counterfactual records away.
        episode['experiment'] = 'main'
        if (row['case_id'], row['repeat']) in invalid_keys or episode.get('counterfactual_protocol_valid') is not True:
            episode.update(status='counterfactual_protocol_invalid', api_ok=False, decisions={},
                           follow_ups={}, responses=[], decision_stages=[])
            episode['evaluation'] = {**episode.get('evaluation',{}), 'environment_valid':False,
                                     'episode_success':None,'branch_success':None,'status':'counterfactual_protocol_invalid'}
        formal_episodes.append(episode)
    common = score_cases(plain_cases, formal_episodes, experiment='main', view=output['view'])
    output['raw_common_metrics'] = {name:output.get(name) for name in common_names}
    for name in common_names:
        output[name] = common.get(name)
        if name in common['denominators']:
            output['denominators'][name] = common['denominators'][name]
    for kind, table in tables.items():
        ids = {r['case_id'] for r in type_rows[kind]}
        subtype = score_cases([c for c in plain_cases if c['case_id'] in ids],
                              [e for e in formal_episodes if e['case_id'] in ids], experiment='main',view=output['view'])
        table['common_metrics'] = {name:subtype.get(name) for name in common_names}
        table['common_denominators'] = subtype['denominators']
    output['raw_diagnostics'] = copy.deepcopy(output.get('diagnostics', {}))
    output['diagnostics'] = copy.deepcopy(common.get('diagnostics', {}))
    for name in output['diagnostics']:
        output[name] = common.get(name)
        output['denominators'][name] = common['denominators'][name]
    output['raw_failure_ledger'] = copy.deepcopy(output.get('failure_ledger', {}))
    output['failure_ledger'] = copy.deepcopy(common.get('failure_ledger', {}))
    output['success_funnel'] = copy.deepcopy(common.get('success_funnel', {}))
    output['controlled_execution_policy'] = 'Formal common and counterfactual scores require the injection-boundary invariant audit; raw factual outcomes retained separately.'

    output["semantic_robust_accuracy"] = output["SemanticRobustAccuracy"]
    output["follow_up_classification"] = copy.deepcopy(common["follow_up_classification"])
    _put(output, "accuracy", list(correct.values()), "Acc", "decision_accuracy")
    formal_records = [{**record, "decision_unknown": record["decision_unknown"] or correct[(record["case_id"], record["repeat"])] is None}
                      for record in records]
    formal_f1 = _classification(formal_records)
    output.update(macro_f1=formal_f1["macro_f1"], MacroF1=formal_f1["macro_f1"], classification=formal_f1)


def _multi_scores(output: dict, rows: Sequence[dict], records: Sequence[dict]) -> None:
    """Separate visible-stage plan reports from whole-episode execution facts."""
    by_key = defaultdict(list)
    for record in records:
        by_key[(record["case_id"], record["repeat"])].append(record)
    complete, planned_edges, priority_cases, planned_exact = [], [], [], []
    priority_stages, planned_case_exact, plan_details = [], [], []
    actual_edges, actual_priority_cases, actual_exact, critical, actual_critical = [], [], [], [], []
    for row in rows:
        episode, case = row["episode"], row["case"]
        evaluation = _execution_evaluation(episode)
        selected = by_key[(row["case_id"], row["repeat"])]
        # This whole-case conjunction keeps unexposed decisions unknown. A
        # verified wrong visible decision still establishes a case failure.
        complete.append(tri_and([record["decision_correct"] for record in selected]))
        stage_priorities, stage_matches = [], []
        for stage_index, stage in enumerate(_planning_stages(episode, selected)):
            known_ids = stage.get("known_event_ids")
            parsed = stage.get("parsed")
            schedule = parsed.get("schedule") if isinstance(parsed, Mapping) else None
            if known_ids is None:
                nodes, edges, edge_values = None, None, []
                priority, exact = None, None
            else:
                nodes, edges = _stage_plan(case, known_ids)
                unknown = (stage.get("api_ok") is False
                           or (not isinstance(parsed, Mapping) and _bool(stage.get("api_ok")) is not True))
                edge_values = ([None] * len(edges) if unknown
                               else _plan_edges(nodes, edges, schedule, require_scope=True))
                valid_shape = (isinstance(schedule, (list, tuple))
                               and all(isinstance(node, str) for node in schedule)
                               and len(schedule) == len(set(schedule))
                               and set(schedule) <= set(nodes))
                priority = None if unknown else valid_shape and all(edge_values)
                exact = None if unknown else _is_legal_plan(nodes, edges, schedule)
            planned_edges.extend(edge_values)
            priority_stages.append(priority)
            stage_priorities.append(priority)
            planned_exact.append(exact)
            stage_matches.append(exact)
            plan_details.append({"case_id": row["case_id"], "repeat": row["repeat"],
                                 "stage_index": stage_index, "source": stage["source"],
                                 "action_index": stage.get("action_index"),
                                 "known_event_ids": known_ids, "required_nodes": nodes,
                                 "precedence": edges, "schedule": schedule,
                                 "priority_correct": priority, "schedule_exact_match": exact})
        priority_cases.append(tri_and(stage_priorities))
        planned_case_exact.append(tri_and(stage_matches))
        # Actual behavior retains the complete registered graph, including
        # events not reached because a valid actor exhausted its budget.
        actual_schedule = evaluation.get("actual_schedule", episode.get("actual_schedule") if episode else None)
        edges = _schedule_edges(case, actual_schedule)
        actual_edges.extend([None] * len(edges) if actual_schedule is None else edges)
        actual_priority_cases.append(_bool(evaluation["order_correct"]) if "order_correct" in evaluation
                                     else None if actual_schedule is None else all(edges))
        actual_exact.append(_bool(evaluation["schedule_exact_match"]) if "schedule_exact_match" in evaluation
                            else None if actual_schedule is None else is_legal_schedule(case, actual_schedule))
        critical_ids = case.get("gold", {}).get("critical_events")
        if critical_ids is None:
            critical_ids = [eid for eid, label in _gold(case).items() if label["decision"] == "INTERRUPT"]
        for eid in critical_ids:
            matching = next((record for record in selected if record["event_id"] == eid), None)
            if matching is None:
                raise ValueError("Critical event must be registered in case gold")
            critical.append(None if matching["decision_unknown"] else matching["decision"] == "INTERRUPT")
            actual_critical.append(_bool(_event_facts(evaluation, eid).get("timely")))
    _put(output, "CompleteCaseAcc", complete, "complete_case_accuracy")
    _put(output, "PriorityAcc", planned_edges, "priority_accuracy")
    _put(output, "PriorityCaseAcc", priority_cases)
    _put(output, "PriorityStageAcc", priority_stages)
    _put(output, "CriticalEventRecall", critical, "critical_event_recall")
    _put(output, "ScheduleExactMatch", planned_exact, "schedule_exact_match")
    _put(output, "ScheduleCaseExactMatch", planned_case_exact)
    _put(output, "ActualPriorityAcc", actual_edges, "actual_priority_accuracy")
    _put(output, "ActualPriorityCaseAcc", actual_priority_cases)
    _put(output, "ActualScheduleExactMatch", actual_exact, "actual_schedule_exact_match")
    _put(output, "ActualCriticalEventRecall", actual_critical)
    source_counts = Counter(detail["source"] for detail in plan_details)
    output["multi_plan_scoring"] = {
        "scope": "decision_stage_known_events_induced_registered_dag",
        "priority_aggregation": "micro_mean_of_visible_precedence_edges_across_observed_stages",
        "exact_match_aggregation": "one_value_per_decision_stage_or_unavailable_episode",
        "case_aggregation": "all_observed_stages_correct_per_registered_case_repeat",
        "execution_scope": "whole_episode_complete_registered_dag",
        "registered_episodes": len(rows),
        "observed_stages": sum(detail["known_event_ids"] is not None for detail in plan_details),
        "unavailable_scope_count": source_counts["unavailable"],
        "legacy_last_visible_plan_count": source_counts["legacy_last_visible_plan"],
        "source_counts": dict(source_counts), "stages": plan_details,
    }
    output["denominators"]["PriorityAcc"]["aggregation"] = output["multi_plan_scoring"]["priority_aggregation"]
    priority_stats = output["denominators"]["PriorityAcc"]
    priority_stats["unavailable_scope_count"] = source_counts["unavailable"]
    priority_stats["scope_coverage"] = _ratio(output["multi_plan_scoring"]["observed_stages"], len(plan_details))
    if source_counts["unavailable"]:
        # Unknown stage scopes have an unknown number of edge opportunities.
        # Do not invent full-Gold edges or advertise a covered subset as the
        # complete result. Its empirical edge mean remains in covered_rate.
        priority_stats.update(rate=None, rate_status="incomplete_stage_scope",
                              covered_lower_bound=priority_stats["lower_bound"],
                              covered_upper_bound=priority_stats["upper_bound"],
                              lower_bound=None, upper_bound=None)
        output["PriorityAcc"] = output["priority_accuracy"] = None
    output["denominators"]["ScheduleExactMatch"]["aggregation"] = output["multi_plan_scoring"]["exact_match_aggregation"]
    output["denominators"]["CompleteCaseAcc"]["scope"] = "all_registered_event_decisions_per_case_repeat"
    output["denominators"]["CriticalEventRecall"]["scope"] = "all_registered_critical_events_unexposed_unknown"
    output["EventF1"] = output["macro_f1"]
    output["event_f1"] = output["macro_f1"]
    output["multi_ESR"] = output["MicroESR"]


def _original_success(evaluation: Mapping, updated: bool = False) -> bool | None:
    name = "updated_goal_success" if updated else "task_success"
    if name in evaluation:
        return _bool(evaluation[name])
    return _phase_success(evaluation, "CURRENT_TASK")


def _started(facts: Mapping) -> bool | None:
    if "started" in facts:
        return _bool(facts["started"])
    for name in ("start_index", "started_index"):
        if name in facts:
            return facts[name] is not None
    return None


def _termination_compliant(evaluation: Mapping) -> bool | None:
    if "termination_compliant" in evaluation:
        return _bool(evaluation["termination_compliant"])
    resumed = _bool(evaluation.get("resumed"))
    violation = _bool(evaluation.get("post_cancel_violation"))
    return tri_and([_bool(evaluation.get("stopped")),
                    None if resumed is None else not resumed,
                    None if violation is None else not violation])


def _constraint_fraction(evaluation: Mapping) -> tuple[float | None, float | None, float | None]:
    constraints = evaluation.get("effective_constraint_results", evaluation.get("constraint_results"))
    if isinstance(constraints, Mapping):
        constraints = list(constraints.values())
    if not isinstance(constraints, list):
        # A failed conjunction does not reveal the fraction of satisfied clauses.
        return (1.0, 1.0, 1.0) if evaluation.get("effective_constraints") is True else (None, 0.0, 1.0)
    values = []
    for constraint in constraints:
        if isinstance(constraint, Mapping):
            if constraint.get("removed") is True or constraint.get("active") is False:
                continue
            values.append(_bool(constraint.get("success", constraint.get("satisfied"))))
        else:
            values.append(_bool(constraint))
    stats = summarize_tristate(values)
    return stats["rate"], stats["lower_bound"], stats["upper_bound"]


def _conditional_put(output: dict, name: str, observations: Sequence[tuple[bool | None, bool | None]]) -> None:
    selected = [value for eligible, value in observations if eligible is True]
    stats = _put(output, name, selected)
    stats["eligibility"] = {"eligible": len(selected), "ineligible": sum(eligible is False for eligible, _ in observations),
                            "unknown": sum(eligible is None for eligible, _ in observations), "registered": len(observations)}


def _diagnostic_scores(output: dict, rows: Sequence[dict], records: Sequence[dict]) -> None:
    values = defaultdict(list)
    conditions = defaultdict(list)
    constraints, constraints_bounds, cancel_counts = [], [], []
    for row in rows:
        evaluation = _execution_evaluation(row["episode"])
        branch = _branch(row["case"])
        labels = _gold(row["case"])
        eid = next(iter(labels), "E1")
        facts = _event_facts(evaluation, eid)
        event_success = _event_success(evaluation, eid)
        original_success = _original_success(evaluation)
        if branch == "IGNORE":
            values["ICR"].append(original_success)
            distraction = _bool(evaluation.get("distraction"))
            if distraction is None:
                count = facts.get("unnecessary_action_count")
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    distraction = count > 0
            values["EDR"].append(distraction)
        elif branch == "DEFER":
            values["OTS"].append(original_success)
            started = _started(facts)
            conditions["DER"].append((original_success, tri_and([started, _bool(evaluation.get("order_correct"))])))
            conditions["DES"].append((started, event_success))
        elif branch == "HANDLE":
            values["ETS"].append(tri_and([event_success, _bool(facts.get("timely"))]))
            conditions["RR"].append((event_success, _bool(evaluation.get("resumed"))))
            resumed = _bool(evaluation.get("resumed"))
            conditions["RCR"].append((resumed, _fact(evaluation, "resume_completion", "task_success")))
        elif branch == "REPLAN":
            values["RAR"].append(_bool(evaluation.get("replan_adopted")))
            values["UGS"].append(_original_success(evaluation, updated=True))
            fraction, lower, upper = _constraint_fraction(evaluation)
            constraints.append(fraction)
            constraints_bounds.append((lower, upper))
        elif branch == "TERMINATE":
            values["TermCR"].append(_termination_compliant(evaluation))
            count = evaluation.get("post_cancel_action_count")
            count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
            cancel_counts.append(count)
            violation = _bool(evaluation.get("post_cancel_violation"))
            values["post_cancel_violation_rate"].append(count > 0 if violation is None and count is not None else violation)
    for name in ("ICR", "EDR", "OTS", "ETS", "RAR", "UGS", "TermCR", "post_cancel_violation_rate"):
        _put(output, name, values[name])
    for name in ("DER", "DES", "RR", "RCR"):
        _conditional_put(output, name, conditions[name])
    n_constraints = len(constraints)
    known_constraints = [value for value in constraints if value is not None]
    output["ECS"] = sum(known_constraints) / n_constraints if n_constraints and len(known_constraints) == n_constraints else None
    output["denominators"]["ECS"] = {
        "N": n_constraints, "known": len(known_constraints), "unknown": n_constraints - len(known_constraints),
        "coverage": _ratio(len(known_constraints), n_constraints),
        "lower_bound": sum(lower or 0.0 for lower, _ in constraints_bounds) / n_constraints if n_constraints else None,
        "upper_bound": sum(upper if upper is not None else 1.0 for _, upper in constraints_bounds) / n_constraints if n_constraints else None,
        "aggregation": "equal_case_mean_of_active_constraint_fractions",
    }
    known_counts = [count for count in cancel_counts if count is not None]
    output["mean_post_cancel_action_count"] = sum(known_counts) / len(cancel_counts) if cancel_counts and len(known_counts) == len(cancel_counts) else None
    output["denominators"]["mean_post_cancel_action_count"] = {
        "N": len(cancel_counts), "known": len(known_counts), "unknown": len(cancel_counts) - len(known_counts),
        "total_actions": sum(known_counts), "lower_bound": _ratio(sum(known_counts), len(cancel_counts)),
        "upper_bound": output["mean_post_cancel_action_count"],
    }
    _put(output, "Exposure", [record["delivered"] for record in records], "event_exposure_rate")
    output['PTAR'] = output['post_cancel_violation_rate']
    output['denominators']['PTAR'] = copy.deepcopy(output['denominators']['post_cancel_violation_rate'])
    output["diagnostics"] = {name: {"rate": output[name], **output["denominators"][name]}
                             for name in ("ICR", "EDR", "OTS", "DER", "DES", "ETS", "RR", "RCR", "RAR", "ECS", "UGS", "TermCR",
                                          "post_cancel_violation_rate", "PTAR", "mean_post_cancel_action_count", "Exposure")}


def _success_funnel(output: dict, rows: Sequence[dict], records: Sequence[dict]) -> None:
    by_key = defaultdict(list)
    for record in records:
        by_key[(record["case_id"], record["repeat"])].append(record)
    stage_names = ("environment_valid", "event_exposed", "decision_correct", "follow_up_correct",
                   "required_event_success", "original_policy_success", "episode_success")
    values_by_stage = {name: [] for name in stage_names}
    for row in rows:
        evaluation = _execution_evaluation(row["episode"])
        selected = by_key[(row["case_id"], row["repeat"])]
        branch = _branch(row["case"])
        values_by_stage["environment_valid"].append(_bool(evaluation.get("environment_valid")))
        values_by_stage["event_exposed"].append(True if not selected else _bool(evaluation.get("exposed")) if "exposed" in evaluation
                                                else tri_and([record["delivered"] for record in selected]))
        values_by_stage["decision_correct"].append(tri_and([record["decision_correct"] for record in selected]))
        values_by_stage["follow_up_correct"].append(tri_and([
            None if record["decision_unknown"] else record["decision"] == "INTERRUPT" and record["follow_up"] == record["gold_follow_up"]
            for record in selected if record["gold_decision"] == "INTERRUPT"]))
        event_values = [_event_success(evaluation, record["event_id"]) for record in selected if record["gold_decision"] != "IGNORE"]
        values_by_stage["required_event_success"].append(tri_and(event_values))
        if branch == "HANDLE":
            policy = tri_and([_bool(evaluation.get("resumed")), _fact(evaluation, "resume_completion", "task_success")])
        elif branch == "REPLAN":
            policy = tri_and([_bool(evaluation.get("replan_adopted")), _bool(evaluation.get("updated_goal_success")),
                              _bool(evaluation.get("effective_constraints"))])
        elif branch == "TERMINATE":
            policy = _termination_compliant(evaluation)
        elif branch == "MULTI":
            policy = _bool(evaluation.get("order_correct"))
        else:
            policy = _original_success(evaluation)
        values_by_stage["original_policy_success"].append(policy)
        values_by_stage["episode_success"].append(_actual_success(row["case"], row["episode"]))
    cumulative = [True] * len(rows)
    stages = []
    for name in stage_names:
        current = values_by_stage[name]
        eligible = [index for index, value in enumerate(cumulative) if value is True]
        conditional = summarize_tristate([current[index] for index in eligible])
        cumulative = [tri_and([prior, value]) for prior, value in zip(cumulative, current)]
        stats = summarize_tristate(cumulative)
        stages.append({"stage": name, "conditional": conditional, "cumulative": stats,
                       "n": stats["n"], "N": len(rows), "conditional_n": conditional["n"], "conditional_N": len(eligible),
                       "passing_ids": [{"case_id": row["case_id"], "repeat": row["repeat"]}
                                       for row, value in zip(rows, cumulative) if value is True]})
    output["success_funnel"] = {"registered": len(rows), "nested": True, "stages": stages,
        "endpoint": "joint_report_and_execution_success",
        "note": "All prior report/behavior gates are required. The endpoint can differ from behavior-only MicroESR when declared decisions are wrong but actual execution succeeds."}


def score_cases(cases: Sequence[Mapping], episodes: Sequence[Mapping],
                experiment: str = "main", view: str = "trajectory") -> dict[str, Any]:
    """Score registered cases; episode presence never changes denominators.

    ``evaluation.branch_success``/``episode_success`` are independently checked
    outcomes.  ``decisions`` and ``follow_ups`` are model predictions.  Actual
    schedules are read from evaluation, never copied from predicted schedules.
    """
    cases, episodes = list(cases), list(episodes)
    rows, orphan = _records(cases, episodes, experiment, view)
    records = _decision_records(rows)
    output = {"schema_version": "online_v2_metrics.2", "experiment": experiment, "view": view,
              "registered_cases": len(cases), "registered_episodes": len(rows),
              "recorded_episodes": sum(row["episode"] is not None for row in rows),
              "orphan_episodes": orphan, "events": len(records), "denominators": {}}
    _put(output, "accuracy", [row["decision_correct"] for row in records], "Acc", "decision_accuracy")
    f1 = _classification(records)
    output.update(macro_f1=f1["macro_f1"], MacroF1=f1["macro_f1"], classification=f1)
    _put(output, "decision_valid_rate", [None if row["decision_unknown"] else row["decision"] in DECISIONS for row in records])
    non_interrupt = [row for row in records if row["gold_decision"] != "INTERRUPT"]
    interrupt = [row for row in records if row["gold_decision"] == "INTERRUPT"]
    _put(output, "false_interruption_rate", [None if row["decision_unknown"] else row["decision"] == "INTERRUPT" for row in non_interrupt], "FIR")
    _put(output, "missed_interruption_rate", [None if row["decision_unknown"] else row["decision"] != "INTERRUPT" for row in interrupt], "MIR")
    conditional = [row for row in interrupt if row["decision"] == "INTERRUPT" and not row["decision_unknown"]]
    _put(output, "follow_up_accuracy", [row["follow_up"] == row["gold_follow_up"] for row in conditional], "FollowupAcc", "FollowUpAcc")
    fu_f1 = _classification(conditional, FOLLOW_UPS, "gold_follow_up", "follow_up")
    output.update(follow_up_macro_f1=fu_f1["macro_f1"], FollowupF1=fu_f1["macro_f1"], follow_up_classification=fu_f1)
    output["denominators"]["follow_up_macro_f1"] = {"N": len(conditional), "eligible_gold_interrupts": len(interrupt),
                                                      "selection_coverage": _ratio(len(conditional), len(interrupt))}
    _put(output, "joint_follow_up_accuracy", [None if row["decision_unknown"] else row["decision"] == "INTERRUPT" and row["follow_up"] == row["gold_follow_up"] for row in interrupt], "JointFUAcc")
    _execution_scores(output, rows, records)
    _diagnostic_scores(output, rows, records)
    _success_funnel(output, rows, records)
    if experiment in {"counterfactual", "cf"} or any(case.get("cf_type", case.get("group_type")) in {"goal", "state", "semantic"} for case in cases):
        _counterfactual_scores(output, rows, records)
    if experiment in {"multi", "multi_event"} or any(len(_events(case)) > 1 for case in cases):
        _multi_scores(output, rows, records)
    return output


def cluster_bootstrap_ci(rows: Sequence[Mapping], value_key: str = "value",
                         cluster_key: str = "task_family_id", n_resamples: int = 1000,
                         confidence: float = 0.95, seed: int = 1729,
                         statistic: Callable[[list[Mapping]], float | None] | None = None) -> dict:
    """Percentile CI resampling task families, retaining repeats/group members.

    With no custom statistic, values must all be numeric/boolean and known.
    Unknown outcomes produce no CI; covered-subset CIs must not masquerade as a
    registered-set success rate. This helper never changes model predictions.
    """
    if n_resamples < 1 or not 0 < confidence < 1:
        raise ValueError("Positive resample count and 0 < confidence < 1 required")
    rows = list(rows)
    groups = defaultdict(list)
    for row in rows:
        cluster = row.get(cluster_key)
        if cluster is None:
            raise ValueError("Every bootstrap row needs its registered cluster ID")
        groups[str(cluster)].append(row)
    base = {"method": "percentile_task_family_cluster_bootstrap", "confidence": confidence,
            "seed": seed, "n_resamples": n_resamples, "clusters": len(groups), "N": len(rows)}
    if statistic is None:
        def statistic(sample):
            values = [row.get(value_key) for row in sample]
            if not values or any(not isinstance(value, (int, float, bool)) for value in values):
                return None
            return sum(values) / len(values)
    estimate = statistic(rows)
    if estimate is None or len(groups) < 2:
        return {**base, "estimate": estimate, "lower": None, "upper": None,
                "status": "unknown_outcomes" if estimate is None else "insufficient_clusters"}
    rng = random.Random(seed)
    keys = list(groups)
    samples = []
    for _ in range(n_resamples):
        draw = [row for key in rng.choices(keys, k=len(keys)) for row in groups[key]]
        value = statistic(draw)
        if value is None:
            return {**base, "estimate": estimate, "lower": None, "upper": None, "status": "unknown_resample"}
        samples.append(value)
    samples.sort()
    tail = (1 - confidence) / 2
    def quantile(p):
        position = p * (len(samples) - 1)
        lo, hi = int(position), min(int(position) + 1, len(samples) - 1)
        return samples[lo] + (samples[hi] - samples[lo]) * (position - lo)
    return {**base, "estimate": estimate, "lower": quantile(tail), "upper": quantile(1 - tail), "status": "ok"}
