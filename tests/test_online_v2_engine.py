import asyncio
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from eventarena.online_v2_engine import (context_for_view, decision_messages, match_trigger,
    normalize_action, prepare_state_pair, replay_checkpoint_audit, run_episode, run_frozen, lossless_trajectory,
    decode_lossless_trajectory)


class Model:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.messages = []

    def call(self, messages):
        self.messages.append(messages)
        value = next(self.actions)
        return value if isinstance(value, str) else json.dumps(value)


class Page:
    def __init__(self):
        self.url, self.value = "about:blank", ""

    def on(self, *_):
        pass


class Context:
    async def new_page(self):
        return Page()


class FakeBrowser:
    def __init__(self, run_dir, headless=False):
        self.context, self.page, self.history = Context(), Page(), []
        self.pending_popups = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def observe(self):
        return {"url": self.page.url, "title": "Research", "text": f"Research results {self.page.value}",
                "candidates": [{"node_id": "0", "tag": "input", "name": "query", "type": "text", "value": self.page.value},
                               {"node_id": "1", "tag": "button", "text": "Search"}], "http_status": 200}

    async def save(self, action=None, phase=None):
        self.history.append({"action": action, "phase": phase, "observation": await self.observe()})

    async def execute(self, action, phase=None):
        if action["op"] == "GOTO":
            self.page.url = action["url"]
        elif action["op"] == "TYPE":
            self.page.value = action["value"]
        await self.save(action, phase)

    async def inject(self, events):
        self.injected = events


def verification(value):
    return {"method": "deterministic", "checks": [{"type": "saved_output_contains", "value": value},
            {"type": "saved_sources_observed"}]}


def case(branch="IGNORE"):
    kind = "update_goal" if branch == "REPLAN" else "cancel" if branch == "TERMINATE" else "research_task"
    execution = {"kind": kind, "goal": "Find beta", "url": "https://example.test/b", "verification": verification("beta")}
    if branch == "REPLAN":
        execution.update(updated_goal="Find gamma", updated_verification=verification("gamma"))
    return {"case_id": "V2", "goal": "Find alpha", "url": "https://example.test/a",
            "rules": ["Use only authorized research pages."],
            "trigger": {"kind": "control_value", "name": "query", "value": "alpha", "min_non_navigation_actions": 1},
            "event": {"id": "E1", "source": "User", "text": "New request", "execution": execution},
            "gold": {"decision": branch if branch in ("IGNORE", "DEFER") else "INTERRUPT",
                     "follow_up": None if branch in ("IGNORE", "DEFER") else branch},
            "verification": {"CURRENT_TASK": verification("alpha")}}


def save(value, url):
    return {"op": "SAVE_RESULT", "text": value, "sources": [{"url": url, "quote": value}]}


def finish(value="alpha", url="https://example.test/a"):
    return [save(value, url), {"op": "COMPLETE_TASK"}]


def b_actions():
    return [{"op": "SWITCH_TASK", "task_id": "E1"}, {"op": "GOTO", "url": "https://example.test/b"},
            {"op": "TYPE", "node_id": "0", "value": "beta"}, *finish("beta", "https://example.test/b")]


def run(c, responses, tmp_path, **opts):
    model = Model(responses)
    return asyncio.run(run_episode(c, model, tmp_path, opts, FakeBrowser)), model


@pytest.mark.parametrize("branch", ["IGNORE", "DEFER", "HANDLE", "REPLAN", "TERMINATE"])
def test_five_real_behavior_branches(branch, tmp_path):
    responses = [{"op": "TYPE", "node_id": "0", "value": "alpha"},
                 {"decision": case(branch)["gold"]["decision"], "follow_up": case(branch)["gold"]["follow_up"]}]
    if branch == "IGNORE":
        responses += finish()
    elif branch == "DEFER":
        responses += finish() + b_actions()
    elif branch == "HANDLE":
        responses += b_actions() + [{"op": "SWITCH_TASK", "task_id": "A"}, {"op": "CLICK", "node_id": "1"}] + finish()
    elif branch == "REPLAN":
        responses += [{"op": "ACCEPT_EVENT", "event_id": "E1"}, {"op": "TYPE", "node_id": "0", "value": "gamma"}] + finish("gamma")
    else:
        responses += [{"op": "ACCEPT_EVENT", "event_id": "E1"}]
    responses += [{"op": "ANSWER", "answer": "done"}]
    episode, _ = run(case(branch), responses, tmp_path)
    assert episode["status"] == "agent_finished"
    assert episode["evaluation"]["episode_success"] is True
    assert episode["evaluation"]["branch"] == branch


