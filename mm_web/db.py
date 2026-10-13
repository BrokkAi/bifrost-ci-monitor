"""Request-scoped read-only SQLite with a constrained query console."""
from __future__ import annotations
import json
import re
import sqlite3
import time
import threading
import monitor
from mm_web.errors import ApiError
from mm_web.models import Schema, SchemaColumn, SchemaTable, SqlResult
from mm_web.security import sensitive, scrub
from mm_web.allowlist import COLUMNS, allowed

SQL_TIMEOUT = 5.0
SQL_LENGTH_LIMIT = 1024 * 1024
SQL_TEXT_LIMIT = 100 * 1024
SQL_RESPONSE_LIMIT = 5 * 1024 * 1024
SQL_SLOTS = threading.BoundedSemaphore(2)


def connection():
    conn = None
    try:
        conn = sqlite3.connect(f'{monitor.DB_PATH.resolve().as_uri()}?mode=ro', uri=True,
                               timeout=0.25, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=1')
        conn.execute('PRAGMA busy_timeout=250')
    except sqlite3.Error:
        if conn is not None:
            conn.close()
        conn = None
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


def require(conn):
    if conn is None:
        raise ApiError(503, 'database_unavailable', 'Dashboard database is unavailable')
    return conn


def has_table(conn, name):
    return conn is not None and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def rows(conn, table, *, where='1', params=(), order='', limit=None, columns='*'):
    require(conn)
    if not has_table(conn, table):
        return []
    sql = f'SELECT {columns} FROM "{table}" WHERE {where}'
    if order:
        sql += ' ORDER BY ' + order
    if limit is not None:
        sql += ' LIMIT ?'
        params = (*params, limit)
    return [dict(row) for row in conn.execute(sql, params)]


def schema(conn):
    require(conn)
    tables = []
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        name = row['name']
        if name not in COLUMNS or sensitive(name):
            continue
        quoted = name.replace('"', '""')
        columns = [SchemaColumn(name=c['name'], type=c['type'], nullable=not bool(c['notnull']),
                    primary_key=bool(c['pk'])) for c in conn.execute(f'PRAGMA table_info("{quoted}")')
                   if allowed(name, c['name']) and not sensitive(c['name'])]
        tables.append(SchemaTable(name=name, columns=columns))
    return Schema(tables=tables)


def query(conn, request):
    require(conn)
    if not SQL_SLOTS.acquire(blocking=False):
        raise ApiError(429, 'sql_busy', 'Two SQL queries are already running; try again later')
    try:
        return _query(conn, request)
    finally:
        SQL_SLOTS.release()


def _query(conn, request):
    # SQLite is the parser and authority; this only checks the entry keyword.
    prefix = re.sub(r'/\*.*?\*/|--[^\n]*(?:\n|$)', ' ', request.sql, flags=re.S).lstrip()
    if not re.match(r'(?:SELECT|WITH)\b', prefix, re.I):
        raise ApiError(400, 'invalid_sql', 'Only a single SELECT or WITH statement is allowed')
    deadline = time.monotonic() + SQL_TIMEOUT
    read_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}
    denied_functions = {'load_extension', 'readfile', 'writefile', 'eval', 'edit', 'fts3_tokenizer'}

    def check_deadline():
        if time.monotonic() >= deadline:
            raise ApiError(408, 'sql_timeout', 'Query exceeded the five-second time limit')

    def authorize(action, first, second, database, source):
        if action in read_actions:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if database != 'main' or str(first).lower().startswith('pragma_'):
                return sqlite3.SQLITE_DENY
            if not allowed(first, second) or sensitive(second):
                return sqlite3.SQLITE_IGNORE
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            function = str(second or first).lower()
            return sqlite3.SQLITE_DENY if function in denied_functions or function.startswith('pragma_') else sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    previous_length = conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQL_LENGTH_LIMIT)
    previous_sql_length = conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, SQL_TEXT_LIMIT)
    conn.set_authorizer(authorize)
    conn.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
    result = None
    try:
        check_deadline()
        result = conn.execute(request.sql, request.params)
        check_deadline()
        names = [c[0] for c in result.description or []]
        keep = [i for i, name in enumerate(names) if not sensitive(name)]
        names = [scrub(names[i]) for i in keep]
        encode = lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        # Reserve the complete envelope, not just cell or row bytes.
        used = len(encode({'columns': names, 'rows': [], 'truncated': False}))
        if used > SQL_RESPONSE_LIMIT:
            raise ApiError(400, 'sql_resource_limit', 'SQL result columns exceed the response byte limit')
        output, truncated = [], False
        for index in range(request.limit + 1):
            check_deadline()
            row = result.fetchone()
            check_deadline()
            if row is None:
                break
            if index == request.limit:
                truncated = True
                break
            values = [scrub(row[i]) for i in keep]
            check_deadline()
            size = len(encode(values)) + bool(output)
            check_deadline()
            if used + size > SQL_RESPONSE_LIMIT:
                truncated = True
                break
            output.append(values)
            used += size
        response = SqlResult(columns=names, rows=output, truncated=truncated)
        check_deadline()
        response.model_dump_json()  # Check the final model serialization within the same deadline.
        check_deadline()
        return response
    except sqlite3.Error as exc:
        check_deadline()
        if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_TOOBIG:
            raise ApiError(400, 'sql_resource_limit', 'SQL exceeded the SQLite size limit') from None
        raise ApiError(400, 'invalid_sql', 'Query must be one valid read-only SELECT or WITH statement') from None
    except (ValueError, TypeError):
        raise ApiError(400, 'invalid_sql', 'SQL result cannot be serialized') from None
    finally:
        if result is not None:
            result.close()
        conn.set_progress_handler(None, 0)
        conn.set_authorizer(None)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_length)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, previous_sql_length)
