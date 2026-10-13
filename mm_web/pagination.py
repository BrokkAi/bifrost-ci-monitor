"""Opaque compound keyset cursors, matched to the complete SQL ordering."""
import base64
import binascii
import json
from mm_web.errors import ApiError


BATCH = (('created_at', 'DESC'), ('batch_id', 'DESC'))
OUTBOX = (('created_at', 'DESC'), ('intent_id', 'DESC'))
JOB = (('created_at', 'DESC'), ('id', 'DESC'))
LEDGER = (('last_seen_at', 'DESC'), ('workflow', 'ASC'), ('job_name', 'ASC'),
          ('identity_kind', 'ASC'), ('identity', 'ASC'))


def encode(row, order):
    payload = json.dumps([row[name] for name, _ in order], separators=(',', ':'), ensure_ascii=True)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')


def after(cursor, order):
    if cursor is None:
        return '1', ()
    try:
        if len(cursor) > 16384:
            raise ValueError()
        raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        values = json.loads(raw)
        if (not isinstance(values, list) or len(values) != len(order)
                or any(not isinstance(v, str) or len(v) > 4096 for v in values)):
            raise ValueError()
    except (ValueError, binascii.Error, UnicodeDecodeError, RecursionError):
        raise ApiError(400, 'invalid_cursor', 'History cursor is invalid') from None
    terms, params = [], []
    for i, (name, direction) in enumerate(order):
        equal = [f'{order[j][0]}=?' for j in range(i)]
        comparison = f'{name}{"<" if direction == "DESC" else ">"}?'
        terms.append('(' + ' AND '.join([*equal, comparison]) + ')')
        params.extend([*values[:i], values[i]])
    return '(' + ' OR '.join(terms) + ')', tuple(params)


def sql_order(order):
    return ','.join(f'{name} {direction}' for name, direction in order)
