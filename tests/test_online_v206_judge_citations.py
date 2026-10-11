"""Judge format repairs retain strict, observed evidence requirements."""
import asyncio
import copy
import hashlib
import json

import pytest

from eventarena.online_v2_eval import (
    JUDGE_CITATION_POLICY, JUDGE_FORMAT_RETRIES, evaluate_task,
    OUTCOME_EVIDENCE_POLICY, evaluate_episode,
)


URL = "https://weather.example/hourly/90028"
TEXT = "ZIP 90028 hourly forecast\nThursday, October 8\n8 am Sunny 74° 0 in precipitation"


def episode():
    observed = {"index": 83, "task_id": "A", "url": URL, "text": TEXT}
    return {
        "status": "agent_finished", "task_states": {"A": {"goal_version": 0}},
        "observations": [observed],
        "actions": [{"task_id": "A", "action": {"op": "GOTO", "url": URL},
                     "error": None, "after": copy.deepcopy(observed)}],
        "task_outputs": {"A": [{"goal_version": 0,
            "text": "Thursday, October 8, 8 am: Sunny, 74°, 0 in precipitation.",
            "sources": [{"url": URL, "quote": "8 am Sunny 74° 0 in precipitation"}]}]},
    }


def positive(quote="8 am Sunny 74° 0 in precipitation", index=0, url=URL):
    return {"success": True, "reason": "The observed hourly period supports the answer.",
            "evidence": [{"observation_index": index, "url": url, "quote": quote}]}


class Judge:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.messages = []

    def __call__(self, messages):
        self.messages.append(copy.deepcopy(messages))
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def evaluate(judge, current=None):
    return asyncio.run(evaluate_task(
        "Find the dated next hourly forecast for ZIP 90028, temperature and precipitation",
        {"method": "llm_judge"}, current or episode(), judge=judge))


def test_exact_citation_and_uniform_short_quote_policy_need_no_repair():
    judge = Judge([positive()])
    result = evaluate(judge)
    assert result["success"] is True
    assert result["judge_validation"] == {
        "policy": JUDGE_CITATION_POLICY, "max_format_retries": 1,
        "request_count": 1, "attempts": [{"attempt": 1, "validation": "valid"}],
    }
    assert JUDGE_FORMAT_RETRIES == 1
    system = judge.messages[0][0]["content"]
    assert "short, contiguous passage" in system
    assert "Do not summarize" in system


@pytest.mark.parametrize("bad", [
    positive("ZIP 90028 hourly forecast ... 8 am Sunny 74°"),
    positive("ZIP 90028 hourly forecast\\nThursday, October 8"),
    positive(index=83),  # Trajectory indices are not request citation IDs.
    positive(index=True),
    positive(url="https://invented.example/"),
    positive("8 am Rainy 44° 10 in precipitation"),
    {"success": True, "reason": "done", "evidence": []},
])
def test_ungrounded_positive_gets_one_repair_without_altering_evidence(bad):
    judge = Judge([bad, positive()])
    result = evaluate(judge)
    assert result["success"] is True
    assert len(judge.messages) == 2
    assert judge.messages[0] == judge.messages[1][:-1]
    original = json.loads(judge.messages[0][1]["content"])
    repaired = json.loads(judge.messages[1][1]["content"])
    assert original == repaired
    assert original["actual_observations"][0]["citation_index"] == 0
    assert original["actual_observations"][0]["index"] == 83
    assert "gold" not in original
    feedback = json.loads(judge.messages[1][-1]["content"])
    assert feedback["judge_response_validation"] == "ungrounded_positive_citation"
    assert result["judge_validation"]["attempts"][0]["repair_requested"] is True


