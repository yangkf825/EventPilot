import asyncio
import copy
import json

from eventarena.online_v2_eval import evaluate_episode


def verifier(value):
    return {"method": "deterministic", "checks": [{"type": "saved_output_contains", "value": value}]}


def revision(eid, value):
    return {"id": eid, "text": "Revise the active report", "execution": {
        "kind": "update_goal", "updated_goal": "Find " + value,
        "updated_verification": verifier(value)},
        "gold": {"decision": "INTERRUPT", "follow_up": "REPLAN"}}


def termination(eid):
    return {"id": eid, "text": "Withdraw the active report", "execution": {"kind": "cancel"},
            "gold": {"decision": "INTERRUPT", "follow_up": "TERMINATE"}}


def research(eid):
    return {"id": eid, "text": "Research the required reference", "execution": {
        "kind": "research_task", "goal": "Find beta", "verification": verifier("beta")},
        "gold": {"decision": "INTERRUPT", "follow_up": "HANDLE"}}


def case(events):
    return {"case_id": "runtime_versions", "goal": "Find alpha", "events": events,
            "gold": {"decisions": {event["id"]: event["gold"] for event in events}},
            "verification": {"CURRENT_TASK": verifier("alpha"),
                             "CURRENT_TASK_UPDATED": verifier("unseen target")}}


def episode(events, value="alpha", version=0, status="agent_finished"):
    page = {"url": "https://example.test/evidence", "text": "alpha beta gamma", "candidates": []}
    return {"status": status, "protocol": "continuous_v2", "exposed": True,
            "task_states": {"A": {"completed": True, "completion_index": 10, "goal_version": version}},
            "observations": [page], "actions": [],
            "events": {event["id"]: {"delivered": False, "delivery_index": None,
                                      "started_index": None, "completion_index": None}
                       for event in events},
            "task_outputs": {"A": [{"text": value, "goal_version": version,
                "sources": [{"url": page["url"], "quote": value}]}]}}


def evaluate(c, e):
    return asyncio.run(evaluate_episode(c, e))


def apply_revision(e, eid, index):
    e["events"][eid].update(delivered=True, delivery_index=index,
                            started_index=index, accepted_index=index,
                            completion_index=index, effect_applied=True)


def test_unseen_revision_does_not_change_current_content_or_constraint_metrics():
    events = [revision("future", "beta")]
    e = episode(events, status="provider_error")
    result = evaluate(case(events), e)
    phase = result["phase_results"]["CURRENT_TASK"]
    assert phase["effective_goal"] == "Find alpha" and phase["success"] is True
    assert result["updated_goal_success"] is None
    assert result["effective_constraints"] is None and result["effective_constraint_results"] == []
    assert result["replan_adopted"] is None
    assert result["event_results"]["future"]["success"] is None
    assert result["event_results"]["future"]["timely"] is None
    assert result["episode_success"] is None and result["schedule_exact_match"] is None


def test_seen_unapplied_revision_is_a_separate_compliance_failure():
    events = [revision("seen", "beta")]
    e = episode(events)
    e["events"]["seen"].update(delivered=True, delivery_index=1)
    result = evaluate(case(events), e)
    assert result["phase_results"]["CURRENT_TASK"]["effective_goal"] == "Find alpha"
    assert result["task_success"] is True
    assert result["updated_goal_success"] is None and result["replan_adopted"] is False
    assert result["event_results"]["seen"]["success"] is False
    assert result["episode_success"] is False


def test_last_applied_revision_uses_its_verifier_instead_of_future_case_verifier():
    events = [revision("applied", "gamma"), revision("future", "beta")]
    e = episode(events, "gamma", 1, "provider_error")
    apply_revision(e, "applied", 1)
    result = evaluate(case(events), e)
    assert result["phase_results"]["CURRENT_TASK"]["effective_goal"] == "Find gamma"
    assert result["task_success"] is True and result["updated_goal_success"] is True
    assert result["event_results"]["future"]["success"] is None
    assert result["episode_success"] is None


def test_applied_revision_order_comes_from_actions_instead_of_dataset_array_order():
    events = [revision("late", "gamma"), revision("early", "beta")]
    e = episode(events, "gamma", 2)
    apply_revision(e, "early", 1)
    apply_revision(e, "late", 2)
    result = evaluate(case(events), e)
    assert result["phase_results"]["CURRENT_TASK"]["effective_goal"] == "Find gamma"
    assert result["episode_success"] is True


def test_unseen_termination_keeps_current_phase_and_final_episode_obligation():
    events = [termination("future")]
    result = evaluate(case(events), episode(events))
    assert result["phase_results"]["CURRENT_TASK"]["required"] is True
    assert result["task_completion_results"] == {"CURRENT_TASK": True}
    assert result["cancelled"] is False and result["termination_compliant"] is None
    assert "TERMINATE_TASK" in result["required_nodes"]
    assert result["episode_success"] is False


def test_unseen_termination_does_not_make_completed_prefix_a_known_schedule_violation():
    events = [termination("future")]
    result = evaluate(case(events), episode(events, status="provider_error"))
    assert result["task_success"] is True
    assert result["order_correct"] is None and result["schedule_exact_match"] is None
    assert result["episode_success"] is None


