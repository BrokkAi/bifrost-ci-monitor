"""Shared stdlib-only secret filtering for diagnostics and public text."""
from __future__ import annotations
import json
import re

SENSITIVE = re.compile(r'prompt|token|secret|connection', re.I)
HMAC_VALUE = re.compile(r'(?<![A-Za-z0-9_])[0-9a-fA-F]{64}(?![A-Za-z0-9_])')
HEX = frozenset('0123456789abcdefABCDEF')
CREDENTIAL = re.compile(r'\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|mmw_[A-Za-z0-9_.-]+)\b')
BEARER = re.compile(r'\bBearer\s+[^\s\"\x27,}]+', re.I)
JSON_SECRET = re.compile(
    r'(["\x27][^"\x27]*(?:prompt|token|secret|connection)[^"\x27]*["\x27]\s*:\s*)'
    r'("(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27)', re.I)
DIAGNOSTIC_PRIVATE = re.compile(
    r'prompt|token|secret|connection|password|passwd|credential|authorization|api.?key|access.?key|'
    r'^(?:message|messages|instructions|content|body)$', re.I)
SECRET_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s;&|]+)'''
COMMAND_SECRET = re.compile(
    r'(\b[A-Za-z_]*(?:token|secret|password|passwd|api_?key|access_?key|connection|prompt)'
    r'[A-Za-z0-9_]*\s*=\s*|--[A-Za-z_-]*(?:token|password|passwd|secret|api-key|access-key|connection|prompt)'
    r'[A-Za-z_-]*(?:=|\s+))' + SECRET_VALUE, re.I)


def diagnostic(value):
    """Additional filtering for explicitly requested active-call diagnostics."""
    if isinstance(value, dict):
        return {str(key): diagnostic(item) for key, item in value.items()
                if not DIAGNOSTIC_PRIVATE.search(str(key))}
    if isinstance(value, (list, tuple)):
        return [diagnostic(item) for item in value]
    if isinstance(value, str):
        value = COMMAND_SECRET.sub(lambda match: match[1] + '[redacted]', decode_unicode(value))
    return scrub(value)


def decode_unicode(value):
    """Decode nested Unicode quoting in linear time, including escaped slashes."""
    if '\\u' not in value:
        return value
    output = []
    for character in value:
        output.append(character)
        while (len(output) >= 6 and output[-6] == '\\' and output[-5] == 'u'
               and all(c in HEX for c in output[-4:])):
            decoded = chr(int(''.join(output[-4:]), 16))
            del output[-6:]
            output.append(decoded)
    return ''.join(output)


def sensitive(name):
    return bool(SENSITIVE.search(str(name)))


def scrub(value):
    if isinstance(value, dict):
        return {str(k): scrub(v) for k, v in value.items()
                if not sensitive(k) and k != 'supervisor_instruction'}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, bytes):
        return '[binary omitted]'
    if isinstance(value, str):
        # Values, including embedded JSON preceded by prose, are sanitized
        # independently of their key spelling or depth of quote escaping.
        value = decode_unicode(value)
        value = value.encode('utf-16', errors='surrogatepass').decode('utf-16', errors='replace')
        value = HMAC_VALUE.sub('[redacted]', value)
        value = BEARER.sub('Bearer [redacted]', CREDENTIAL.sub('[redacted]', value))
        # JSON stored in TEXT columns must get the same recursive filtering.
        if value.lstrip().startswith(('{', '[')):
            try:
                return json.dumps(scrub(json.loads(value)), separators=(',', ':'))
            except (ValueError, RecursionError):
                pass
        # Briefs may quote a JSON connection object one or more layers deep.
        # Normalizing quote escapes makes the same redaction cover those forms.
        value = re.sub(r'\\+(?=["\x27])', '', value)
        value = JSON_SECRET.sub(lambda m: m.group(1) + '"[redacted]"', value)
        return BEARER.sub('Bearer [redacted]', CREDENTIAL.sub('[redacted]', value))[:4000]
    return value
