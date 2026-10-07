"""Checks for denominators, execution grounding and paired protocol validity."""
import copy

import pytest

from eventarena.online_v2_metrics import (
    cluster_bootstrap_ci, fixed_macro_f1, is_legal_schedule, score_cases,
    summarize_tristate, tri_and,
)


def case(cid="C", branch="IGNORE", family="F"):
    decision = branch if branch in {"IGNORE", "DEFER"} else "INTERRUPT"
    return {"case_id": cid, "task_family_id": family,
            "event": {"id": "E1", "execution": {"kind": "research_task"}},
            "gold": {"decision": decision, "follow_up": branch if decision == "INTERRUPT" else None}}


def episode(c, decision=None, follow_up=None, success=True, repeat=0):
    label = c["gold"]
    return {"case_id": c["case_id"], "repeat": repeat, "status": "completed", "api_ok": True,
            "decisions": {"E1": decision or label["decision"]},
            "follow_ups": {"E1": follow_up or label["follow_up"]},
            "events": {"E1": {"delivered": True}},
            "evaluation": {"status": "passed" if success is True else "failed" if success is False else "unverified",
                "episode_success": success, "branch_success": success, "task_success": success,
                "updated_goal_success": success, "environment_valid": True, "exposed": True,
                "distraction": False, "resumed": True, "resume_completion": success,
                "order_correct": True, "replan_adopted": True,
                "effective_constraints": success, "effective_constraint_results": [success],
                "stopped": True, "termination_compliant": True,
                "post_cancel_violation": False, "post_cancel_action_count": 0,
                "task_completion_results": {"CURRENT_TASK": success, "E1": False},
                "event_results": {"E1": {"success": success, "delivered": True, "start_index": 3, "timely": True}}}}


def multi_case():
    return {"case_id": "M", "events": [{"id": eid} for eid in ("E1", "E2", "E3", "E4")],
            "gold": {"decisions": {
                "E1": {"decision": "INTERRUPT", "follow_up": "HANDLE"},
                "E2": {"decision": "INTERRUPT", "follow_up": "HANDLE"},
                "E3": {"decision": "DEFER", "follow_up": None},
                "E4": {"decision": "IGNORE", "follow_up": None}},
                "required_nodes": ["E1", "E2", "CURRENT_TASK", "E3"],
                "precedence": [["E1", "CURRENT_TASK"], ["E2", "CURRENT_TASK"], ["CURRENT_TASK", "E3"]],
                "critical_events": ["E1", "E2"]}}


def multi_episode(c):
    return {"case_id": c["case_id"], "api_ok": True,
            "decisions": {eid: label["decision"] for eid, label in c["gold"]["decisions"].items()},
            "follow_ups": {eid: label["follow_up"] for eid, label in c["gold"]["decisions"].items()},
            "predicted_schedule": ["E2", "E1", "CURRENT_TASK", "E3"],
            "events": {eid: {"delivered": True} for eid in c["gold"]["decisions"]},
            "evaluation": {"episode_success": True, "branch_success": True, "task_success": True,
                "environment_valid": True, "exposed": True,
                "actual_schedule": ["E1", "E2", "CURRENT_TASK", "E3"], "order_correct": True,
                "schedule_exact_match": True,
                "event_results": {eid: {"success": True, "timely": True} for eid in ("E1", "E2", "E3")}}}


def test_tristate_never_converts_unknown_or_integer_to_failure():
    stats = summarize_tristate([True, False, None, 1])
    assert stats == {"rate": None, "n": 1, "N": 4, "successes": 1, "failures": 1,
                     "known": 2, "unknown": 2, "coverage": .5,
                     "lower_bound": .25, "upper_bound": .75, "covered_rate": .5}
    assert tri_and([False, None]) is False
    assert tri_and([True, None]) is None
    assert summarize_tristate([])["rate"] is None


