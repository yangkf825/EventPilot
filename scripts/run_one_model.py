#!/usr/bin/env python3
"""Run Online v2.0.5 for one exact model ID, with credentials kept in the environment.

Example: python scripts/run_one_model.py --provider deepseek --model deepseek-v4-pro
Use --dry-run to check registration without requesting a key or calling an API.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = {
    'deepseek': 'gateway_ds', 'glm': 'gateway_glm',
    'kimi': 'gateway_kimi', 'gpt': 'gateway_gpt',
}
PILOT_CASES = 'ON2_S_026,ON2_S_027,ON2_S_028,ON2_S_029,ON2_S_034'
PILOT_CF_GROUPS = 'G07,T07,P07'
PILOT_MULTI_CASES = 'ON2_M_06,ON2_M_13'
INTERRUPT_TIMEOUT_SECONDS = 15
_CREDENTIAL_FIELDS = {
    'apikey', 'key', 'token', 'secret', 'password', 'authorization',
    'accesstoken', 'refreshtoken', 'clientsecret', 'privatekey',
    'credential', 'credentials', 'cookie', 'cookies', 'xapikey', 'authtoken',
    'apitoken', 'bearertoken', 'xauthtoken', 'accesskey', 'secretkey',
    'auth', 'authentication',
}
_LITERAL_SECRET = re.compile(r'(?i)\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}')


def _field_name(value):
    return re.sub(r'[^a-z0-9]', '', str(value).casefold())


def reject_credentials(value):
    """Reject credential fields and recognizable literal keys without echoing values."""
    if isinstance(value, dict):
        for key, child in value.items():
            if _field_name(key) in _CREDENTIAL_FIELDS:
                raise ValueError('Credentials must be environment variables; remove credential fields from the config')
            reject_credentials(child)
    elif isinstance(value, list):
        for child in value:
            reject_credentials(child)
    elif isinstance(value, str) and _LITERAL_SECRET.search(value):
        raise ValueError('Literal credentials are not permitted in model IDs or configuration')


def one_model_config(config, alias, model_id):
    """Copy global settings and the selected provider; never change the input config."""
    if not isinstance(config, dict) or not isinstance(config.get('models'), list):
        raise ValueError('Config must contain a models list')
    reject_credentials(config)
    reject_credentials(model_id)
    if not model_id.strip() or any(ord(char) < 32 or ord(char) == 127 for char in model_id):
        raise ValueError('--model must be a nonempty exact model ID without control characters')
    selected = [entry for entry in config['models']
                if isinstance(entry, dict) and entry.get('name') == alias]
    if len(selected) != 1:
        raise ValueError('Config must contain exactly one entry for the selected provider alias')
    entry = copy.deepcopy(selected[0])
    if entry.get('backend') != 'chat-completions':
        raise ValueError('Selected provider must use the chat-completions backend')
    env_name = entry.get('api_key_env')
    if not isinstance(env_name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env_name):
        raise ValueError('Selected api_key_env must be an environment variable name')
    endpoint = entry.get('endpoint')
    if not isinstance(endpoint, str):
        raise ValueError('Selected provider requires an HTTP(S) endpoint')
    try:
        url = urlsplit(endpoint)
        valid_endpoint = (url.scheme in ('https', 'http') and url.hostname
                          and url.username is None and url.password is None)
    except ValueError:
        valid_endpoint = False
    if not valid_endpoint:
        raise ValueError('Endpoint must be a valid HTTP(S) URL without credentials')
    if url.scheme == 'http' and entry.get('allow_http') is not True:
        raise ValueError('HTTP endpoint requires explicit allow_http=true')
    if any(_field_name(key) in _CREDENTIAL_FIELDS for key, _ in parse_qsl(url.query)):
        raise ValueError('Endpoint credentials must be environment variables')
    entry['model'] = model_id
    entry['enabled'] = True
    # Providers merge extra_body after the standard request. Keep a duplicate
    # model field consistent with the exact CLI selection if one was configured.
    if 'extra_body' in entry:
        if not isinstance(entry['extra_body'], dict):
            raise ValueError('extra_body must be a JSON object')
        if 'model' in entry['extra_body']:
            entry['extra_body']['model'] = model_id
    result = copy.deepcopy(config)
    result['models'] = [entry]
    return result


def default_run_root(model_id):
    safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', model_id).strip('._-')[:80] or 'model'
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    return ROOT / 'runs' / f'{safe_name}_v2_0_5_once_{stamp}'


def load_json(path, description):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError(f'Cannot read {description}: {path}') from error


def persist_config(run_root, config, resume=False):
    """Only logs may exist before registration; never replace a saved config."""
    path = run_root / 'logs' / 'model_config.json'
    manifest = run_root / 'manifest.json'
    if manifest.exists() and not resume:
        raise ValueError('Existing registered run; use --resume or a new --run-root')
    if manifest.exists() and not path.is_file():
        raise ValueError('Registered run has no launcher config; use its original command or a new --run-root')
    if run_root.exists() and not manifest.exists() and any(p.name != 'logs' for p in run_root.iterdir()):
        raise ValueError('Nonempty run directory without a registered manifest; use a new --run-root')
    if path.exists():
        if load_json(path, 'saved launcher config') != config:
            raise ValueError('Saved model configuration differs; keep the original config and model ID or use a new --run-root')
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents an accidental overwrite by a concurrent launch.
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    except FileExistsError:
        if load_json(path, 'saved launcher config') != config:
            raise ValueError('Saved model configuration differs; use a new --run-root') from None
    return path


def runner_argv(args, alias, config_path, run_root):
    command = [sys.executable, str(ROOT / 'scripts' / 'run_online_v2.py'), 'run',
               '--config', str(config_path), '--only', alias,
               '--run-root', str(run_root), '--experiments', 'all',
               '--repeats', str(args.repeats), '--workers', str(args.workers),
               '--preparation-mode', 'before-actors', '--prep-model', alias,
               '--judge-model', alias]
    if not args.headed:
        command.append('--headless')
    if args.mode == 'pilot':
        command.extend(['--case-ids', PILOT_CASES, '--cf-group-ids', PILOT_CF_GROUPS,
                        '--multi-case-ids', PILOT_MULTI_CASES])
    if args.resume:
        command.append('--resume')
    if args.retry_infra:
        command.append('--retry-infra')
    if args.dry_run:
        command.append('--dry-run')
    return command


def interrupt_child(child):
    """Interrupt the child process group, then enforce a bounded wait and reap it."""
    if child.poll() is not None:
        return
    try:
        if os.name == 'posix':
            os.killpg(child.pid, signal.SIGINT)
        else:
            child.send_signal(getattr(signal, 'CTRL_BREAK_EVENT', signal.SIGINT))
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=INTERRUPT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            if os.name == 'posix':
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
        except ProcessLookupError:
            pass
        child.wait()


def stream_command(command, env, log_path):
    """Execute argument lists directly and tee output line by line."""
    process_options = {'start_new_session': True} if os.name == 'posix' else {
        'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    with log_path.open('a', encoding='utf-8') as log:
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1,
                                 **process_options)
        try:
            for line in child.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
                log.flush()
            return child.wait()
        except KeyboardInterrupt:
            interrupt_child(child)
            print('Interrupted; child process stopped.', file=sys.stderr, flush=True)
            return 130
        except BaseException:
            interrupt_child(child)
            raise
        finally:
            child.stdout.close()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--provider', choices=tuple(PROVIDERS), required=True)
    result.add_argument('--model', required=True, help='Exact model ID accepted by your configured endpoint')
    result.add_argument('--config', type=Path, default=ROOT / 'models.gateway.json',
                        help='Existing provider config; endpoint and request parameters are retained')
    result.add_argument('--run-root', type=Path, help='Result directory (default: timestamped runs/<model>_v2_0_5_once_...)')
    result.add_argument('--mode', choices=('full', 'pilot'), default='full')
    result.add_argument('--repeats', type=int, default=1)
    result.add_argument('--workers', type=int, default=2)
    result.add_argument('--resume', action='store_true')
    result.add_argument('--retry-infra', action='store_true',
                        help='With --resume only: retry infrastructure/unverified jobs; keep scored Agent results')
    result.add_argument('--dry-run', action='store_true', help='No key prompt, connectivity probe, browser or API calls')
    result.add_argument('--skip-probe', action='store_true', help='Skip the connectivity API request before a real run')
    result.add_argument('--headed', action='store_true', help='Show browser windows (default: headless)')
    return result


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if min(args.repeats, args.workers) < 1:
            raise ValueError('--repeats and --workers must be positive integers')
        if args.resume and args.run_root is None:
            raise ValueError('--resume requires the original --run-root')
        if args.retry_infra and not args.resume:
            raise ValueError('--retry-infra requires --resume')
        alias = PROVIDERS[args.provider]
        config = one_model_config(load_json(args.config.expanduser().resolve(), 'provider config'), alias, args.model)
        run_root = (args.run_root or default_run_root(args.model)).expanduser().resolve()
        config_path = persist_config(run_root, config, resume=args.resume)
        command = runner_argv(args, alias, config_path, run_root)
        env_name = config['models'][0]['api_key_env']
        print(f'Model ID: {args.model}\nProvider alias: {alias}\nAPI key environment: {env_name}\nRun directory: {run_root}', flush=True)
        env = os.environ.copy()
        if not args.dry_run:
            if not env.get(env_name):
                env[env_name] = getpass.getpass(f'{env_name} (hidden): ')
                if not env[env_name]:
                    raise ValueError(f'A nonempty {env_name} is required')
            if not args.skip_probe:
                probe = [sys.executable, str(ROOT / 'scripts' / 'probe_gateway.py'),
                         '--config', str(config_path), '--only', alias, '--timeout', '60',
                         '--output', str(run_root / 'logs' / 'model_probe.json')]
                result = stream_command(probe, env, run_root / 'logs' / 'probe.log')
                if result:
                    return result
        return stream_command(command, env, run_root / 'logs' / 'run.log')
    except (ValueError, OSError) as error:
        cli.error(str(error))
    except (KeyboardInterrupt, EOFError):
        print('Interrupted before starting the experiment.', file=sys.stderr, flush=True)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
