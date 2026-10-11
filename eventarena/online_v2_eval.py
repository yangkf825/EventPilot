"""Gold-side outcome verification for autonomous browser episodes.

Classification and behavior are evaluated independently. Completion claims,
model schedules, and answers never substitute for executed, grounded evidence.
Unknown provider/site/Judge outcomes remain null rather than fabricated labels.
"""
from __future__ import annotations

import inspect
import hashlib
import json
import re
from collections.abc import Mapping
from urllib.parse import urlparse

from .online_eval import evaluate_phase, parse_judge, JUDGE_SYSTEM
from .json_protocol import extract_object, JSONEnvelopeError

JUDGE_SYSTEM_V2 = JUDGE_SYSTEM + '''
For this v2 protocol, distinguish an observed Agent failure from an unavailable
evaluation. Return false when the available saved answer/trace establishes a
wrong entity, missing requested detail, unsupported result, or unmet effective
requirement. These are observable failures, not unknown outcomes. Return null
only when missing/corrupt evidence, inaccessible sources, or another evaluation
limitation prevents a determination. Never turn an incomplete answer into true
because its citations happen to match a homepage. For a scoped requirement,
grade that registered requirement independently of other task requirements.
For each positive evidence citation, copy a short, contiguous passage from the
decoded text of the cited actual_observations row. Do not summarize, add an
ellipsis, join separated passages, or write literal backslash-n characters in
place of observed line breaks. If multiple passages are needed, cite them as
separate evidence entries. Prefer a short quotation (approximately 280
characters or fewer); this is a formatting aid, not a limit on the evidence
available to you. Only quote text actually supporting the registered goal.
The copied quotation must still match the observed text after whitespace
normalization. Never substitute a homepage title for evidence of task adequacy.
'''

JUDGE_CITATION_POLICY = "verbatim_citations_v1"
JUDGE_FORMAT_RETRIES = 1
OUTCOME_EVIDENCE_POLICY = "observed_evidence_integrity_v1"


def _validate_judge_reply(raw, phase):
    """Classify structural failures without changing their outcome semantics."""
    try:
        parsed = extract_object(raw)
    except JSONEnvelopeError:
        return None, "invalid_json"
    if "success" not in parsed or (parsed["success"] is not None and
                                    not isinstance(parsed["success"], bool)):
        return None, "invalid_schema"
    checked = parse_judge(parsed, phase)
    if parsed["success"] is True and checked["success"] is not True:
        return parsed, "ungrounded_positive_citation"
    # A negative or explicitly unknown judgment is an evaluation, not a format
    # failure. In particular, never request a second chance after a valid false.
    return parsed, "valid"


def _citation_repair_message(failure):
    return {
        "role": "user",
        "content": json.dumps({
            "judge_response_validation": failure,
            "instruction": (
                "Reissue exactly one JSON evaluation of the same registered goal "
                "and unchanged evidence above. This is the only format repair. "
                "Your earlier response failed JSON/schema validation or lacked a "
                "matching citation for a positive claim. Recheck answer adequacy; "
                "do not preserve a positive claim if it is unsupported. For true, "
                "copy actual_observations[*].citation_index and its exact URL and "
                "a short contiguous verbatim text passage. Use separate entries "
                "for separate passages. Do not insert ellipses or double-escape "
                "observed newlines. Return false when the available trace shows "
                "a task failure, or null if a determination is unavailable."
            ),
        }, ensure_ascii=False),
    }

MODEL_FAILURES = {"invalid_output", "context_overflow", "output_truncated", "checkpoint_setup_failed", "step_budget_exhausted", "loop_guard_exhausted"}
ENV_FAILURES = {"provider_error", "website_unavailable", "browser_error", "interrupted", "checkpoint_setup_unverified",
                "checkpoint_replay_mismatch", "checkpoint_capture_unavailable", "checkpoint_projection_unavailable"}
EFFECTIVE_ACTIONS = {"GOTO", "CLICK", "TYPE", "SELECT", "BACK", "SCROLL", "SAVE_RESULT", "COMPLETE_TASK"}


def _result(success, reason, source="deterministic", **extra):
    return {"success": success, "status": "passed" if success is True else "failed" if success is False else "unverified",
            "reason": reason, "outcome_source": source, "judge_human_reviewed": False, **extra}


