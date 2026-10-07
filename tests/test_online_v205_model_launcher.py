"""Exact model launch configuration and process behavior; no provider API calls."""
import copy
import io
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import run_one_model as launcher


def provider_config():
    return {'schema_version': 1, 'timeout': 180, 'retries': 3, 'max_retry_after_s': 45,
            'models': [
                {'name': alias, 'model': 'original-model', 'backend': 'chat-completions',
                 'enabled': False, 'endpoint': 'https://example.invalid/v1/chat/completions',
                 'api_key_env': 'LAUNCHER_TEST_' + provider.upper(), 'timeout': 140,
                 'max_completion_tokens': 16384, 'temperature': 0.5,
                 'min_interval_s': 1, 'extra_body': {'thinking': {'type': 'disabled'}}}
                for provider, alias in launcher.PROVIDERS.items()]}


def write_config(tmp_path, config=None):
    path = tmp_path / 'providers.json'
    path.write_text(json.dumps(config or provider_config()), encoding='utf-8')
    return path


class FakeProcess:
    def __init__(self, output='registered_jobs: 700\n', returncode=0):
        self.stdout = io.StringIO(output)
        self.returncode = returncode
        self.pid = 12345

    def wait(self, timeout=None):
        return self.returncode


def capture_processes(monkeypatch, outputs=None):
    calls = []
    results = iter(outputs or [('registered_jobs: 700\n', 0)])

    def popen(command, **options):
        calls.append((command, options))
        output, code = next(results)
        return FakeProcess(output, code)

    monkeypatch.setattr(launcher.subprocess, 'Popen', popen)
    return calls


@pytest.mark.parametrize('provider,model_id', [
    ('deepseek', 'deepseek-v4-pro'), ('glm', 'glm-5.3'),
    ('kimi', 'k3-256k'), ('gpt', 'gpt-5.4'),
])
def test_provider_override_preserves_budgets_and_base_config(provider, model_id):
    base = provider_config()
    before = copy.deepcopy(base)
    alias = launcher.PROVIDERS[provider]
    selected = launcher.one_model_config(base, alias, model_id)
    assert base == before
    assert {key: value for key, value in selected.items() if key != 'models'} == {
        key: value for key, value in before.items() if key != 'models'}
    expected = next(entry for entry in before['models'] if entry['name'] == alias)
    assert selected['models'] == [expected | {'model': model_id, 'enabled': True}]
    assert selected['models'][0]['api_key_env'] == 'LAUNCHER_TEST_' + provider.upper()


def test_extra_body_model_cannot_override_exact_cli_selection():
    base = provider_config()
    base['models'][0]['extra_body']['model'] = 'old-request-override'
    selected = launcher.one_model_config(base, 'gateway_ds', 'deepseek-v4-pro')
    assert selected['models'][0]['extra_body']['model'] == 'deepseek-v4-pro'
    assert selected['models'][0]['extra_body']['thinking'] == {'type': 'disabled'}
    assert base['models'][0]['extra_body']['model'] == 'old-request-override'


@pytest.mark.parametrize('provider,model_id', [
    ('deepseek', 'deepseek-v4-pro'), ('glm', 'glm-5.3'),
    ('kimi', 'k3-256k'), ('gpt', 'gpt-5.4'),
])
def test_dry_run_never_prompts_or_probes_and_only_creates_logs(monkeypatch, tmp_path, capsys, provider, model_id):
    base_path = write_config(tmp_path)
    original = base_path.read_bytes()
    for name in launcher.PROVIDERS:
        monkeypatch.delenv('LAUNCHER_TEST_' + name.upper(), raising=False)
    monkeypatch.setattr(launcher.getpass, 'getpass', lambda *_: pytest.fail('dry-run must never ask for a key'))
    calls = capture_processes(monkeypatch)
    run_root = tmp_path / 'run'
    result = launcher.main(['--provider', provider, '--model', model_id,
                            '--config', str(base_path), '--run-root', str(run_root), '--dry-run'])
    assert result == 0 and len(calls) == 1
    command, options = calls[0]
    alias = launcher.PROVIDERS[provider]
    assert command[:3] == [sys.executable, str(ROOT / 'scripts' / 'run_online_v2.py'), 'run']
    for flag, expected in [('--only', alias), ('--experiments', 'all'), ('--repeats', '1'),
                           ('--workers', '2'), ('--prep-model', alias), ('--judge-model', alias),
                           ('--preparation-mode', 'before-actors')]:
        assert command[command.index(flag) + 1] == expected
    assert '--dry-run' in command and '--headless' in command and '--resume' not in command
    assert 'shell' not in options and options['cwd'] == ROOT
    assert {path.name for path in run_root.iterdir()} == {'logs'}
    assert json.loads((run_root / 'logs' / 'model_config.json').read_text())['models'][0]['model'] == model_id
    assert (run_root / 'logs' / 'run.log').read_text() == 'registered_jobs: 700\n'
    assert base_path.read_bytes() == original
    assert model_id in capsys.readouterr().out


