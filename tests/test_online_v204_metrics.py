"""v2.0.4 regressions for experiment-aware repeats and visible-stage plans."""
import copy

import pytest

from eventarena.online_v2_metrics import score_cases
from test_online_v2_metrics import case, episode, multi_case, multi_episode


def cf_cases(groups=1):
    result = []
    for kind, size in (("goal", 2), ("state", 2), ("semantic", 3)):
        for group in range(groups):
            for index in range(size):
                branch = ("HANDLE", "DEFER", "IGNORE")[index]
                row = case(f"{kind}_{group}_{index}", branch)
                row.update(cf_type=kind, cf_group_id=f"{kind}_{group}")
                result.append(row)
    return result


def cf_episodes(cases):
    result = []
    for repeat in range(3):
        for row in cases:
            record = episode(row, repeat=repeat)
            record.update(experiment="counterfactual", view="trajectory", counterfactual_protocol_valid=True)
            result.append(record)
    return result


def staged_episode():
    c = multi_case()
    e = multi_episode(c)
    e.update(experiment="multi", repeat=0, status="output_truncated")
    e["decisions"] = {"E1": "INTERRUPT", "E2": "INTERRUPT"}
    e["follow_ups"] = {"E1": "HANDLE", "E2": "HANDLE"}
    e["predicted_schedule"] = ["E2", "E1", "CURRENT_TASK"]
    for eid in ("E3", "E4"):
        e["events"][eid]["delivered"] = False
    e["decision_stages"] = [{"new_event_ids": ["E1", "E2"], "known_event_ids": ["E1", "E2"],
                             "action_index": 2, "api_ok": True,
                             "parsed": {"decisions": copy.deepcopy(e["decisions"]),
                                        "follow_ups": copy.deepcopy(e["follow_ups"]),
                                        "schedule": copy.deepcopy(e["predicted_schedule"])}}]
    e["evaluation"].update(episode_success=False, branch_success=False,
                           actual_schedule=[], order_correct=False, schedule_exact_match=False)
    return c, e


def test_real_counterfactual_experiment_records_retain_all_210_repeats_and_subtypes():
    cases = cf_cases(groups=10)
    episodes = cf_episodes(cases)
    original = copy.deepcopy(episodes)
    metrics = score_cases(cases, episodes, experiment="counterfactual")
    assert metrics["registered_episodes"] == metrics["recorded_episodes"] == 210
    assert metrics["MicroESR"] == 1
    assert metrics["denominators"]["MicroESR"]["N"] == 210
    assert metrics["denominators"]["Coverage"]["N"] == 210
    assert metrics["failure_ledger"]["N"] == 210
    assert metrics["failure_ledger"]["counts"]["missing_episode"] == 0
    assert {row["repeat"] for row in metrics["failure_ledger"]["rows"]} == {0, 1, 2}
    for kind, total in (("goal", 60), ("state", 60), ("semantic", 90)):
        subtype = metrics["counterfactual"]["types"][kind]
        assert subtype["common_denominators"]["MicroESR"]["N"] == total
        assert subtype["common_metrics"]["MicroESR"] == 1
        assert subtype["group_counts"]["N"] == 30
    assert episodes == original


def test_counterfactual_missing_whole_repeat_is_not_collapsed_to_default_repeat():
    cases = cf_cases()
    episodes = cf_episodes(cases)
    for row in episodes:
        if row["repeat"] == 2:
            row.clear()
    episodes = [row for row in episodes if row] + [
        {"case_id": c["case_id"], "experiment": "counterfactual", "repeat": 2,
         "status": "registered_pending", "view": "trajectory"} for c in cases]
    metrics = score_cases(cases, episodes, experiment="counterfactual")
    assert metrics["registered_episodes"] == metrics["denominators"]["MicroESR"]["N"] == 21
    assert metrics["recorded_episodes"] == 14
    assert metrics["MicroESR"] is None
    assert metrics["denominators"]["MicroESR"]["unknown"] == 7
    assert metrics["failure_ledger"]["counts"]["missing_episode"] == 7