def _and(values):
    values = list(values)
    return False if any(v is False for v in values) else None if any(v is None for v in values) else True


def _successful_actions(episode, task_id=None):
    return [r for r in episode.get("actions", []) if isinstance(r, Mapping) and not r.get("error") and
            (task_id is None or r.get("task_id") == task_id)]


def _task_phase(episode, task_id, goal_version=None):
    # Source-validation events may provide legitimate evidence for A. Include
    # their real observations for the Judge, while placing the task's own final
    # capture last so deterministic DOM checks remain task-local.
    supplied = episode.get("observations", [])
    evidence_incomplete = not isinstance(supplied, list)
    observations = list(supplied) if isinstance(supplied, list) else []
    actions = _successful_actions(episode, task_id)
    # 'after' observations cover the final action even if the process stopped
    # before the next actor call. They are real captures, not model statements.
    for row in actions:
        after = row.get("after")
        if after and row.get("active_task_after", task_id) == task_id:
            observations.append(after)
    unique, seen = [], set()
    final_capture_unavailable = False
    # Remove identical recaptures, retaining complete observed page states and
    # a task-local final observation. No page text is shortened or invented.
    for position, observation in enumerate(reversed(observations)):
        if (not isinstance(observation, Mapping) or
                not isinstance(observation.get("url"), str) or not observation.get("url") or
                not isinstance(observation.get("text"), str)):
            evidence_incomplete = True
            if position == 0:
                final_capture_unavailable = True
            continue
        state = {k: v for k, v in observation.items()
                 if k not in {'index', 'action_index', 'task_id', 'provenance'}}
        signature = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if signature not in seen:
            unique.append(observation)
            seen.add(signature)
    observations = list(reversed(unique))
    outputs = [o for o in episode.get("task_outputs", {}).get(task_id, [])
               if goal_version is None or o.get("goal_version", 0) == goal_version]
    result = outputs[-1] if outputs else {}
    return {"status": "agent_claimed_done", "answer": result.get("text", ""),
            "actions": actions, "observations": observations,
            "result_observation": observations[-1] if observations and not final_capture_unavailable else {},
            "observation_evidence_incomplete": evidence_incomplete,
            "saved_output": result, "task_state": episode.get("task_states", {}).get(task_id, {})}


def _output_check(check, phase):
    output = phase["saved_output"]
    kind, value = check.get("type"), check.get("value")
    text = output.get("text", "")
    if kind == "saved_output_contains":
        return str(value).casefold() in text.casefold()
    if kind == "saved_output_regex":
        return bool(re.search(str(value), text, re.I))
    if kind == "saved_sources_observed":
        sources = output.get("sources", [])
        observations = phase["observations"]
        if not isinstance(sources, list) or not sources or not all(
                isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"]
                and isinstance(s.get("quote"), str) and s["quote"].strip() for s in sources):
            return False
        # Lost/corrupt captures cannot establish that an actor invented a
        # quotation. A complete observed trace with no match does establish an
        # unsupported saved citation. Do not conflate the two outcomes.
        usable = [o for o in observations if isinstance(o.get("url"), str) and o["url"]
                  and isinstance(o.get("text"), str)]
        incomplete = (phase.get("observation_evidence_incomplete", False)
                      or len(usable) != len(observations))
        if not usable:
            return None
        matches = [any(o["url"] == s["url"] and
                       " ".join(s["quote"].split()) in " ".join(o["text"].split())
                       for o in usable) for s in sources]
        if all(matches):
            return True
        return None if incomplete else False
    if kind == "saved_source_host":
        host = str(value)
        return any((urlparse(s.get("url", "")).hostname or "") == host or
                   (urlparse(s.get("url", "")).hostname or "").endswith("." + host)
                   for s in output.get("sources", []) if isinstance(s, dict))
    return None


