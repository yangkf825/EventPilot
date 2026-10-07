#!/usr/bin/env python3
"""Pure scoring functions for EventArena v3.

API/transport failures are never converted into wrong decisions. A headline
metric is unavailable (None) when the calls required for it did not complete.
Malformed *successful* model replies are model errors and count as incorrect.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


DECISIONS = ("IGNORE", "DEFER", "INTERRUPT")
FOLLOW_UPS = ("HANDLE", "REPLAN", "TERMINATE")


def _safe_div(num: int, den: int) -> float | None:
    return num / den if den else None


def macro_f1(gold: list[str], pred: list[str | None], labels: tuple[str, ...]) -> float | None:
    """Fixed-label macro F1; invalid predictions are false negatives."""
    if not gold:
        return None
    scores = []
    for label in labels:
        tp = sum(g == label and p == label for g, p in zip(gold, pred))
        fp = sum(g != label and p == label for g, p in zip(gold, pred))
        fn = sum(g == label and p != label for g, p in zip(gold, pred))
        scores.append((2 * tp / (2 * tp + fp + fn)) if 2 * tp + fp + fn else 0.0)
    return sum(scores) / len(scores)


def _record(records: dict[tuple, dict[str, Any]], key: tuple) -> dict[str, Any] | None:
    return records.get(key)


def _ok(record: dict[str, Any] | None) -> bool:
    return record is not None and record.get("error") is None and record.get("api_ok") is True


def _label(record: dict[str, Any] | None, allowed: tuple[str, ...]) -> str | None:
    value = record.get("parsed") if record else None
    return value if isinstance(value, str) and value in allowed else None


def _base_coverage(cases: list[dict], records: dict[tuple, dict], view: str) -> dict[str, Any]:
    primary = [_record(records, (view, case["case_id"], "decision", None)) for case in cases]
    n = len(cases)
    return {
        "n": n,
        "primary_api_ok_n": sum(_ok(row) for row in primary),
        "primary_api_failure_n": sum(not _ok(row) for row in primary),
        "primary_valid_n": sum(_label(row, DECISIONS) is not None for row in primary),
    }


def score_single(cases: list[dict], records: dict[tuple, dict], view: str) -> dict[str, Any]:
    """Main and ablation metrics, with conditional second-stage scoring.

    `records` is keyed by (view, case_id, stage, event_id); event_id is None.
    Follow-up is evaluated only where gold and prediction are INTERRUPT.
    """
    result = _base_coverage(cases, records, view)
    gold = [case["gold"]["decision"] for case in cases]
    primary = [_record(records, (view, case["case_id"], "decision", None)) for case in cases]
    pred = [_label(row, DECISIONS) for row in primary]
    follow_cases = [case for case, prediction in zip(cases, pred)
                    if case["gold"]["decision"] == "INTERRUPT" and prediction == "INTERRUPT"]
    result["gold_interrupt_n"] = sum(g == "INTERRUPT" for g in gold)
    result["follow_up_n"] = len(follow_cases)
    follow_records = [_record(records, (view, case["case_id"], "follow_up", None)) for case in follow_cases]
    result["follow_up_api_ok_n"] = sum(_ok(row) for row in follow_records)
    result["follow_up_valid_n"] = sum(_label(row, FOLLOW_UPS) is not None for row in follow_records)
    result["follow_up_selection_rate"] = _safe_div(result["follow_up_n"], result["gold_interrupt_n"])

    if result["primary_api_failure_n"]:
        result.update({key: None for key in ("accuracy", "macro_f1", "fir", "mir")})
    else:
        result["accuracy"] = _safe_div(sum(g == p for g, p in zip(gold, pred)), len(gold))
        result["macro_f1"] = macro_f1(gold, pred, DECISIONS)
        non_interrupt = [(g, p) for g, p in zip(gold, pred) if g != "INTERRUPT"]
        interrupt = [(g, p) for g, p in zip(gold, pred) if g == "INTERRUPT"]
        result["fir"] = _safe_div(sum(p == "INTERRUPT" for _, p in non_interrupt), len(non_interrupt))
        result["mir"] = _safe_div(sum(p != "INTERRUPT" for _, p in interrupt), len(interrupt))

    if result["primary_api_failure_n"] or not follow_cases or result["follow_up_api_ok_n"] != len(follow_cases):
        result["follow_up_acc"] = None
        result["follow_up_f1"] = None
    else:
        follow_gold = [case["gold"]["follow_up"] for case in follow_cases]
        follow_pred = [_label(row, FOLLOW_UPS) for row in follow_records]
        result["follow_up_acc"] = _safe_div(
            sum(g == p for g, p in zip(follow_gold, follow_pred)), len(follow_gold)
        )
        result["follow_up_f1"] = macro_f1(follow_gold, follow_pred, FOLLOW_UPS)
    return result


def score_counterfactual(cases: list[dict], records: dict[tuple, dict], view: str = "state") -> dict[str, Any]:
    """Case accuracy and strict all-variants-correct group accuracies."""
    result = _base_coverage(cases, records, view)
    group_types = {"goal": "cfa", "state": "sfa", "semantic": "semantic_robust_accuracy"}
    by_group: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for case in cases:
        by_group[(case["group_type"], case["group_id"])].append(case)
    result["group_n"] = {metric: sum(kind == group_type for kind, _ in by_group)
                         for group_type, metric in group_types.items()}
    if result["primary_api_failure_n"]:
        result.update({"overall_acc": None, **{metric: None for metric in group_types.values()}})
        return result
    correct = {
        case["case_id"]: _label(_record(records, (view, case["case_id"], "decision", None)), DECISIONS)
        == case["gold"]["decision"] for case in cases
    }
    result["overall_acc"] = _safe_div(sum(correct.values()), len(cases))
    for group_type, metric in group_types.items():
        groups = [members for (kind, _), members in by_group.items() if kind == group_type]
        result[metric] = _safe_div(
            sum(all(correct[case["case_id"]] for case in members) for members in groups), len(groups)
        )
    return result


def _schedule_ok(schedule: Any, required_nodes: list[str], precedence: list[list[str]]) -> bool:
    if not isinstance(schedule, list) or not all(isinstance(node, str) for node in schedule):
        return False
    if len(schedule) != len(required_nodes) or len(schedule) != len(set(schedule)) or set(schedule) != set(required_nodes):
        return False
    positions = {node: i for i, node in enumerate(schedule)}
    return all(positions.get(before, float("inf")) < positions.get(after, -1)
               for before, after in precedence)


def _precedence_closure(precedence: list[list[str]]) -> set[tuple[str, str]]:
    """All comparable ordered node pairs implied by the annotated DAG."""
    edges = {(before, after) for before, after in precedence}
    while True:
        inferred = edges | {(a, d) for a, b in edges for c, d in edges if b == c}
        if inferred == edges:
            return edges
        edges = inferred


def score_multi(cases: list[dict], records: dict[tuple, dict], view: str = "state") -> dict[str, Any]:
    """Six multi-event metrics. Any valid topological order is accepted."""
    result = {"n_cases": len(cases), "n_events": sum(len(case["events"]) for case in cases)}
    primaries = [_record(records, (view, case["case_id"], "multi_decision", None)) for case in cases]
    result["primary_api_ok_n"] = sum(_ok(row) for row in primaries)
    result["primary_api_failure_n"] = len(cases) - result["primary_api_ok_n"]
    if result["primary_api_failure_n"]:
        result.update({key: None for key in ("event_f1", "complete_case_acc", "priority_acc",
                                                  "critical_event_recall", "fir", "schedule_exact_match")})
        result["follow_up_n"] = None
        return result

    event_gold: list[str] = []
    event_pred: list[str | None] = []
    complete: list[bool] = []
    relation_ok: list[bool] = []
    schedule_ok: list[bool] = []
    follow_needed: list[tuple[str, str, str]] = []
    valid_event_n = 0
    for case, row in zip(cases, primaries):
        payload = row.get("parsed") if isinstance(row.get("parsed"), dict) else {}
        decisions = payload.get("decisions") if isinstance(payload.get("decisions"), dict) else {}
        schedule = payload.get("schedule")
        cid = case["case_id"]
        case_correct = True
        for event in case["events"]:
            eid = event["id"]
            gold = case["gold"]["decisions"][eid]["decision"]
            prediction = decisions.get(eid)
            if prediction not in DECISIONS:
                prediction = None
            else:
                valid_event_n += 1
            event_gold.append(gold)
            event_pred.append(prediction)
            case_correct &= prediction == gold
            if gold == prediction == "INTERRUPT":
                follow_needed.append((cid, eid, case["gold"]["decisions"][eid]["follow_up"]))
        complete.append(case_correct)
        positions = ({node: i for i, node in enumerate(schedule)}
                     if isinstance(schedule, list) and all(isinstance(node, str) for node in schedule)
                     and len(schedule) == len(set(schedule)) else {})
        for before, after in _precedence_closure(case["gold"]["precedence"]):
            relation_ok.append(before in positions and after in positions and positions[before] < positions[after])
        schedule_ok.append(_schedule_ok(schedule, case["gold"]["required_nodes"], case["gold"]["precedence"]))

    result["valid_event_n"] = valid_event_n
    result["follow_up_n"] = len(follow_needed)
    follow_rows = [_record(records, (view, cid, "follow_up", eid)) for cid, eid, _ in follow_needed]
    result["follow_up_api_ok_n"] = sum(_ok(row) for row in follow_rows)
    result["event_f1"] = macro_f1(event_gold, event_pred, DECISIONS)
    result["complete_case_acc"] = _safe_div(sum(complete), len(complete))
    result["priority_acc"] = _safe_div(sum(relation_ok), len(relation_ok))
    result["priority_relation_n"] = len(relation_ok)
    critical = [(g, p) for g, p in zip(event_gold, event_pred) if g == "INTERRUPT"]
    non_critical = [(g, p) for g, p in zip(event_gold, event_pred) if g != "INTERRUPT"]
    result["critical_event_recall"] = _safe_div(sum(p == "INTERRUPT" for _, p in critical), len(critical))
    result["fir"] = _safe_div(sum(p == "INTERRUPT" for _, p in non_critical), len(non_critical))
    if result["follow_up_api_ok_n"] != len(follow_needed):
        result["schedule_exact_match"] = None
    else:
        follow_ok_by_case: dict[str, bool] = defaultdict(lambda: True)
        for (cid, _eid, gold), row in zip(follow_needed, follow_rows):
            follow_ok_by_case[cid] &= _label(row, FOLLOW_UPS) == gold
        result["schedule_exact_match"] = _safe_div(
            sum(decisions_ok and order_ok and follow_ok_by_case[case["case_id"]]
                for case, decisions_ok, order_ok in zip(cases, complete, schedule_ok)), len(cases)
        )
    return result