def test_runner_does_not_execute_reported_schedule_or_force_interrupt(tmp_path):
    c = case("HANDLE")
    responses = [{"op": "TYPE", "node_id": "0", "value": "alpha"},
                 {"decision": "INTERRUPT", "follow_up": "HANDLE"}, *finish(), *b_actions(), {"op": "ANSWER"}]
    ep, _ = run(c, responses, tmp_path)
    assert ep["decisions"]["E1"] == "INTERRUPT"
    assert ep["evaluation"]["episode_success"] is False
    assert ep["evaluation"]["event_results"]["E1"]["timely"] is False
    assert ep["actual_schedule"] == ["CURRENT_TASK", "E1"]


def test_irrelevant_event_is_not_blocked_by_private_gold_and_real_distraction_fails(tmp_path):
    c = case('IGNORE')
    responses = [{'op':'TYPE','node_id':'0','value':'alpha'}, {'decision':'INTERRUPT','follow_up':'HANDLE'},
                 *b_actions(), {'op':'SWITCH_TASK','task_id':'A'}, *finish(), {'op':'ANSWER'}]
    ep, _ = run(c,responses,tmp_path)
    assert ep['evaluation']['task_success'] is True
    assert ep['evaluation']['event_results']['E1']['success'] is True
    assert ep['evaluation']['event_results']['E1']['distraction'] is True
    assert ep['evaluation']['episode_success'] is False


def test_interrupt_resuming_before_event_completed_fails(tmp_path):
    responses = [{"op": "TYPE", "node_id": "0", "value": "alpha"}, {"decision": "INTERRUPT", "follow_up": "HANDLE"},
                 *b_actions()[:3], {"op": "SWITCH_TASK", "task_id": "A"}, *finish(),
                 {"op": "SWITCH_TASK", "task_id": "E1"}, *finish("beta", "https://example.test/b"), {"op": "ANSWER"}]
    ep, _ = run(case("HANDLE"), responses, tmp_path)
    assert ep["evaluation"]["event_results"]["E1"]["timely"] is False


def test_termination_can_replace_original_with_real_event_task(tmp_path):
    c = case("TERMINATE")
    c["event"]["execution"].update(kind="research_task", terminates_original=True)
    c["gold"].update(required_nodes=["TERMINATE_TASK", "E1"], precedence=[["TERMINATE_TASK", "E1"]])
    responses = [{"op": "TYPE", "node_id": "0", "value": "alpha"}, {"decision": "INTERRUPT", "follow_up": "TERMINATE"},
                 {"op": "STOP_TASK", "task_id": "A"}, *b_actions(), {"op": "ANSWER"}]
    ep, _ = run(c, responses, tmp_path)
    assert ep["evaluation"]["episode_success"] is True
    assert ep["evaluation"]["event_results"]["E1"]["success"] is True
    assert ep["evaluation"]["task_completion_results"] == {}