@pytest.mark.parametrize("bad", [
    "The answer looks correct.",
    '{"success": true',
    {"reason": "There is no verdict.", "evidence": []},
    {"success": 1, "reason": "Wrong success type.", "evidence": []},
    {"success": "true", "reason": "Wrong success type.", "evidence": []},
    '{"success":true,"success":false,"evidence":[]}',
])
def test_invalid_json_and_schema_use_same_single_repair_budget(bad):
    judge = Judge([bad, positive()])
    result = evaluate(judge)
    assert result["success"] is True
    assert result["judge_validation"]["request_count"] == 2
    assert result["judge_validation"]["attempts"][0]["validation"] in {
        "invalid_json", "invalid_schema"}


@pytest.mark.parametrize("verdict", [False, None])
def test_valid_negative_or_unknown_never_gets_a_second_chance(verdict):
    judge = Judge([{"success": verdict, "reason": "Requirements not established.", "evidence": []}])
    result = evaluate(judge)
    assert result["success"] is verdict
    assert len(judge.messages) == 1
    assert result["judge_validation"]["attempts"][0]["validation"] == "valid"


@pytest.mark.parametrize("second", [
    positive("unobserved text"),
    positive(index=83),
    "Still no JSON response.",
])
def test_invalid_repair_remains_unknown_at_fixed_limit(second):
    judge = Judge([positive("forecast ... precipitation"), second])
    result = evaluate(judge)
    assert result["success"] is None
    assert result["status"] == "unverified"
    assert "registered format repair" in result["reason"]
    assert len(judge.messages) == 2
    assert result["judge_validation"]["request_count"] == 2


def test_repair_can_correct_a_positive_claim_to_observed_failure():
    judge = Judge([positive("nonexistent quote"),
                   {"success": False, "reason": "The requested period was not provided.", "evidence": []}])
    assert evaluate(judge)["success"] is False
    assert len(judge.messages) == 2


def test_transport_error_is_not_retried_and_is_auditable_without_secrets():
    judge = Judge([RuntimeError("sensitive provider details must not be serialized")])
    result = evaluate(judge)
    assert result["success"] is None
    assert result["status"] == "api_error"
    assert result["judge_validation"]["request_count"] == 1
    assert result["judge_validation"]["attempts"][0] == {
        "attempt": 1, "validation": "request_error", "error_type": "RuntimeError"}
    assert "sensitive" not in json.dumps(result)


def test_citation_without_observed_saved_source_does_not_call_judge():
    current = episode()
    current["task_outputs"]["A"][0]["sources"][0]["quote"] = "Never observed"
    judge = Judge([])
    result = evaluate(judge, current)
    assert result["success"] is False
    assert not judge.messages


@pytest.mark.parametrize("observations", [
    [], None, "corrupt observation container", [None], [42],
    [{"url": URL, "text": None}], [{"url": URL}],
    [{"url": None, "text": TEXT}],
])
def test_missing_or_corrupt_observations_cannot_be_repaired_to_success(observations):
    current = episode()
    current["observations"] = observations
    current["actions"][0].pop("after")
    judge = Judge([])
    result = evaluate(judge, current)
    assert result["success"] is None
    assert result["status"] == "unverified"
    assert result["evidence_policy"] == OUTCOME_EVIDENCE_POLICY
    assert not judge.messages


def test_corrupt_partial_capture_does_not_turn_unmatched_quote_into_known_failure():
    current = episode()
    current["observations"] = [{"url": "https://example.test/other", "text": "Other page"}, None]
    current["actions"][0].pop("after")
    result = evaluate(Judge([]), current)
    assert result["success"] is None


def test_corrupt_capture_is_never_supplied_as_real_judge_evidence():
    current = episode()
    current["observations"].append({"url": URL, "text": None})
    judge = Judge([positive()])
    assert evaluate(judge, current)["success"] is True
    payload = json.loads(judge.messages[0][1]["content"])
    assert len(payload["actual_observations"]) == 1
    assert payload["actual_observations"][0]["text"] == TEXT


def test_corrupt_final_capture_does_not_make_a_stale_page_a_final_state():
    current = episode()
    current["observations"].append({"url": URL, "text": None})
    current["actions"][0].pop("after")
    verifier = {"method": "deterministic", "checks": [
        {"type": "text_contains", "value": "74°"}]}
    result = asyncio.run(evaluate_task("Find forecast", verifier, current))
    assert result["success"] is None