@pytest.mark.parametrize("invalid", [False, None])
def test_counterfactual_invariant_audit_remains_required_and_unknown_is_not_zero(invalid):
    cases = cf_cases()
    episodes = cf_episodes(cases)
    episodes[0]["counterfactual_protocol_valid"] = invalid
    episodes[1]["decisions"]["E1"] = "INTERRUPT"
    metrics = score_cases(cases, episodes, experiment="counterfactual")
    stats = metrics["denominators"]["MicroESR"]
    assert stats["N"] == 21 and stats["unknown"] == 2 and stats["failures"] == 0
    assert metrics["MicroESR"] is None and metrics["CFA"] is None
    assert metrics["episode_success_rate"] is metrics["event_handling_success"] is None
    assert metrics["follow_up_accuracy"] == metrics["FollowupAcc"]
    assert metrics["failure_ledger"]["counts"]["environment_error"] == 2
    assert metrics["failure_ledger"]["counts"]["missing_episode"] == 0
    assert metrics["raw_failure_ledger"]["counts"]["success"] == 21


def test_future_unseen_events_do_not_make_current_stage_plan_wrong():
    c, e = staged_episode()
    original = copy.deepcopy(e)
    metrics = score_cases([c], [e], experiment="multi")
    assert metrics["PriorityAcc"] == metrics["ScheduleExactMatch"] == 1
    assert metrics["PriorityCaseAcc"] == metrics["ScheduleCaseExactMatch"] == 1
    assert metrics["denominators"]["PriorityAcc"]["N"] == 2
    assert metrics["denominators"]["ScheduleExactMatch"]["N"] == 1
    assert metrics["CompleteCaseAcc"] is None
    assert metrics["denominators"]["CompleteCaseAcc"]["N"] == 1
    assert metrics["denominators"]["CompleteCaseAcc"]["unknown"] == 1
    assert metrics["ActualPriorityAcc"] == metrics["ActualScheduleExactMatch"] == metrics["multi_ESR"] == 0
    stage = metrics["multi_plan_scoring"]["stages"][0]
    assert stage["known_event_ids"] == ["E1", "E2"]
    assert set(stage["required_nodes"]) == {"E1", "E2", "CURRENT_TASK"}
    assert e == original


def test_every_stage_is_scored_and_later_good_plan_does_not_erase_bad_early_plan():
    c, e = staged_episode()
    first = e["decision_stages"][0]
    first["parsed"]["schedule"] = ["E1", "CURRENT_TASK", "E2"]
    second_schedule = ["E2", "E1", "CURRENT_TASK", "E3"]
    e["decision_stages"].append({"known_event_ids": ["E1", "E2", "E3", "E4"],
                                  "new_event_ids": ["E3", "E4"], "action_index": 8,
                                  "api_ok": True, "parsed": {"schedule": second_schedule}})
    e["predicted_schedule"] = second_schedule
    metrics = score_cases([c], [e], experiment="multi")
    assert metrics["PriorityAcc"] == pytest.approx(4 / 5)
    assert metrics["denominators"]["PriorityAcc"]["N"] == 5
    assert metrics["ScheduleExactMatch"] == .5
    assert metrics["denominators"]["ScheduleExactMatch"]["N"] == 2
    assert metrics["PriorityCaseAcc"] == metrics["ScheduleCaseExactMatch"] == 0
    assert metrics["multi_plan_scoring"]["observed_stages"] == 2


def test_future_gold_change_cannot_change_early_stage_plan_score():
    c, e = staged_episode()
    before = score_cases([c], [e], experiment="multi")
    c["gold"]["decisions"]["E3"] = {"decision": "INTERRUPT", "follow_up": "TERMINATE"}
    c["gold"]["required_nodes"] = ["E1", "E2", "TERMINATE_TASK", "E3"]
    c["gold"]["precedence"] = [["E1", "TERMINATE_TASK"], ["E2", "TERMINATE_TASK"], ["E3", "TERMINATE_TASK"]]
    after = score_cases([c], [e], experiment="multi")
    assert before["PriorityAcc"] == after["PriorityAcc"] == 1
    assert before["ScheduleExactMatch"] == after["ScheduleExactMatch"] == 1
    assert "TERMINATE_TASK" not in after["multi_plan_scoring"]["stages"][0]["required_nodes"]


def test_future_node_in_report_is_rejected_instead_of_rewarding_hidden_guess():
    c, e = staged_episode()
    e["decision_stages"][0]["parsed"]["schedule"].append("E3")
    metrics = score_cases([c], [e], experiment="multi")
    assert metrics["ScheduleExactMatch"] == metrics["PriorityAcc"] == 0