def test_state_pair_performs_b_and_preserves_same_a_checkpoint(tmp_path):
    incomplete = case("HANDLE")
    incomplete.update(case_id="STATE_B", group_type="state")
    complete = copy.deepcopy(incomplete)
    complete.update(case_id="STATE_A", gold={"decision": "IGNORE", "follow_up": None},
                    setup_tasks=[{"id": "PRIOR_B", "goal": "Find beta", "url": "https://example.test/b",
                                  "verification": verification("beta"), "alias_event_id": "E1"}])
    model = Model([{"op": "TYPE", "node_id": "0", "value": "alpha"},
                   {"op": "TYPE", "node_id": "0", "value": "beta"}, *finish("beta", "https://example.test/b")])
    result = asyncio.run(prepare_state_pair([complete, incomplete], model, tmp_path, browser_factory=FakeBrowser))
    assert result["status"] == "checkpoint_ready"
    a, b = result["checkpoints"]["STATE_A"], result["checkpoints"]["STATE_B"]
    assert a["state"]["prior_results"]["E1"]["provenance"] == "verified_actual_browser_execution"
    assert b["state"]["prior_results"] == {}
    x, y = copy.deepcopy(a["state"]), copy.deepcopy(b["state"])
    x.pop("prior_results"); y.pop("prior_results")
    assert x == y
    frozen = asyncio.run(run_frozen(complete, Model([{"decision": "IGNORE"}]), a, "state", tmp_path / "frozen"))
    assert frozen["events"]["E1"]["delivered"] is True


def staggered_case():
    c = case('HANDLE')
    first = copy.deepcopy(c.pop('event')); first['gold'] = {'decision':'INTERRUPT','follow_up':'HANDLE'}
    ignored = {'id':'E2','source':'Public page','text':'Other request', 'execution':{'kind':'none'},
               'gold':{'decision':'IGNORE','follow_up':None}}
    deferred = copy.deepcopy(first); deferred.update(id='E3',text='Additional delta note',delivery_after=['E1'])
    deferred['gold'] = {'decision':'DEFER','follow_up':None}
    deferred['execution'].update(goal='Find delta',url='https://example.test/d',verification=verification('delta'))
    revision = copy.deepcopy(case('REPLAN')['event']); revision.update(id='E4',delivery_after=['E1'])
    revision['gold'] = {'decision':'INTERRUPT','follow_up':'REPLAN'}
    c['events'] = [first,ignored,deferred,revision]
    c['gold'] = {'decisions':{e['id']:e['gold'] for e in c['events']},
                 'required_nodes':['E1','E4','CURRENT_TASK','E3'],
                 'precedence':[['E1','E4'],['E4','CURRENT_TASK'],['CURRENT_TASK','E3']]}
    return c


def test_staggered_updates_preserve_first_decisions_and_allow_legitimate_replan_after_handle(tmp_path):
    c = staggered_case()
    responses = [{'op':'TYPE','node_id':'0','value':'alpha'},
        {'decisions':{'E1':'INTERRUPT','E2':'IGNORE'}, 'follow_ups':{'E1':'HANDLE'},'schedule':['E1','CURRENT_TASK']},
        *b_actions(), {'op':'SWITCH_TASK','task_id':'A'},
        {'decisions':{'E1':'IGNORE','E2':'IGNORE','E3':'DEFER','E4':'INTERRUPT'},
         'follow_ups':{'E4':'REPLAN'},'schedule':['E1','E4','CURRENT_TASK','E3']},
        {'op':'ACCEPT_EVENT','event_id':'E4'}, {'op':'TYPE','node_id':'0','value':'gamma'}, *finish('gamma'),
        {'op':'SWITCH_TASK','task_id':'E3'}, {'op':'GOTO','url':'https://example.test/d'},
        {'op':'TYPE','node_id':'0','value':'delta'}, *finish('delta','https://example.test/d'), {'op':'ANSWER'}]
    ep, _ = run(c,responses,tmp_path)
    assert ep['evaluation']['episode_success'] is True
    assert ep['decisions']['E1'] == 'INTERRUPT' and ep['follow_ups']['E1'] == 'HANDLE'
    assert len(ep['decision_stages']) == 2
    assert ep['decision_stages'][1]['known_event_ids'] == ['E1','E2','E3','E4']
    assert ep['predicted_schedule'] == ep['actual_schedule'] == ['E1','E4','CURRENT_TASK','E3']
    assert ep['evaluation']['recovery_audit']['effective_goal_version_consistent'] is True