def test_fixed_f1_absent_classes_still_contribute_zero():
    score, details = fixed_macro_f1(["INTERRUPT"], ["INTERRUPT"])
    assert score == pytest.approx(1 / 3)
    assert details["IGNORE"]["support"] == 0
    score, _ = fixed_macro_f1(["INTERRUPT"], ["INVALID"])
    assert score == 0


def test_conditional_followup_and_joint_use_different_denominators():
    cases = [case("H", "HANDLE"), case("R", "REPLAN"), case("T", "TERMINATE")]
    episodes = [episode(cases[0]), episode(cases[1], decision="DEFER"), episode(cases[2], follow_up="HANDLE")]
    metrics = score_cases(cases, episodes)
    assert metrics["accuracy"] == pytest.approx(2 / 3)
    assert metrics["FollowupAcc"] == .5
    assert metrics["JointFUAcc"] == pytest.approx(1 / 3)
    assert metrics["denominators"]["FollowupAcc"]["N"] == 2
    assert metrics["denominators"]["JointFUAcc"]["N"] == 3
    assert metrics["follow_up_classification"]["classes"]["REPLAN"]["support"] == 0


def test_fir_mir_registered_gold_denominators():
    cases = [case("I"), case("D", "DEFER"), case("H", "HANDLE")]
    metrics = score_cases(cases, [episode(cases[0], decision="INTERRUPT"), episode(cases[1]), episode(cases[2], decision="INVALID")])
    assert metrics["FIR"] == .5
    assert metrics["MIR"] == 1
    assert metrics["decision_valid_rate"] == pytest.approx(2 / 3)


def test_provider_unknown_is_distinct_from_invalid_model_output():
    c = case()
    missing = {"case_id": "C", "api_ok": False, "status": "provider_error", "evaluation": {"episode_success": None}}
    metrics = score_cases([c], [missing])
    assert metrics["accuracy"] is None
    assert metrics["macro_f1"] is None
    assert metrics["failure_ledger"]["counts"]["provider_error"] == 1
    invalid = episode(c, decision="INVALID", success=False)
    metrics = score_cases([c], [invalid])
    assert metrics["accuracy"] == 0
    assert metrics["failure_ledger"]["counts"]["decision_error"] == 1


def test_pending_preserves_repeats_but_does_not_inflate_recorded_count():
    c = case()
    pending = {"case_id": "C", "repeat": 1, "status": "registered_pending", "api_ok": False}
    metrics = score_cases([c], [episode(c), pending])
    assert metrics["registered_episodes"] == 2
    assert metrics["recorded_episodes"] == 1
    assert metrics["MicroESR"] is None
    assert metrics["denominators"]["MicroESR"]["lower_bound"] == .5
    assert metrics["denominators"]["MicroESR"]["upper_bound"] == 1
    assert metrics["failure_ledger"]["counts"]["missing_episode"] == 1


def test_undelivered_event_is_not_a_private_label_error():
    c = case("C", "HANDLE")
    e = episode(c, decision="IGNORE", success=False)
    e["events"]["E1"]["delivered"] = False
    e["evaluation"]["exposed"] = False
    metrics = score_cases([c], [e])
    assert metrics["accuracy"] is None
    assert metrics["MIR"] is None
    assert metrics["MicroESR"] == 0
    assert metrics["Exposure"] == 0
    assert metrics["failure_ledger"]["counts"]["execution_failure"] == 1


def test_macro_esr_equal_five_branches_and_task_completion_only_original():
    branches = ["IGNORE", "DEFER", "HANDLE", "REPLAN", "TERMINATE"]
    cases = [case(branch, branch) for branch in branches]
    episodes = [episode(c) for c in cases]
    metrics = score_cases(cases, episodes)
    assert metrics["MacroESR"] == metrics["MicroESR"] == 1
    assert metrics["TaskCompletion"] == 1
    assert metrics["denominators"]["TaskCompletion"]["N"] == 4
    assert metrics["denominators"]["EHS"]["N"] == 4
    assert sum(metrics["failure_ledger"]["counts"].values()) == 5
    assert score_cases(cases[:1], episodes[:1])["MacroESR"] is None