def test_provider_failed_stage_is_unknown_but_invalid_model_report_is_failure():
    c, e = staged_episode()
    e["decision_stages"][0].update(parsed=None, api_ok=False)
    metrics = score_cases([c], [e], experiment="multi")
    assert metrics["PriorityAcc"] is metrics["ScheduleExactMatch"] is None
    assert metrics["denominators"]["PriorityAcc"]["unknown"] == 2
    assert metrics["denominators"]["ScheduleExactMatch"]["unknown"] == 1
    e["decision_stages"][0]["api_ok"] = True
    metrics = score_cases([c], [e], experiment="multi")
    assert metrics["PriorityAcc"] == metrics["ScheduleExactMatch"] == 0


def test_critical_recall_keeps_full_registered_denominator_and_unseen_unknown():
    c, e = staged_episode()
    c["gold"]["critical_events"] = ["E1", "E3"]
    c["gold"]["decisions"]["E3"] = {"decision": "INTERRUPT", "follow_up": "HANDLE"}
    metrics = score_cases([c], [e], experiment="multi")
    stats = metrics["denominators"]["CriticalEventRecall"]
    assert stats["N"] == 2 and stats["known"] == 1 and stats["unknown"] == 1
    assert metrics["CriticalEventRecall"] is None
    assert stats["covered_rate"] == 1


def test_legacy_plan_uses_only_delivered_scope_and_is_explicitly_labelled():
    c, e = staged_episode()
    e.pop("decision_stages")
    metrics = score_cases([c], [e], experiment="multi")
    assert metrics["PriorityAcc"] == metrics["ScheduleExactMatch"] == 1
    assert metrics["multi_plan_scoring"]["legacy_last_visible_plan_count"] == 1
    assert metrics["multi_plan_scoring"]["stages"][0]["source"] == "legacy_last_visible_plan"


def test_missing_episode_plan_is_unknown_and_invalid_scope_is_rejected():
    c, e = staged_episode()
    metrics = score_cases([c], [], experiment="multi")
    assert metrics["PriorityAcc"] is metrics["ScheduleExactMatch"] is None
    assert metrics["denominators"]["ScheduleExactMatch"]["unknown"] == 1
    assert metrics["multi_plan_scoring"]["unavailable_scope_count"] == 1
    e["decision_stages"][0]["known_event_ids"] = ["E1", "INVENTED"]
    with pytest.raises(ValueError, match="unique registered IDs"):
        score_cases([c], [e], experiment="multi")


@pytest.mark.parametrize("status", ["checkpoint_capture_unavailable", "checkpoint_projection_unavailable"])
def test_unavailable_checkpoint_material_is_environment_unknown(status):
    c = case("C", "HANDLE")
    e = episode(c, success=False)
    e.update(status=status, api_ok=False, decisions={})
    e["evaluation"]["status"] = status
    metrics = score_cases([c], [e])
    assert metrics["MicroESR"] is None
    assert metrics["failure_ledger"]["counts"]["environment_error"] == 1


def test_unavailable_scope_does_not_publish_covered_edge_mean_as_full_priority_rate():
    c, e = staged_episode()
    missing = copy.deepcopy(c)
    missing["case_id"] = "MISSING"
    metrics = score_cases([c, missing], [e], experiment="multi")
    stats = metrics["denominators"]["PriorityAcc"]
    assert metrics["PriorityAcc"] is metrics["priority_accuracy"] is None
    assert stats["N"] == 2 and stats["covered_rate"] == 1
    assert stats["unavailable_scope_count"] == 1 and stats["scope_coverage"] == .5
    assert stats["lower_bound"] is stats["upper_bound"] is None
    assert metrics["denominators"]["ScheduleExactMatch"]["N"] == 2
    assert metrics["denominators"]["ScheduleExactMatch"]["unknown"] == 1


def test_partial_visible_decision_error_still_establishes_complete_case_failure():
    c, e = staged_episode()
    e["decisions"]["E1"] = "IGNORE"
    metrics = score_cases([c], [e], experiment="multi")
    assert metrics["CompleteCaseAcc"] == 0
    assert metrics["denominators"]["CompleteCaseAcc"]["failures"] == 1


def test_stage_without_prediction_or_provider_outcome_is_unknown():
    c, e = staged_episode()
    e["decision_stages"][0].update(parsed=None, api_ok=None)
    metrics = score_cases([c], [e], experiment="multi")
    assert metrics["PriorityAcc"] is metrics["ScheduleExactMatch"] is None
    assert metrics["denominators"]["PriorityAcc"]["unknown"] == 2
    assert metrics["denominators"]["ScheduleExactMatch"]["unknown"] == 1