def test_unverified_completion_claim_does_not_unlock_later_events(tmp_path):
    c = staggered_case()
    responses = [{'op':'TYPE','node_id':'0','value':'alpha'},
        {'decisions':{'E1':'INTERRUPT','E2':'IGNORE'},'follow_ups':{'E1':'HANDLE'},'schedule':['E1','CURRENT_TASK']},
        {'op':'SWITCH_TASK','task_id':'E1'}, {'op':'COMPLETE_TASK'}, {'op':'ANSWER'}]
    ep, _ = run(c,responses,tmp_path)
    assert ep['verified_tasks']['E1']['result']['success'] is False
    assert ep['events']['E3']['delivered'] is False and ep['events']['E4']['delivered'] is False
    assert ep['evaluation']['episode_success'] is False


def test_trigger_excludes_homepage_and_navigation_only():
    t = {"all": [{"type": "url_path_not_in", "values": ["/", "/home"]}, {"type": "text_contains", "value": "results"}],
         "min_non_navigation_actions": 1}
    obs = {"url": "https://example.test/results", "text": "results"}
    assert not match_trigger(t, obs, [{"action": {"op": "GOTO"}}])
    assert match_trigger(t, obs, [{"action": {"op": "TYPE"}}])
    assert not match_trigger(t, {**obs, "url": "https://example.test/"}, [{"action": {"op": "SCROLL"}}])


def test_prior_b_actions_cannot_substitute_for_original_a_progress():
    t = {'kind':'text_contains','value':'Research','min_non_navigation_actions':1}
    observation = {'url':'https://example.test/a','text':'Research alpha'}
    history = [{'task_id':'PRIOR_B','action':{'op':'TYPE'}},
               {'task_id':'A','action':{'op':'GOTO'}}]
    assert not match_trigger(t,observation,history)
    history.append({'task_id':'A','action':{'op':'TYPE'}})
    assert match_trigger(t,observation,history)


def test_input_projection_never_leaks_private_gold_and_policy_is_identical():
    cp = {"goal": "SECRET_GOAL", "rules": ["COMMON_POLICY"], "state": {"summary": "SECRET_STATE"},
          "trajectory": [{"observation": "SECRET_HISTORY"}], "event": {"id": "E1", "source": "User", "text": "EVENT",
          "gold": "PRIVATE", "execution": {"updated_goal": "PRIVATE_VERIFIER"}}}
    for view in ("event-only", "goal", "state", "trajectory"):
        messages = decision_messages(cp, view)
        assert "COMMON_POLICY" in messages[1]["content"]
        assert "PRIVATE" not in messages[1]["content"]
    assert "SECRET_GOAL" not in decision_messages(cp, "event-only")[1]["content"]
    assert "SECRET_HISTORY" in decision_messages(cp, "trajectory")[1]["content"]


def test_nested_action_and_replay_semantics():
    assert normalize_action('{"action":{"op":"TYPE","node_id":"0","value":"a"}}')["op"] == "TYPE"
    with pytest.raises(ValueError):
        normalize_action('{"action":{"op":"TYPE"},"second_action":{"op":"GOTO"}}')
    a = {"url": "https://example.test/", "text": "same", "candidates": [{"node_id": "1", "id": "dynamic1", "name": "q", "value": "a"}]}
    b = copy.deepcopy(a); b["candidates"][0].update(node_id="2", id="dynamic2")
    assert replay_checkpoint_audit(a, b)["matches"]
    b["candidates"][0]["value"] = "b"
    assert not replay_checkpoint_audit(a, b)["matches"]


def test_lossless_history_keeps_all_steps_without_duplicating_page_bodies():
    state = {'url':'https://example.test/a','text':'Full observed page','candidates':[{'name':'q','value':'alpha'}]}
    ep = {'observations':[{**state,'index':0,'action_index':0,'task_id':'A'},
                          {**state,'index':1,'action_index':1,'task_id':'A'}],
          'actions':[{'index':0,'task_id':'A','action':{'op':'SCROLL'},'before':state,'after':state}]}
    history = lossless_trajectory(ep)
    assert len(history['states']) == 1
    assert len(history['records']) == 3
    assert decode_lossless_trajectory(history) == ep
    assert history['records'][1]['before_state_ref'] == history['records'][1]['after_state_ref']