def test_real_launch_prompts_only_selected_key_probes_then_streams_runner(monkeypatch, tmp_path, capsys):
    base_path = write_config(tmp_path)
    monkeypatch.delenv('LAUNCHER_TEST_KIMI', raising=False)
    prompts = []

    def fake_getpass(prompt):
        prompts.append(prompt)
        return 'FAKE_TEST_VALUE_NOT_A_REAL_CREDENTIAL'

    monkeypatch.setattr(launcher.getpass, 'getpass', fake_getpass)
    calls = capture_processes(monkeypatch, [('probe succeeded\n', 0), ('first\nsecond\n', 0)])
    run_root = tmp_path / 'run'
    assert launcher.main(['--provider', 'kimi', '--model', 'k3-256k', '--config', str(base_path),
                          '--run-root', str(run_root), '--mode', 'pilot', '--headed',
                          '--repeats', '3', '--workers', '4']) == 0
    assert len(prompts) == 1 and 'LAUNCHER_TEST_KIMI' in prompts[0]
    probe, probe_options = calls[0]
    assert probe[1] == str(ROOT / 'scripts' / 'probe_gateway.py')
    assert probe[probe.index('--timeout') + 1] == '60'
    assert probe[probe.index('--output') + 1] == str(run_root / 'logs' / 'model_probe.json')
    command, options = calls[1]
    assert '--headless' not in command
    assert command[command.index('--case-ids') + 1] == launcher.PILOT_CASES
    assert command[command.index('--cf-group-ids') + 1] == launcher.PILOT_CF_GROUPS
    assert command[command.index('--multi-case-ids') + 1] == launcher.PILOT_MULTI_CASES
    assert command[command.index('--repeats') + 1] == '3'
    assert command[command.index('--workers') + 1] == '4'
    assert options['env']['LAUNCHER_TEST_KIMI'] == probe_options['env']['LAUNCHER_TEST_KIMI']
    assert (run_root / 'logs' / 'run.log').read_text() == 'first\nsecond\n'
    assert 'FAKE_TEST_VALUE_NOT_A_REAL_CREDENTIAL' not in capsys.readouterr().out


def test_probe_failure_does_not_start_runner(monkeypatch, tmp_path):
    monkeypatch.setenv('LAUNCHER_TEST_GPT', 'FAKE_TEST_VALUE_NOT_A_REAL_CREDENTIAL')
    calls = capture_processes(monkeypatch, [('provider unavailable\n', 1)])
    assert launcher.main(['--provider', 'gpt', '--model', 'gpt-5.4', '--config', str(write_config(tmp_path)),
                          '--run-root', str(tmp_path / 'run')]) == 1
    assert len(calls) == 1 and Path(calls[0][0][1]).name == 'probe_gateway.py'


def test_skip_probe_uses_existing_environment_without_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv('LAUNCHER_TEST_GLM', 'FAKE_TEST_VALUE_NOT_A_REAL_CREDENTIAL')
    monkeypatch.setattr(launcher.getpass, 'getpass', lambda *_: pytest.fail('Existing environment key must be reused'))
    calls = capture_processes(monkeypatch)
    assert launcher.main(['--provider', 'glm', '--model', 'glm-5.3', '--config', str(write_config(tmp_path)),
                          '--run-root', str(tmp_path / 'run'), '--skip-probe']) == 0
    assert len(calls) == 1 and Path(calls[0][0][1]).name == 'run_online_v2.py'


def test_saved_config_is_stable_on_resume_and_model_change_is_rejected(tmp_path):
    config = launcher.one_model_config(provider_config(), 'gateway_ds', 'deepseek-v4-pro')
    run_root = tmp_path / 'run'
    saved = launcher.persist_config(run_root, config)
    original, original_mtime = saved.read_bytes(), saved.stat().st_mtime_ns
    manifest = run_root / 'manifest.json'
    manifest.write_text('{"registered": true}\n')
    assert launcher.persist_config(run_root, config, resume=True) == saved
    assert saved.stat().st_mtime_ns == original_mtime
    with pytest.raises(ValueError, match='--resume'):
        launcher.persist_config(run_root, config)
    different = launcher.one_model_config(provider_config(), 'gateway_ds', 'another-model')
    with pytest.raises(ValueError, match='differs'):
        launcher.persist_config(run_root, different, resume=True)
    different = copy.deepcopy(config) | {'timeout': 181}
    with pytest.raises(ValueError, match='differs'):
        launcher.persist_config(run_root, different, resume=True)
    assert saved.read_bytes() == original and manifest.read_text() == '{"registered": true}\n'


def test_config_mismatch_blocks_prompt_and_subprocess(monkeypatch, tmp_path):
    base_path = write_config(tmp_path)
    run_root = tmp_path / 'run'
    launcher.persist_config(run_root, launcher.one_model_config(provider_config(), 'gateway_ds', 'old-model'))
    monkeypatch.setattr(launcher.getpass, 'getpass', lambda *_: pytest.fail('Must reject before asking for credentials'))
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *_, **__: pytest.fail('Must reject before any API subprocess'))
    with pytest.raises(SystemExit) as caught:
        launcher.main(['--provider', 'deepseek', '--model', 'new-model', '--config', str(base_path),
                       '--run-root', str(run_root), '--resume'])
    assert caught.value.code == 2


