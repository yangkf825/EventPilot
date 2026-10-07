"""Minimal provider adapter. Credentials are environment variables only."""
import json
import os
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


class ProviderText(str):
    """Text with credential-free response diagnostics, bound to this call."""
    def __new__(cls, value, diagnostics):
        result = super().__new__(cls, value)
        result.diagnostics = diagnostics
        return result


class Model:
    def __init__(self, name, config=None):
        self.config = json.loads(Path(config or ROOT / 'models.json').read_text())
        matches = [m for m in self.config['models'] if m['name'] == name]
        if len(matches) != 1 or matches[0]['backend'] != 'chat-completions':
            raise ValueError('Choose a configured chat-completions model')
        self.model = matches[0]
        self.usage = []
        self.last_call = 0.0

    def call(self, messages, max_tokens=None):
        key = os.environ.get(self.model['api_key_env'])
        if not key:
            raise RuntimeError(f'Set {self.model["api_key_env"]} in the terminal first')
        body = {'model': self.model['model'], 'messages': messages, 'stream': False}
        for name in ('temperature', 'top_p', 'max_tokens', 'max_completion_tokens'):
            if self.model.get(name) is not None:
                body[name] = self.model[name]
        if max_tokens is not None:
            field = 'max_completion_tokens' if 'max_completion_tokens' in body else 'max_tokens'
            body[field] = max_tokens
        elif 'max_tokens' not in body and 'max_completion_tokens' not in body:
            body['max_tokens'] = 4096
        body.update(self.model.get('extra_body', {}))
        wait = self.model.get('min_interval_s', 0) - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        retries = int(self.model.get('retries', self.config.get('retries', 2)))
        ca_file = os.environ.get('EVENTARENA_CA_BUNDLE')
        if not ca_file and Path('/etc/ssl/cert.pem').is_file():
            ca_file = '/etc/ssl/cert.pem'
        verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
        for attempt in range(retries + 1):
            self.last_call = time.monotonic()
            try:
                with httpx.Client(timeout=self.model.get('timeout', self.config.get('timeout', 120)), verify=verify) as client:
                    response = client.post(self.model['endpoint'], json=body,
                                           headers={'Authorization': f'Bearer {key}'})
                if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    retry_after = response.headers.get('Retry-After')
                    delay = min(8, 2 ** attempt)
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            try:
                                deadline = parsedate_to_datetime(retry_after)
                                if deadline.tzinfo is None:
                                    deadline = deadline.replace(tzinfo=timezone.utc)
                                delay = max(delay, (deadline-datetime.now(timezone.utc)).total_seconds())
                            except (TypeError, ValueError, OverflowError):
                                pass
                    maximum = float(self.model.get('max_retry_after_s', self.config.get('max_retry_after_s', 60)))
                    if delay > maximum:
                        raise RuntimeError('Provider retry delay exceeds configured limit')
                    time.sleep(max(0, delay))
                    continue
                if response.is_error:
                    # Never include request headers or bearer credentials in errors/logs.
                    raise RuntimeError(f'Provider HTTP {response.status_code}; inspect provider availability/configuration')
                data = response.json()
                if not data.get('choices'):
                    raise RuntimeError('Provider returned no choices')
                choice = data['choices'][0]
                message = choice.get('message', {})
                content = message.get('content') or ''
                if isinstance(content, list):
                    content = ''.join(part.get('text', '') for part in content if isinstance(part, dict))
                if not isinstance(content, str):
                    raise RuntimeError('Provider returned nontext content')
                reasoning = message.get('reasoning_content', message.get('reasoning', ''))
                diagnostics = {'finish_reason': choice.get('finish_reason'),
                               'content_chars': len(content),
                               'reasoning_chars': len(reasoning) if isinstance(reasoning, str) else None,
                               'message_fields': sorted(message),
                               'requested_output_tokens': body.get('max_completion_tokens', body.get('max_tokens')),
                               'thinking_requested': body.get('thinking'),
                               'empty_content': not bool(content.strip())}
                self.usage.append({'model': data.get('model'), 'id': data.get('id'),
                                   'usage': data.get('usage'), **diagnostics})
                return ProviderText(content, diagnostics)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == retries:
                    raise RuntimeError('Provider transport failure; not a model decision score') from None
                time.sleep(2 ** attempt)


def object_output(text):
    raw = text.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Expected one JSON object')
    return value