@pytest.mark.browser
def test_actual_chromium_state_pair_and_termination_replacement(tmp_path):
    from eventarena.browser import Browser
    server = ThreadingHTTPServer(('127.0.0.1',0), LocalFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    a, b = f'http://127.0.0.1:{server.server_port}/a', f'http://127.0.0.1:{server.server_port}/b'
    try:
        incomplete = case('HANDLE'); incomplete.update(case_id='PAIR_A',url=a)
        incomplete['event']['execution']['url'] = b
        complete = copy.deepcopy(incomplete)
        complete.update(case_id='PAIR_B',gold={'decision':'IGNORE','follow_up':None},setup_tasks=[
            {'id':'PRIOR_B','goal':'Find beta','url':b,'alias_event_id':'E1','verification':verification('beta')}])
        model = Model([{'op':'TYPE','node_id':'0','value':'alpha'},
                       {'op':'TYPE','node_id':'0','value':'beta'}, {'op':'CLICK','node_id':'1'}, *finish('beta',b)])
        pair = asyncio.run(prepare_state_pair([incomplete,complete],model,tmp_path/'pair',{'headless':True},Browser))
        assert pair['status'] == 'checkpoint_ready'
        assert pair['restoration_audit']['matches']
        assert pair['checkpoints']['PAIR_B']['state']['prior_results']['E1']['verification']['success'] is True
        c = case('TERMINATE'); c['url'] = a
        c['event']['execution'].update(kind='research_task',url=b,terminates_original=True)
        c['gold'].update(required_nodes=['TERMINATE_TASK','E1'],precedence=[['TERMINATE_TASK','E1']])
        model = Model([{'op':'TYPE','node_id':'0','value':'alpha'}, {'decision':'INTERRUPT','follow_up':'TERMINATE'},
            {'op':'STOP_TASK','task_id':'A'}, {'op':'SWITCH_TASK','task_id':'E1'}, {'op':'GOTO','url':b},
            {'op':'TYPE','node_id':'0','value':'beta'}, {'op':'CLICK','node_id':'1'}, *finish('beta',b), {'op':'ANSWER'}])
        ep = asyncio.run(run_episode(c,model,tmp_path/'terminate',{'headless':True},Browser))
        assert ep['evaluation']['episode_success'] is True
        assert ep['evaluation']['post_cancel_action_count'] == 0
        assert ep['actual_schedule'] == ['TERMINATE_TASK','E1']
        assert (tmp_path/'terminate'/'trace.zip').exists()
    finally:
        server.shutdown(); server.server_close()


class LocalFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'''<html><head><title>Research</title></head><body><h1>Research results</h1>
          <input id="query" name="query"><button onclick="document.getElementById('result').innerText=document.getElementById('query').value">Search</button>
          <div id="result"></div></body></html>'''
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *_):
        pass


@pytest.mark.browser
def test_actual_chromium_autonomous_handle_and_recovery(tmp_path):
    from eventarena.browser import Browser
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    a, b = f"http://127.0.0.1:{server.server_port}/a", f"http://127.0.0.1:{server.server_port}/b"
    c = case("HANDLE"); c["url"] = a; c["event"]["execution"]["url"] = b
    model = Model([{"op": "TYPE", "node_id": "0", "value": "alpha"}, {"decision": "INTERRUPT", "follow_up": "HANDLE"},
                   {"op": "SWITCH_TASK", "task_id": "E1"}, {"op": "GOTO", "url": b},
                   {"op": "TYPE", "node_id": "0", "value": "beta"}, {"op": "CLICK", "node_id": "1"}, *finish("beta", b),
                   {"op": "SWITCH_TASK", "task_id": "A"}, {"op": "CLICK", "node_id": "1"}, *finish("alpha", a), {"op": "ANSWER"}])
    try:
        ep = asyncio.run(run_episode(c, model, tmp_path, {"headless": True}, Browser))
        assert ep["evaluation"]["episode_success"] is True
        assert ep["evaluation"]["recovery_audit"]["same_form_values"] is True
        assert (tmp_path / "trace.zip").is_file()
    finally:
        server.shutdown(); server.server_close()
