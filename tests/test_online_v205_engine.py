"""The common checkpoint is eligible only after replay cleanup is audited."""
import asyncio
from types import SimpleNamespace

import pytest

from eventarena import online_v2_engine as engine_module


@pytest.mark.parametrize('changed', [False, True])
def test_restore_audits_the_actual_state_after_har_cleanup(monkeypatch, changed):
    calls = []
    before = {'url': 'https://example.test/a', 'title': 'Research',
              'text': 'Original task evidence', 'candidates': []}
    after = {**before, 'text': 'Late response changed task evidence' if changed else before['text']}
    checkpoint = {'state': {'url': before['url'], 'summary': before['text'], 'candidates': []},
                  'environment_capture': {'mode': 'recorded_prefix_then_live'}}

    class Browser:
        async def begin_checkpoint_replay(self, capture):
            calls.append('begin')

        async def end_checkpoint_replay(self):
            calls.append('cleanup')

    async def observe(browser):
        calls.append('post_cleanup_observation')
        return after

    obj = SimpleNamespace(case={}, episode={}, observe=observe)

    async def prefix(*args):
        calls.append('prefix')
        return {'matches': True}, before

    monkeypatch.setattr(engine_module, '_restore_checkpoint_prefix', prefix)
    audit, observation = asyncio.run(engine_module.restore_checkpoint(obj, Browser(), checkpoint))
    assert calls == ['begin', 'prefix', 'cleanup', 'post_cleanup_observation']
    assert audit['matches'] is (not changed)
    assert observation == after
    assert obj.episode['checkpoint_replay_cleanup_verified'] is (not changed)
    assert obj.episode['checkpoint_replay_audit_before_cleanup']['matches'] is True


def test_cleanup_error_does_not_return_a_successful_prefix_audit(monkeypatch):
    class Browser:
        async def begin_checkpoint_replay(self, capture):
            pass

        async def end_checkpoint_replay(self):
            raise TimeoutError('Replay cleanup failed')

    async def prefix(*args):
        return {'matches': True}, {}

    monkeypatch.setattr(engine_module, '_restore_checkpoint_prefix', prefix)
    obj = SimpleNamespace(case={}, episode={})
    with pytest.raises(TimeoutError, match='Replay cleanup failed'):
        asyncio.run(engine_module.restore_checkpoint(obj, Browser(), {'environment_capture': {}}))
    assert obj.episode['checkpoint_replay_cleanup_verified'] is False
    assert obj.episode['checkpoint_replay_audit']['available'] is False
    assert obj.episode['checkpoint_replay_audit']['matches'] is None