async def evaluate_task(goal, verification, episode, task_id="A", judge=None, goal_version=None):
    """Verify a durable task output and registered outcome, not a done claim."""
    if goal_version is None:
        goal_version = episode.get('task_states', {}).get(task_id, {}).get('goal_version', 0)
    phase = _task_phase(episode, task_id, goal_version)
    cached = episode.get("verified_tasks", {}).get(task_id, {})
    output_hash = hashlib.sha256(json.dumps(episode.get("task_outputs", {}).get(task_id, []), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    verifier_hash = hashlib.sha256(json.dumps(verification, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if not phase["saved_output"]:
        return _result(False, "No durable result for the effective task version")
    if not phase["saved_output"].get("sources"):
        return _result(False, "Saved research result has no observed supporting citation")
    verification = verification or {"method": "llm_judge"}
    checks = verification.get("checks", [])
    custom_types = {"saved_output_contains", "saved_output_regex", "saved_sources_observed", "saved_source_host"}
    # Grounding is mandatory for every research task even if authors forget to
    # list it. Adequacy remains independently checked by the registered method.
    custom = [_output_check(check, phase) for check in checks if check.get("type") in custom_types]
    custom.append(_output_check({"type": "saved_sources_observed"}, phase))
    if any(v is False for v in custom):
        return _result(False, "Registered durable-output or source-grounding check failed", checks=custom)
    if any(v is None for v in custom):
        return _result(None, "Required observed source evidence is missing or corrupt",
                       checks=custom, evidence_policy=OUTCOME_EVIDENCE_POLICY)
    # Never let a prior positive judgment bypass a currently missing/corrupt
    # source capture. This check uses only executed observations and saved data.
    if (cached.get("output_hash") == output_hash and cached.get("goal_version") == goal_version
            and cached.get('goal') == goal and cached.get('verification_sha256') == verifier_hash
            and cached.get("result")):
        return {**cached["result"], "reused_independent_verification": True}
    remaining = [c for c in checks if c.get("type") not in custom_types]
    method = verification.get("method", "llm_judge")
    if method == "deterministic" and not remaining:
        if not checks:
            return _result(None, "Grounding alone does not prove answer adequacy")
        return _result(True, "Registered durable output and grounded citations verified", checks=custom)
    judge_validation = {"policy": JUDGE_CITATION_POLICY,
                        "max_format_retries": JUDGE_FORMAT_RETRIES,
                        "request_count": 0, "attempts": []}

    async def policy_judge(messages):
        messages = [dict(m) for m in messages]
        messages[0]['content'] = JUDGE_SYSTEM_V2
        # Repair only malformed independent evaluations. The observations and
        # task requirements stay identical and neither request receives Gold.
        # Transport errors are not format errors and use no extra request here.
        for attempt in range(JUDGE_FORMAT_RETRIES + 1):
            audit = {"attempt": attempt + 1}
            judge_validation["attempts"].append(audit)
            judge_validation["request_count"] += 1
            try:
                raw = judge(messages)
                raw = await raw if inspect.isawaitable(raw) else raw
            except Exception as error:
                audit.update(validation="request_error", error_type=type(error).__name__)
                raise
            parsed, failure = _validate_judge_reply(raw, phase)
            audit["validation"] = failure
            if failure == "valid":
                return parsed
            if attempt < JUDGE_FORMAT_RETRIES:
                # Retain the original actual-observations message; no invented
                # quotation or automatically selected evidence is supplied.
                messages = messages + [_citation_repair_message(failure)]
                audit["repair_requested"] = True
            else:
                return {"success": None, "evidence": [],
                        "reason": "Independent Judge response remains invalid after "
                                  "the registered format repair: " + failure}

    result = await evaluate_phase(goal, phase, {**verification, "checks": remaining},
                                  judge=policy_judge if judge is not None else None)
    result["durable_output_checks"] = custom
    if judge_validation["request_count"]:
        result["judge_validation"] = judge_validation
    return result


def _gold(case):
    events = case.get("events") or ([case["event"]] if case.get("event") else [])
    gold = case.get("gold", {})
    if "decisions" in gold:
        labels = gold["decisions"]
    elif events:
        labels = {events[0]["id"]: gold}
    else:
        labels = {}
    labels = {eid: ({"decision": v} if isinstance(v, str) else v) for eid, v in labels.items()}
    return events, labels, gold


def _verification(case, node, event=None, update=False):
    registered = case.get("verification", {})
    if node in registered:
        return registered[node]
    if node == "CURRENT_TASK" and registered.get("method"):
        return registered
    execution = (event or {}).get("execution", {})
    if update:
        return execution.get("updated_verification", execution.get("verification", {}))
    return execution.get("verification", {})


def _actual_schedule(episode, event_states):
    nodes = []
    current = episode.get("task_states", {}).get("A", {})
    if current.get("stopped"):
        if current.get("stop_index") is not None:
            nodes.append((current["stop_index"], "TERMINATE_TASK"))
    elif current.get("verified_completion_index", current.get("completion_index")) is not None:
        nodes.append((current.get("verified_completion_index", current.get("completion_index")), "CURRENT_TASK"))
    for eid, state in event_states.items():
        if state.get("started_index") is not None:
            nodes.append((state["started_index"], eid))
    return [name for _, name in sorted(nodes, key=lambda item: (item[0], item[1] == "TERMINATE_TASK"))]


async def evaluate_episode(case, episode, judge=None):
    events, labels, gold = _gold(case)
    event_map = {e["id"]: e for e in events}
    current = episode.get("task_states", {}).get("A", {})
    states = episode.get("events", {})
    environment_failure = episode.get("status") in ENV_FAILURES
    base = {"case_id": case["case_id"], "protocol": episode.get("protocol"),
            "gold_human_reviewed": case.get("annotation", {}).get("human_reviewed", False),
            "judge_human_reviewed": False, "exposed": bool(episode.get("exposed")),
            "environment_valid": False if environment_failure else True,
            "phase_results": {}, "event_results": {}, "resumed": None,
            "resume_completion": None, "updated_goal_success": None, "effective_constraints": None,
            "resumed_checkpoint_consistent": None, "stale_action_after_replan_count": 0}
    if episode.get("protocol") == "frozen_checkpoint_v2":
        return {**base, "episode_success": None, "branch_success": None,
                "status": "decision_only", "failure_category": None, "evaluation_coverage": False}

    registered_revisions = [event for event in events if labels.get(event["id"], {}).get("follow_up") == "REPLAN"]
    registered_terminations = [event for event in events if labels.get(event["id"], {}).get("follow_up") == "TERMINATE"]
    delivered_revisions = [event for event in registered_revisions if states.get(event["id"], {}).get("delivered")]
    delivered_terminations = [event for event in registered_terminations if states.get(event["id"], {}).get("delivered")]
    # Gold registers the complete episode, including events that are still in
    # the future. Only an executed, delivered effect changes the task version
    # whose saved output is being verified. The event/schedule checks below
    # separately enforce every registered obligation.
    revised = sorted([event for event in delivered_revisions if states.get(event["id"], {}).get("effect_applied")],
                     key=lambda event: states[event["id"]].get("accepted_index")
                     if states[event["id"]].get("accepted_index") is not None else -1)
    terminated = [event for event in delivered_terminations if states.get(event["id"], {}).get("effect_applied")
                  or current.get("stopped")]
    goal = revised[-1].get("execution", {}).get("updated_goal", case["goal"]) if revised else case["goal"]
    task_verification = _verification(case, "CURRENT_TASK")
    initial_bindings = [c for c in task_verification.get("checks", []) if c.get("type") == "required_event_completed"]
    if revised:
        task_verification = (revised[-1].get("execution", {}).get("updated_verification")
                             or _verification(case, "CURRENT_TASK_UPDATED", revised[-1], update=True))
        # The registered revisions retain the initial acceptance checklist.
        # Keep its existing bindings even when an event-local content verifier
        # omits the case-level copy; never inherit future revision content.
        task_verification = {**task_verification, "checks": list(task_verification.get("checks", []))}
        task_verification["checks"] += [binding for binding in initial_bindings
                                       if binding not in task_verification["checks"]]
    content_goal = task_verification.get("goal") or goal
    bindings = [c for c in task_verification.get("checks", []) if c.get("type") == "required_event_completed"]
    task_verification = {**task_verification, "checks": [c for c in task_verification.get("checks", [])
                                                        if c.get("type") != "required_event_completed"]}
    goal_version = len(revised)

    async def observed_task_result(task_goal, verifier, task_id, version=None):
        # A transport/browser interruption before any durable output is not
        # evidence that the actor answered incorrectly. Preserve an observed
        # invalid output as a failure when an output actually exists.
        if version is None:
            version = episode.get("task_states", {}).get(task_id, {}).get("goal_version", 0)
        if environment_failure and not _task_phase(episode, task_id, version)["saved_output"]:
            return _result(None, "Infrastructure interrupted before a durable task result was available")
        return await evaluate_task(task_goal, verifier, episode, task_id, judge, version)

    task_result = await observed_task_result(content_goal, task_verification, "A", goal_version)
    task_result.update(required=not bool(terminated), effective_goal=goal,
                       content_verification_goal=content_goal, tasked=True)
    base["phase_results"]["CURRENT_TASK"] = task_result
    task_success = task_result["success"]
    if revised:
        base["updated_goal_success"] = task_success
        base["effective_constraints"] = task_success

    for event in events:
        eid, label = event["id"], labels.get(event["id"], {})
        state = states.get(eid, {})
        execution = event.get("execution", {})
        kind = execution.get("kind", "none")
        delivered = state.get("delivered", False)
        if not delivered:
            result = _result(None if environment_failure else False, "Event was never delivered")
        elif kind == "research_task":
            result = await observed_task_result(execution.get("goal", event["text"]),
                                               _verification(case, eid, event), eid)
        elif kind == "update_goal":
            applied = bool(state.get("effect_applied"))
            result = _result(True if applied else None if environment_failure else False,
                             "Authorized goal revision must be accepted by an executed action")
        elif kind == "cancel":
            applied = bool(state.get("effect_applied")) and bool(current.get("stopped"))
            result = _result(True if applied else None if environment_failure else False,
                             "Cancellation must be accepted and the original task stopped")
        else:
            result = _result(None, "No executable event task registered")
        event_actions = _successful_actions(episode, eid)
        substantive = (state.get("started_index") is not None or
                       any(r.get("action", {}).get("op") in EFFECTIVE_ACTIONS for r in event_actions))
        # Switching into an irrelevant event and using the browser is a real
        # distraction. Merely acknowledging a notification is not.
        distraction = substantive or state.get("effect_applied", False)
        handled_index = state.get("verified_completion_index", state.get("completion_index"))
        resumed_actions = [r for r in _successful_actions(episode) if handled_index is not None and
                           r.get('index',-1) > handled_index and (
                               r.get('active_task_after') == 'A' and r.get('action',{}).get('op') in {'SWITCH_TASK','OPEN_TASK','FOCUS_TASK'}
                               or r.get('task_id') == 'A' and r.get('action',{}).get('op') in EFFECTIVE_ACTIONS - {'COMPLETE_TASK'})]
        continued_after_delivery = [r for r in _successful_actions(episode, "A") if delivered and
                                    r.get("index", -1) >= state.get("delivery_index", 0) and
                                    r.get("action", {}).get("op") in EFFECTIVE_ACTIONS and
                                    (handled_index is None or r.get("index", -1) < handled_index)]
        timing = None
        if label.get("decision") == "INTERRUPT":
            timing = (False if continued_after_delivery else True if delivered and state.get("started_index") is not None
                      else None if environment_failure else False)
        elif label.get("decision") == "DEFER":
            completed = current.get("verified_completion_index", current.get("completion_index"))
            timing = (completed is not None and completed < state["started_index"]
                      if state.get("started_index") is not None else None if environment_failure else False)
        event_result = {**result, **{k: state.get(k) for k in ("delivery_index", "started_index", "completion_index", "accepted_index")},
                        "delivered": delivered, "start_index": state.get("started_index"),
                        "timely": timing, "distraction": bool(distraction), "completed": result["success"],
                        "accepted": state.get("accepted_index") is not None,
                        "started": state.get("started_index") is not None,
                        "unnecessary_action_count": len(event_actions) if label.get("decision") == "IGNORE" else 0,
                        "queued": episode.get("decisions", {}).get(eid) == "DEFER",
                        "resumed": True if resumed_actions else None if environment_failure else False,
                        "continued_before_handling": bool(continued_after_delivery),
                        "required": label.get("decision") != "IGNORE", "tasked": kind == "research_task"}
        base["event_results"][eid] = event_result
        base["phase_results"][eid] = event_result

    if bindings:
        bound = [base["event_results"].get(c.get("event_id", c.get("value")), {}).get("success") for c in bindings]
        task_success = _and([task_success, *bound])
        base["phase_results"]["CURRENT_TASK"].update(success=task_success,
                                                     required_event_checks=bound)
        if revised:
            base["updated_goal_success"] = task_success
            base["effective_constraints"] = task_success

    cancel_deliveries = [states.get(e["id"], {}).get("delivery_index") for e in delivered_terminations]
    cancellation_index = min([i for i in cancel_deliveries if i is not None], default=None)
    violations = [r for r in _successful_actions(episode, "A") if cancellation_index is not None and
                  r.get("index", -1) >= cancellation_index and r.get("action", {}).get("op") in EFFECTIVE_ACTIONS]
    base.update(task_success=task_success, cancelled=(any(states.get(e["id"], {}).get("effect_applied") for e in terminated)
                                                   or (bool(terminated) and bool(current.get("stopped")))),
                stopped=bool(current.get("stopped")), post_cancel_action_count=len(violations),
                post_cancel_violation=bool(violations), actual_schedule=_actual_schedule(episode, states),
                distraction=any(r["distraction"] for eid, r in base["event_results"].items() if labels.get(eid, {}).get("decision") == "IGNORE"))
    base["task_completion_results"] = {"CURRENT_TASK": task_success} if not terminated else {}
    base["replan_adopted"] = (True if all(states.get(e["id"], {}).get("effect_applied") for e in delivered_revisions)
                              else None if environment_failure else False) if delivered_revisions else None
    base['effective_constraint_results'] = []
    if revised:
        for constraint in task_verification.get('effective_constraints', []):
            if constraint.get('active') is False or constraint.get('removed') is True:
                continue
            requirement = constraint['requirement']
            scoped = constraint.get('verification') or {
                'method': 'llm_judge', 'requirements': [requirement],
                'checks': [{'type': 'saved_sources_observed'}]}
            result = await observed_task_result(requirement, scoped, 'A', goal_version)
            base['effective_constraint_results'].append({**constraint, 'success': result['success'],
                                                        'source': 'independent_requirement_verification',
                                                        'evaluation': result})
        values = [r['success'] for r in base['effective_constraint_results']]
        base['effective_constraints'] = _and(values) if values else None
    base["termination_compliant"] = (False if violations else True if current.get("stopped")
                                      else None if environment_failure else False) if delivered_terminations else None
    conditions, branch = [], "MULTI" if len(events) > 1 else "BASELINE"
    if not terminated:
        conditions.append(task_success)
    for eid, label in labels.items():
        event = base["event_results"][eid]
        decision, follow = label.get("decision"), label.get("follow_up")
        if len(events) == 1:
            branch = follow if decision == "INTERRUPT" else decision
        if decision == "IGNORE":
            conditions += [not event["distraction"]]
        elif decision == "DEFER":
            conditions += [event["success"], event["timely"]]
        elif decision == "INTERRUPT":
            conditions += [event["success"], event["timely"]]
            if follow == "HANDLE":
                conditions += [event["resumed"]]
                base["resumed"] = event["resumed"]
                base["resume_completion"] = _and([event["resumed"], task_success])
                # Persistent original tabs and captured states provide a real
                # recovery audit. The effective task version must be unchanged.
                completed_at = states.get(eid,{}).get('verified_completion_index',event.get('completion_index'))
                resumes = [r for r in _successful_actions(episode) if completed_at is not None and
                    r.get('index',-1) > completed_at and (
                        r.get('active_task_after') == 'A' and r.get('action',{}).get('op') in {'SWITCH_TASK','OPEN_TASK','FOCUS_TASK'}
                        or r.get('task_id') == 'A' and r.get('action',{}).get('op') in EFFECTIVE_ACTIONS - {'COMPLETE_TASK'})]
                checkpoints = [cp for cp in episode.get("checkpoints", []) if any(e.get("id") == eid for e in cp.get("events", []))]
                if resumes and checkpoints:
                    saved_state = checkpoints[-1]["state"]
                    resume = resumes[0]
                    switching = resume['action']['op'] in {'SWITCH_TASK','OPEN_TASK','FOCUS_TASK'}
                    before_resume = resume['after'] if switching else resume['before']
                    def controls(observation):
                        return [{k: c.get(k) for k in ("name", "type", "value", "checked", "selected")}
                                for c in observation.get("candidates", []) if c.get("tag") in {"input", "select", "textarea"}]
                    expected_version = sum(e.get('execution',{}).get('kind') == 'update_goal'
                        and states.get(e['id'],{}).get('delivered') is True
                        and states.get(e['id'],{}).get('effect_applied') is True
                        and states.get(e['id'],{}).get('accepted_index',float('inf')) <= resume['index'] for e in events)
                    restored_version = (resume.get('active_task_state_after', {}) if switching
                                        else resume.get('task_state_before', {})).get('goal_version',current.get('goal_version',0))
                    goal_consistent = restored_version == expected_version
                    restore = (saved_state.get("url") == before_resume.get("url") and
                               controls(saved_state) == controls(before_resume) and goal_consistent)
                    base["resumed_checkpoint_consistent"] = restore
                    base["recovery_audit"] = {"same_url": saved_state.get("url") == before_resume.get("url"),
                                               "same_form_values": controls(saved_state) == controls(before_resume),
                                               "effective_goal_version_consistent": goal_consistent,
                                               "expected_goal_version": expected_version,
                                               "restored_goal_version": restored_version}
                    conditions += [restore]
            elif follow == "REPLAN":
                conditions += [states.get(eid, {}).get("effect_applied", False)]
                if base['effective_constraint_results']:
                    conditions += [base['effective_constraints']]
            elif follow == "TERMINATE":
                conditions += [bool(current.get("stopped")), not bool(violations)]

    schedule = base["actual_schedule"]
    node = "TERMINATE_TASK" if registered_terminations else "CURRENT_TASK"
    required = gold.get("required_nodes") or ([node] + [eid for eid, label in labels.items() if label.get("decision") != "IGNORE"])
    required = [node if n == "CURRENT_TASK" else n for n in required]
    precedence = gold.get("precedence", [])
    if not precedence:
        precedence = [[eid, node] if l.get("decision") == "INTERRUPT" else [node, eid]
                      for eid, l in labels.items() if l.get("decision") != "IGNORE"]
    precedence = [[node if n == "CURRENT_TASK" else n for n in edge] for edge in precedence]
    inverted = any(a in schedule and b in schedule and schedule.index(a) >= schedule.index(b) for a, b in precedence)
    complete_edges = all(a in schedule and b in schedule for a, b in precedence)
    order = False if inverted else True if complete_edges else None if environment_failure else False
    extra = set(schedule) - set(required)
    # A completed original report before an as-yet unseen withdrawal is not
    # itself a known scheduling violation. The final stop obligation remains
    # pending; do not use that hidden future command to condemn this prefix.
    if environment_failure and registered_terminations and not delivered_terminations:
        extra.discard("CURRENT_TASK")
    extra_nodes = bool(extra)
    complete_nodes = len(schedule) == len(required) and set(schedule) == set(required)
    exact = (False if extra_nodes or order is False else order if complete_nodes
             else None if environment_failure else False)
    base.update(order_correct=order, schedule_exact_match=exact, required_nodes=required, branch=branch)
    if len(events) > 1:
        conditions += [order, exact]
    success = _and(conditions)
    # Infrastructure failures cannot establish a task/model failure; deliberate
    # violations can still be retained in the facts but aggregate ESR unknown.
    if environment_failure:
        success = None
    elif episode.get("status") in MODEL_FAILURES:
        success = False
    elif events and not episode.get("exposed"):
        # Actor prematurely stopped/answered or budgeted out before the trigger.
        success = False
    base.update(episode_success=success, branch_success=success,
                evaluation_coverage=success is not None,
                status=episode.get("status") if environment_failure else "passed" if success is True else "failed" if success is False else "unverified",
                failure_category=episode.get("status") if environment_failure or episode.get("status") in MODEL_FAILURES else
                ("not_exposed" if events and not episode.get("exposed") else "behavior_or_outcome" if success is False else "judge_or_outcome_unverified" if success is None else None),
                reason="Registered actual outcomes and behavior constraints evaluated independently of predicted labels")
    return base
