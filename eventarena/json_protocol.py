"""Parse one unambiguous JSON object without guessing or editing its meaning."""
from __future__ import annotations
import json
import math


class JSONEnvelopeError(ValueError):
    pass


def _unique_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise JSONEnvelopeError('Duplicate JSON key')
        value[key] = item
    return value


def _finite(value):
    raise JSONEnvelopeError('Non-finite JSON value')


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise JSONEnvelopeError('Non-finite JSON number')
    return number


def extract_object(raw):
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise JSONEnvelopeError('Expected a JSON object reply')
    spans, start, stack, quoted, escaped = [], None, [], False, False
    for i, char in enumerate(raw):
        if start is None:
            if char in '{[':
                start, stack, quoted, escaped = i, [char], False, False
            elif char in '}]':
                raise JSONEnvelopeError('Unmatched outer JSON delimiter')
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in '{[':
            stack.append(char)
        elif char in '}]':
            if not stack or (stack[-1], char) not in {('{','}'),('[',']')}:
                raise JSONEnvelopeError('Mismatched outer JSON delimiter')
            stack.pop()
            if not stack:
                spans.append(raw[start:i+1])
                start = None
    if start is not None or len(spans) != 1:
        raise JSONEnvelopeError('Reply must contain exactly one complete JSON object')
    try:
        value = json.loads(spans[0], object_pairs_hook=_unique_keys, parse_constant=_finite, parse_float=_float)
    except (ValueError, TypeError) as error:
        raise JSONEnvelopeError('Malformed JSON object') from error
    if not isinstance(value, dict):
        raise JSONEnvelopeError('Root JSON arrays are not actions or decisions')
    return value