def test_registered_root_without_generated_config_and_nonempty_root_are_preserved(tmp_path):
    config = launcher.one_model_config(provider_config(), 'gateway_ds', 'deepseek-v4-pro')
    run_root = tmp_path / 'run'
    run_root.mkdir()
    manifest = run_root / 'manifest.json'
    manifest.write_text('{}')
    with pytest.raises(ValueError, match='no launcher config'):
        launcher.persist_config(run_root, config, resume=True)
    assert not (run_root / 'logs').exists()
    manifest.unlink()
    unknown = run_root / 'unregistered-result.json'
    unknown.write_text('{}')
    with pytest.raises(ValueError, match='Nonempty'):
        launcher.persist_config(run_root, config)
    assert unknown.read_text() == '{}'


@pytest.mark.parametrize('mutate', [
    lambda config: config.update(api_key='FAKE_TEST_VALUE'),
    lambda config: config['models'][0].update(password='FAKE_TEST_VALUE'),
    lambda config: config['models'][0].update(bearer_token='FAKE_TEST_VALUE'),
    lambda config: config['models'][0]['extra_body'].update(headers={'Authorization': 'FAKE_TEST_VALUE'}),
    lambda config: config['models'][0].update(endpoint='https://user:FAKE_TEST_VALUE@example.invalid/v1'),
    lambda config: config['models'][0].update(endpoint='https://example.invalid/v1?api_key=FAKE_TEST_VALUE'),
    lambda config: config['models'][0].update(api_key_env='invalid environment name'),
])
def test_credentials_and_invalid_environment_names_are_rejected_without_echo(mutate):
    config = provider_config()
    mutate(config)
    with pytest.raises(ValueError) as error:
        launcher.one_model_config(config, 'gateway_ds', 'deepseek-v4-pro')
    assert 'FAKE_TEST_VALUE' not in str(error.value)


def test_shell_metacharacters_are_literal_model_data_and_safe_in_default_path(monkeypatch, tmp_path):
    model_id = '../../vendor/model;$(do_not_execute) `do_not_execute`'
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    selected = launcher.one_model_config(provider_config(), 'gateway_ds', model_id)
    assert selected['models'][0]['model'] == model_id
    folder = launcher.default_run_root(model_id)
    assert folder.parent == tmp_path / 'runs'
    assert not any(char in folder.name for char in ('/', ';', '$', '`', ' '))


def test_literal_bearer_value_is_rejected_without_echo():
    config = provider_config()
    config['models'][0]['extra_body']['unexpected_field'] = 'Bearer FAKE_TEST_VALUE'
    with pytest.raises(ValueError, match='Literal credentials') as error:
        launcher.one_model_config(config, 'gateway_ds', 'deepseek-v4-pro')
    assert 'FAKE_TEST_VALUE' not in str(error.value)


def test_log_write_failure_interrupts_and_reaps_child(monkeypatch, tmp_path):
    class FailedLog(io.StringIO):
        def write(self, text):
            raise OSError('fixture disk write failure')

    child = FakeProcess()
    child.poll = lambda: None
    waits, signals = [], []
    child.wait = lambda timeout=None: waits.append(timeout) or 0
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *_, **__: child)
    monkeypatch.setattr(launcher.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(Path, 'open', lambda *_, **__: FailedLog())
    with pytest.raises(OSError, match='fixture disk write failure'):
        launcher.stream_command(['python', 'fake.py'], {}, tmp_path / 'run.log')
    assert signals == [(child.pid, signal.SIGINT)]
    assert waits == [launcher.INTERRUPT_TIMEOUT_SECONDS] and child.stdout.closed


def test_ctrl_c_interrupts_child_group_then_reaps(monkeypatch, tmp_path):
    class InterruptingOutput:
        closed = False

        def __iter__(self):
            raise KeyboardInterrupt

        def close(self):
            self.closed = True

    child = FakeProcess()
    child.stdout = InterruptingOutput()
    child.poll = lambda: None
    waits = []
    child.wait = lambda timeout=None: waits.append(timeout) or 0
    signals = []
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *_, **__: child)
    monkeypatch.setattr(launcher.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    assert launcher.stream_command(['python', 'fake.py'], {}, tmp_path / 'run.log') == 130
    assert signals == [(child.pid, signal.SIGINT)]
    assert waits == [launcher.INTERRUPT_TIMEOUT_SECONDS] and child.stdout.closed


def test_unresponsive_child_is_killed_and_reaped(monkeypatch):
    child = FakeProcess()
    child.poll = lambda: None
    waits, signals = [], []

    def wait(timeout=None):
        waits.append(timeout)
        if timeout is not None:
            raise subprocess.TimeoutExpired(['fake.py'], timeout)
        return 0

    child.wait = wait
    monkeypatch.setattr(launcher.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    launcher.interrupt_child(child)
    assert signals == [(child.pid, signal.SIGINT), (child.pid, signal.SIGKILL)]
    assert waits == [launcher.INTERRUPT_TIMEOUT_SECONDS, None]