def test_deterministic_output_checks_cannot_pass_with_missing_source_evidence():
    current = episode()
    current["observations"] = []
    current["actions"][0].pop("after")
    verification = {"method": "deterministic", "checks": [
        {"type": "saved_output_contains", "value": "74°"}]}
    result = asyncio.run(evaluate_task("Find forecast", verification, current))
    assert result["success"] is None


def test_known_durable_output_failure_survives_missing_observations():
    current = episode()
    current["observations"] = []
    current["actions"][0].pop("after")
    verification = {"method": "deterministic", "checks": [
        {"type": "saved_output_contains", "value": "mandatory unavailable detail"}]}
    result = asyncio.run(evaluate_task("Find forecast", verification, current))
    assert result["success"] is False


def test_prior_positive_cache_cannot_bypass_lost_source_observation():
    current = episode()
    goal = "Find forecast"
    verifier = {"method": "llm_judge"}
    current["verified_tasks"] = {"A": {
        "output_hash": hashlib.sha256(json.dumps(current["task_outputs"]["A"],
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "goal_version": 0, "goal": goal,
        "verification_sha256": hashlib.sha256(json.dumps(verifier,
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "result": {"success": True, "status": "passed"},
    }}
    current["observations"] = []
    current["actions"][0].pop("after")
    result = asyncio.run(evaluate_task(goal, verifier, current, judge=Judge([])))
    assert result["success"] is None
    assert not result.get("reused_independent_verification")


def test_missing_observations_propagate_to_task_branch_and_episode_outcomes():
    current = episode()
    current["observations"] = [None]
    current["actions"][0].pop("after")
    current["protocol"] = "continuous_v2"
    current["task_states"]["A"].update(completed=True, completion_index=3)
    case = {"case_id": "C", "goal": "Find forecast", "events": [],
            "gold": {"required_nodes": ["CURRENT_TASK"]}}
    result = asyncio.run(evaluate_episode(case, current, Judge([])))
    assert result["task_success"] is None
    assert result["task_completion_results"]["CURRENT_TASK"] is None
    assert result["phase_results"]["CURRENT_TASK"]["success"] is None
    assert result["episode_success"] is None
    assert result["branch_success"] is None
    assert result["evaluation_coverage"] is False


def test_observed_ignore_distraction_is_failure_despite_lost_source_evidence():
    current = episode()
    current["observations"] = []
    current["actions"][0].pop("after")
    current.update(protocol="continuous_v2", exposed=True)
    current["task_states"]["A"].update(completed=True, completion_index=3)
    current["events"] = {"E1": {"delivered": True, "delivery_index": 1, "started_index": 2}}
    case = {"case_id": "C", "goal": "Find forecast",
            "event": {"id": "E1", "text": "Irrelevant notification", "execution": {"kind": "none"}},
            "gold": {"decision": "IGNORE"}}
    result = asyncio.run(evaluate_episode(case, current, Judge([])))
    assert result["task_success"] is None
    assert result["distraction"] is True
    assert result["episode_success"] is False
    assert result["branch_success"] is False


def test_actual_ellipsis_text_is_allowed_when_verbatim_observed():
    current = episode()
    current["observations"][0]["text"] += "\nObserved statement ... is printed on the source."
    current["actions"][0]["after"] = copy.deepcopy(current["observations"][0])
    judge = Judge([positive("Observed statement ... is printed on the source.")])
    assert evaluate(judge, current)["success"] is True
    assert len(judge.messages) == 1


def test_async_provider_and_literal_json_newlines_match_decoded_evidence():
    calls = []

    async def judge(messages):
        calls.append(messages)
        return json.dumps(positive("ZIP 90028 hourly forecast\nThursday, October 8"))

    assert evaluate(judge)["success"] is True
    assert len(calls) == 1