def test_checkpoint_capture_and_projection_failures_keep_missing_phase_unknown():
    for status in ("checkpoint_capture_unavailable", "checkpoint_projection_unavailable"):
        events = [revision("future", "beta")]
        e = episode(events, status=status)
        e["task_outputs"] = {}
        result = evaluate(case(events), e)
        assert result["task_success"] is None
        assert result["event_results"]["future"]["success"] is None
        assert result["episode_success"] is None and result["failure_category"] == status


def test_interrupt_before_saved_output_remains_unknown_at_each_stage():
    events = [termination("seen")]
    e = episode(events, status="browser_error")
    e["task_outputs"] = {}
    e["task_states"]["A"] = {"goal_version": 0}
    e["events"]["seen"].update(delivered=True, delivery_index=1)
    result = evaluate(case(events), e)
    assert result["task_success"] is None
    assert result["phase_results"]["CURRENT_TASK"]["required"] is True
    assert result["event_results"]["seen"]["success"] is None
    assert result["event_results"]["seen"]["timely"] is None
    assert result["termination_compliant"] is None
    assert result["order_correct"] is None and result["schedule_exact_match"] is None
    assert result["episode_success"] is None


def test_partial_handle_does_not_claim_completion_or_recovery_failure():
    events = [research("reference")]
    e = episode(events, status="provider_error")
    e["task_states"]["A"] = {"goal_version": 0}
    e["events"]["reference"].update(delivered=True, delivery_index=1, started_index=1)
    result = evaluate(case(events), e)
    event = result["event_results"]["reference"]
    assert event["success"] is None and event["resumed"] is None
    assert event["timely"] is True
    assert result["resume_completion"] is None and result["episode_success"] is None


def test_actual_timing_violation_survives_infrastructure_unknown():
    events = [research("reference")]
    e = episode(events, status="provider_error")
    e["events"]["reference"].update(delivered=True, delivery_index=1)
    e["actions"] = [{"index": 2, "task_id": "A", "action": {"op": "CLICK"}, "error": None}]
    result = evaluate(case(events), e)
    assert result["event_results"]["reference"]["timely"] is False
    assert result["event_results"]["reference"]["continued_before_handling"] is True
    assert result["episode_success"] is None


def test_observed_ungrounded_saved_result_remains_a_known_failure():
    events = [research("reference")]
    e = episode(events, status="provider_error")
    e["events"]["reference"].update(delivered=True, delivery_index=1, started_index=1)
    e["task_outputs"]["reference"] = copy.deepcopy(e["task_outputs"]["A"])
    e["task_outputs"]["reference"][0]["sources"][0]["quote"] = "fabricated evidence"
    result = evaluate(case(events), e)
    assert result["event_results"]["reference"]["success"] is False
    assert result["episode_success"] is None


def test_separate_packet_note_is_verified_by_binding_without_duplicating_its_answer():
    events = [research("reference")]
    c = case(events)
    c["goal"] = "Find alpha. The packet acceptance checklist also requires the separate beta note."
    c["verification"]["CURRENT_TASK"] = {"method": "hybrid", "goal": "Find alpha",
        "requirements": ["Find alpha"], "checks": [{"type": "required_event_completed", "event_id": "reference"}]}
    e = episode(events)
    e["task_outputs"]["reference"] = copy.deepcopy(e["task_outputs"]["A"])
    e["task_outputs"]["reference"][0].update(text="beta", sources=[{
        "url": e["observations"][0]["url"], "quote": "beta"}])
    e["events"]["reference"].update(delivered=True, delivery_index=1,
        started_index=1, completion_index=1, verified_completion_index=1)
    e["actions"] = [{"index": 0, "task_id": "A", "action": {"op": "GOTO"},
                     "after": copy.deepcopy(e["observations"][0]), "error": None},
                    {"index": 2, "task_id": "reference", "active_task_after": "A",
                     "action": {"op": "SWITCH_TASK", "task_id": "A"}, "error": None}]
    judged_goals = []

    def judge(messages):
        payload = json.loads(messages[-1]["content"])
        judged_goals.append(payload["registered_goal"])
        return {"success": payload["registered_goal"] == "Find alpha", "reason": "Verify the main report content",
                "evidence": [{"observation_index": 0, "url": e["observations"][0]["url"], "quote": "alpha"}]}

    result = asyncio.run(evaluate_episode(c, e, judge))
    assert judged_goals == ["Find alpha"]
    assert result["phase_results"]["CURRENT_TASK"]["required_event_checks"] == [True]
    assert result["episode_success"] is True
    e["task_outputs"].pop("reference")
    assert asyncio.run(evaluate_episode(c, e, judge))["task_success"] is False


def test_applied_revision_retains_independently_registered_acceptance_binding():
    events = [research("reference"), revision("revision", "gamma")]
    c = case(events)
    c["verification"]["CURRENT_TASK"]["checks"].append({
        "type": "required_event_completed", "event_id": "reference"})
    events[1]["execution"]["updated_goal"] = "Find gamma and preserve the separate beta note."
    events[1]["execution"]["updated_verification"]["goal"] = "Find gamma"
    e = episode(events, "gamma", 1)
    apply_revision(e, "revision", 2)
    e["events"]["reference"].update(delivered=True, delivery_index=1, started_index=1, completion_index=1)
    result = evaluate(c, e)
    phase = result["phase_results"]["CURRENT_TASK"]
    assert phase["content_verification_goal"] == "Find gamma"
    assert phase["required_event_checks"] == [False]
    assert result["task_success"] is False and result["updated_goal_success"] is False
