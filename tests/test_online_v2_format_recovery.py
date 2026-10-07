import asyncio
import json
import pytest

from eventarena.json_protocol import extract_object, JSONEnvelopeError
from eventarena.online_v2_engine import normalize_action, run_episode
from test_online_v2_engine import case, FakeBrowser, Model, finish, run


@pytest.mark.parametrize('raw',[
    'I will scroll.\n{"op":"SCROLL","pixels":100}',
    '{"op":"SCROLL","pixels":100}\nReason: inspect the page.',
    '```json\n{"op":"SCROLL","pixels":100}\n```',
])
def test_unique_json_with_prose_keeps_the_exact_action(raw):
    assert normalize_action(raw)=={'op':'SCROLL','pixels':100}


def test_nested_sources_and_braces_in_strings_are_not_extra_objects():
    value={'op':'SAVE_RESULT','text':'A literal {value} and [example]',
           'sources':[{'url':'https://example.test/a','quote':'alpha'}]}
    assert extract_object('Here is the result:\n'+json.dumps(value))==value


@pytest.mark.parametrize('raw',[
    '{"op":"BACK"} {"op":"BACK"}',
    '[{"op":"BACK"}]',
    '{broken outer: {"op":"BACK"}}',
    '{broken outer: {"op":"BACK"}',
    '{"op":"BACK"} }',
    '{"op":"BACK","op":"GOTO"}',
    '{"op":"SCROLL","pixels":NaN}',
    '{"op":"SCROLL","pixels":Infinity}',
    '{"op":"SCROLL","pixels":1e999}',
    'GOTO: https://example.test/a',
])
def test_ambiguous_or_invalid_json_is_never_guessed(raw):
    with pytest.raises(JSONEnvelopeError):
        extract_object(raw)


def test_format_retry_uses_same_observation_and_executes_only_once(tmp_path):
    c=case('IGNORE')
    responses=['TYPE: query = alpha', {'op':'TYPE','node_id':'0','value':'alpha'},
               {'decision':'IGNORE'},*finish(),{'op':'ANSWER'}]
    ep,model=run(c,responses,tmp_path)
    assert ep['evaluation']['episode_success'] is True
    assert ep['format_retry_count']==1
    assert sum(a['action']['op']=='TYPE' for a in ep['actions'])==1
    assert model.messages[0][:2]==model.messages[1][:2]
    assert ep['format_diagnostics'][0]=={'action_index':1,'first_pass_valid':False,'retry_count':1,'final_valid':True}
    retry=next(r for r in ep['responses'] if r['stage']=='actor_format_retry')
    assert retry['format_attempt']==1 and retry['retry_reason']=='json_envelope_invalid'


def test_two_invalid_formats_remain_agent_failure(tmp_path):
    ep,_=run(case(),['GOTO: invalid','Still no JSON'],tmp_path)
    assert ep['status']=='invalid_output' and ep['format_retry_count']==1
    assert len(ep['actions'])==1
    assert ep['evaluation']['episode_success'] is False


def test_unknown_operation_is_not_given_format_retry(tmp_path):
    ep,_=run(case(),[{'op':'INVENTED_TOOL'}],tmp_path)
    assert ep['status']=='invalid_output' and ep['format_retry_count']==0


def test_current_observation_reference_resolves_the_complete_actual_page(tmp_path):
    ep,model=run(case(),[{'op':'ANSWER'}],tmp_path)
    payload=json.loads(model.messages[0][-1]['content'])
    history=payload['full_trajectory']
    mapping=history['states'][payload['observation']['state_ref']]
    from eventarena.online_v2_engine import decode_lossless_trajectory
    resolved=next(o for o in decode_lossless_trajectory(history)['observations'] if o.get('index')==payload['observation']['observation_index'])
    actual=ep['observations'][0]
    assert resolved['text']==actual['text']
    assert resolved['candidates']==actual['candidates']


def test_semantic_tool_error_is_not_a_format_retry(tmp_path):
    ep,_=run(case(),[{'op':'SWITCH_TASK','task_id':'UNDELIVERED'},{'op':'ANSWER'}],tmp_path)
    assert ep['format_retry_count']==0 and ep['actions'][1]['error'] is not None


class MissingPageBrowser(FakeBrowser):
    async def observe(self):
        result=await super().observe()
        result['http_status']=404 if self.page.url.endswith('/missing') else 200
        return result

    async def execute(self,action,phase=None):
        if action['op']=='BACK':
            self.page.url='https://example.test/a'
        await super().execute(action,phase)


def test_actor_missing_link_can_recover_without_being_excluded(tmp_path):
    c=case(); c.pop('event'); c['events']=[]; c['gold']={}
    model=Model([{'op':'GOTO','url':'https://example.test/missing'},{'op':'BACK'},
                 {'op':'TYPE','node_id':'0','value':'alpha'},*finish(),{'op':'ANSWER'}])
    ep=asyncio.run(run_episode(c,model,tmp_path,browser_factory=MissingPageBrowser))
    assert ep['status']=='agent_finished'
    assert ep['evaluation']['episode_success'] is True
    assert ep['navigation_errors'][0]['category']=='actor_navigation_missing_page'


def test_registered_entry_404_remains_website_unavailable(tmp_path):
    c=case(); c['url']='https://example.test/missing'
    ep=asyncio.run(run_episode(c,Model([]),tmp_path,browser_factory=MissingPageBrowser))
    assert ep['status']=='website_unavailable'
    assert ep['evaluation']['episode_success'] is None