def test_execution_unknown_keeps_bounds_and_failure_ledger_exclusive():
    cases = [case("A"), case("B")]
    es = [episode(cases[0]), episode(cases[1], success=None)]
    es[1]["evaluation"]["status"] = "website_unavailable"
    metrics = score_cases(cases, es)
    assert metrics["accuracy"] == 1
    assert metrics["MicroESR"] is None
    assert metrics["Coverage"] == .5
    assert metrics["denominators"]["MicroESR"]["lower_bound"] == .5
    assert metrics["denominators"]["MicroESR"]["upper_bound"] == 1
    assert metrics["failure_ledger"]["counts"]["environment_error"] == 1


def test_diagnostic_conditional_support_and_case_equal_constraint_fraction():
    cs = [case("D1", "DEFER"), case("D2", "DEFER"), case("H", "HANDLE"), case("R1", "REPLAN"), case("R2", "REPLAN")]
    es = [episode(c) for c in cs]
    es[1]["evaluation"]["task_success"] = False
    es[1]["evaluation"]["event_results"]["E1"]["start_index"] = None
    es[3]["evaluation"]["effective_constraint_results"] = [True, False, {"success": False, "removed": True}]
    es[4]["evaluation"]["effective_constraint_results"] = [True] * 10
    metrics = score_cases(cs, es)
    assert metrics["OTS"] == .5
    assert metrics["DER"] == 1 and metrics["DES"] == 1
    assert metrics["denominators"]["DER"]["N"] == 1
    assert metrics["denominators"]["DES"]["N"] == 1
    assert metrics["RR"] == metrics["RCR"] == 1
    assert metrics["ECS"] == .75


def test_cancellation_counts_can_exceed_one_and_unknown_is_not_zero():
    cs = [case("T1", "TERMINATE"), case("T2", "TERMINATE")]
    es = [episode(c) for c in cs]
    es[0]["evaluation"]["post_cancel_action_count"] = 3
    es[0]["evaluation"]["post_cancel_violation"] = True
    metrics = score_cases(cs, es)
    assert metrics["mean_post_cancel_action_count"] == 1.5
    assert metrics["post_cancel_violation_rate"] == .5
    es[1]["evaluation"]["post_cancel_action_count"] = None
    assert score_cases(cs, es)["mean_post_cancel_action_count"] is None


def test_counterfactual_each_type_and_pair_all_correct_not_case_average():
    cs = []
    for kind, size in (("goal", 2), ("state", 2), ("semantic", 3)):
        for index in range(size):
            c = case(f"{kind}{index}", "HANDLE" if index == 0 else "IGNORE")
            c.update(cf_type=kind, cf_group_id=kind)
            cs.append(c)
    es = [episode(c) for c in cs]
    for e in es:
        e["counterfactual_protocol_valid"] = True
    es[1]["decisions"]["E1"] = "INTERRUPT"
    metrics = score_cases(cs, es, experiment="counterfactual")
    assert metrics["OverallAcc"] == pytest.approx(6 / 7)
    assert metrics["CFA"] == 0
    assert metrics["SFA"] == metrics["SemanticRobustAccuracy"] == 1
    assert metrics["counterfactual"]["types"]["goal"]["case_accuracy"] == .5
    assert metrics["counterfactual"]["types"]["goal"]["group_counts"]["N"] == 1


def test_counterfactual_invalid_group_is_unknown_even_with_wrong_prediction():
    cs = [case("G1"), case("G2")]
    for c in cs:
        c.update(cf_type="goal", cf_group_id="G")
    es = [episode(c) for c in cs]
    es[0]["counterfactual_protocol_valid"] = False
    es[1]["counterfactual_protocol_valid"] = True
    es[1]["decisions"]["E1"] = "INTERRUPT"
    metrics = score_cases(cs, es, experiment="counterfactual")
    assert metrics["CFA"] is None
    assert metrics["OverallAcc"] is None
    assert metrics["accuracy"] is None
    assert metrics["raw_decision_accuracy"] == .5
    assert metrics["counterfactual"]["protocol_validity"]["failures"] == 1


