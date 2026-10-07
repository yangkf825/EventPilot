import asyncio
import copy
import json

from eventarena.online_v2_eval import evaluate_episode, evaluate_task


def episode():
    return {"case_id": "C", "status": "agent_finished", "protocol": "continuous_v2", "exposed": True,
            "task_states": {"A": {"completed": True, "completion_index": 3, "goal_version": 0}},
            "actions": [{"index": 0, "task_id": "A", "action": {"op": "TYPE"}, "error": None,
                         "after": {"url": "https://example.test/a", "text": "Answer alpha", "candidates": []}}],
            "observations": [{"index": 0, "task_id": "A", "url": "https://example.test/a", "text": "Answer alpha", "candidates": []}],
            "events": {"E1": {"delivered": True, "delivery_index": 1, "started_index": None, "completion_index": None}},
            "task_outputs": {"A": [{"text": "alpha", "goal_version": 0, "sources": [{"url": "https://example.test/a", "quote": "alpha"}]}]}}


def case():
    return {"case_id": "C", "goal": "Find alpha", "event": {"id": "E1", "text": "Notice", "execution": {"kind": "none"}},
            "gold": {"decision": "IGNORE"}, "verification": {"CURRENT_TASK": {"method": "hybrid", "requirements": ["Find alpha"]}}}


def test_unconfigured_judge_does_not_fabricate_success():
    e = episode()
    result = asyncio.run(evaluate_episode(case(), e))
    assert result["episode_success"] is None
    assert result["evaluation_coverage"] is False


def test_claim_and_ungrounded_answer_cannot_be_success():
    e = episode(); e["task_outputs"] = {}
    assert asyncio.run(evaluate_episode(case(), e))["episode_success"] is False
    e = episode(); e["task_outputs"]["A"][0]["sources"][0]["quote"] = "invented"
    result = asyncio.run(evaluate_task("Find alpha", {"method": "deterministic", "checks": [{"type": "saved_output_contains", "value": "alpha"}]}, e))
    assert result["success"] is False


def test_grounded_independent_judge_and_unknown_transport():
    def judge(messages):
        payload = json.loads(messages[-1]["content"])
        return {"success": True, "reason": "Actual source supports answer", "evidence": [
            {"observation_index": 0, "url": payload["actual_observations"][0]["url"], "quote": "alpha"}]}
    e = episode()
    assert asyncio.run(evaluate_episode(case(), e, judge))["episode_success"] is True
    e["status"] = "provider_error"
    result = asyncio.run(evaluate_episode(case(), e, judge))
    assert result["episode_success"] is None and result["failure_category"] == "provider_error"


def test_wrong_classification_does_not_override_actual_behavior():
    c = case(); c["verification"]["CURRENT_TASK"] = {"method": "deterministic", "checks": [{"type": "saved_output_contains", "value": "alpha"}]}
    e = episode(); e["decisions"] = {"E1": "INTERRUPT"}
    assert asyncio.run(evaluate_episode(c, e))["episode_success"] is True


def test_termination_action_denominator_is_actions_not_percentage():
    c = case(); c["gold"] = {"decision": "INTERRUPT", "follow_up": "TERMINATE"}; c["event"]["execution"] = {"kind": "cancel"}
    e = episode(); e["task_states"]["A"].update(stopped=True, stop_index=2)
    e["events"]["E1"].update(started_index=2, completion_index=2, effect_applied=True)
    for i in (3, 4):
        e["actions"].append({"index": i, "task_id": "A", "action": {"op": "CLICK"}, "error": None})
    result = asyncio.run(evaluate_episode(c, e))
    assert result["post_cancel_action_count"] == 2
    assert result["post_cancel_violation"] is True
    assert result["episode_success"] is False


def test_replan_constraints_are_independent_and_can_be_partly_satisfied():
    c = case(); c['gold'] = {'decision':'INTERRUPT','follow_up':'REPLAN'}
    verifier = {'method':'deterministic','checks':[{'type':'saved_output_contains','value':'alpha'}],
        'effective_constraints':[
            {'id':'new','origin':'new','requirement':'Find alpha','verification':{'method':'deterministic','checks':[{'type':'saved_output_contains','value':'alpha'}]}},
            {'id':'old','origin':'retained','requirement':'Also preserve beta','verification':{'method':'deterministic','checks':[{'type':'saved_output_contains','value':'beta'}]}}]}
    c['event']['execution'] = {'kind':'update_goal','updated_goal':'Find alpha','updated_verification':verifier}
    e = episode(); e['task_states']['A']['goal_version'] = 1
    e['task_outputs']['A'][0]['goal_version'] = 1
    e['events']['E1'].update(effect_applied=True,started_index=1,accepted_index=1,completion_index=1)
    result = asyncio.run(evaluate_episode(c,e))
    assert result['updated_goal_success'] is True
    assert [r['success'] for r in result['effective_constraint_results']] == [True,False]
    assert result['effective_constraints'] is False
    assert result['episode_success'] is False