def test_multi_accepts_alternative_topological_schedule_and_uses_actual_facts():
    c = multi_case()
    e = multi_episode(c)
    metrics = score_cases([c], [e], experiment="multi")
    assert metrics["EventF1"] == metrics["CompleteCaseAcc"] == 1
    assert metrics["PriorityAcc"] == metrics["ScheduleExactMatch"] == 1
    assert metrics["ActualPriorityAcc"] == metrics["ActualScheduleExactMatch"] == 1
    assert metrics["CriticalEventRecall"] == metrics["multi_ESR"] == 1
    assert is_legal_schedule(c, ["E1", "E2", "CURRENT_TASK", "E3"])
    assert is_legal_schedule(c, ["E2", "E1", "CURRENT_TASK", "E3"])
    assert not is_legal_schedule(c, ["E2", "E1", "CURRENT_TASK", "E3", "E4"])
    e["evaluation"]["actual_schedule"] = None
    e["evaluation"]["order_correct"] = None
    e["evaluation"]["schedule_exact_match"] = None
    assert score_cases([c], [e], experiment="multi")["ActualScheduleExactMatch"] is None


def test_multi_priority_checks_all_edges_missing_nodes_and_duplicates():
    c = multi_case()
    e = multi_episode(c)
    e["predicted_schedule"] = ["E1", "CURRENT_TASK", "E2", "E3"]
    metrics = score_cases([c], [e], experiment="multi")
    assert metrics["PriorityAcc"] == pytest.approx(2 / 3)
    assert metrics["PriorityCaseAcc"] == 0
    assert metrics["ScheduleExactMatch"] == 0
    c["gold"]["precedence"].append(["CURRENT_TASK", "E1"])
    with pytest.raises(ValueError, match="cycle"):
        is_legal_schedule(c, e["predicted_schedule"])


def test_success_funnel_is_nested_and_reports_conditional_n_over_N():
    cs = [case("A", "HANDLE"), case("B", "HANDLE")]
    es = [episode(cs[0]), episode(cs[1], follow_up="REPLAN", success=False)]
    metrics = score_cases(cs, es)
    stages = metrics["success_funnel"]["stages"]
    prior = {(row["case_id"], row["repeat"]) for row in stages[0]["passing_ids"]}
    for stage in stages[1:]:
        current = {(row["case_id"], row["repeat"]) for row in stage["passing_ids"]}
        assert current <= prior
        prior = current
    followup = next(stage for stage in stages if stage["stage"] == "follow_up_correct")
    assert followup["conditional_n"] == 1 and followup["conditional_N"] == 2
    assert followup["n"] == 1 and followup["N"] == 2


def test_duplicate_case_repeat_is_rejected_and_no_event_baseline_is_supported():
    c = case()
    e = episode(c)
    with pytest.raises(ValueError, match="Duplicate episode"):
        score_cases([c], [e, copy.deepcopy(e)])
    baseline = {"case_id": "B"}
    result = score_cases([baseline], [{"case_id": "B", "evaluation": {"episode_success": True, "task_success": True}}], experiment="baseline")
    assert result["TaskCompletion"] == result["MicroESR"] == 1
    assert result["accuracy"] is None


def test_bootstrap_retains_clusters_is_reproducible_and_rejects_unknown_ci():
    rows = [{"task_family_id": family, "value": value} for family, value in (("F1", True), ("F1", True), ("F2", False), ("F3", True))]
    first = cluster_bootstrap_ci(rows, n_resamples=100)
    assert first == cluster_bootstrap_ci(rows, n_resamples=100)
    assert first["estimate"] == .75 and first["clusters"] == 3
    assert first["lower"] <= first["estimate"] <= first["upper"]
    rows[0]["value"] = None
    assert cluster_bootstrap_ci(rows)["lower"] is None
    assert cluster_bootstrap_ci(rows)["status"] == "unknown_outcomes"
