"""Dashboard API security and projections on the production SQLite schema."""
from __future__ import annotations
import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

try:
    from fastapi.testclient import TestClient
    from itsdangerous import TimestampSigner
    from mm_web import auth, data, db, models, releases, tokens, outcomes
    from mm_web.security import scrub
    from mm_web.app import app, create_app
except ImportError as exc:
    OPTIONAL_IMPORT_ERROR = f'mm-web optional dependencies unavailable: {exc}; run with uv run'
else:
    OPTIONAL_IMPORT_ERROR = None

import automerge
import issue_fixer
import monitor
import triage
import stabilize
import stabilize_state

ROOT = Path(__file__).resolve().parent
SHA = 'a' * 40
HEAD = 'b' * 40
SECRET = 'fixture-private-value'
PROMPT = 'Complete private stored prompt: ' + SECRET


@unittest.skipIf(OPTIONAL_IMPORT_ERROR is not None, OPTIONAL_IMPORT_ERROR or '')
class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for obj, name, value in [(monitor, 'DB_PATH', self.root / 'activity.db'),
                                  (automerge, 'DB_PATH', self.root / 'activity.db'),
                                  (monitor, 'STATE_DIR', self.root / 'state'),
                                  (monitor, 'CONFIG_DIR', self.root / 'config'),
                                  (monitor, 'CHANNEL_PATH', self.root / 'channel'),
                                  (stabilize, 'LOCK_PATH', self.root / 'stabilize.lock')]:
            patch = mock.patch.object(obj, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        environment = mock.patch.dict('os.environ', {'MM_WEB_PUBLIC_URL': 'http://localhost:8770', 'BIFROST_FIXER_CONCURRENCY': '5'})
        environment.start()
        self.addCleanup(environment.stop)
        self.external = []
        for obj, name in [(monitor, 'run_gh'), (monitor, 'mj_command'), (automerge, 'run_gh')]:
            patch = mock.patch.object(obj, name, side_effect=AssertionError('unexpected external call'))
            self.external.append(patch.start())
            self.addCleanup(patch.stop)
        # Ephemeral fixtures need the real schema and transactions, not disk
        # durability. Avoid per-DDL fsync stalls on the shared test filesystem.
        real_connect = sqlite3.connect
        def fixture_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.execute('PRAGMA synchronous=OFF')
            return conn
        with mock.patch.object(sqlite3, 'connect', side_effect=fixture_connect):
            conn = monitor.connect_db()
            conn.close()
            conn = automerge.connect_db()
        issue_fixer.ensure_schema(conn)
        triage.ensure_schema(conn)
        stabilize.ensure_schema(conn)
        # Concurrent heartbeat extraction is additive; fallback allows this
        # suite to validate the specified contract before that work is ready.
        conn.execute('''CREATE TABLE IF NOT EXISTS entrypoint_heartbeats (
            entry_point TEXT PRIMARY KEY, last_started_at TEXT, last_finished_at TEXT,
            last_outcome TEXT, last_error TEXT, last_skipped_at TEXT)''')
        self.created = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.finished = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        for identifier, number in [('primary', 123), ('speculative', 124), ('landed', 121), ('direct', 122)]:
            p = automerge.PullRequest(number, 'Fixture PR', HEAD, f'https://github.com/{monitor.REPO_NAME}/pull/{number}')
            automerge.create_batch(conn, [p], SHA, batch_id=identifier, ci_mode='async',
                                   kind='direct' if identifier == 'direct' else 'batch',
                                   predecessor_id='primary' if identifier == 'speculative' else None)
            conn.execute('UPDATE automerge_batches SET created_at=?,session_id=? WHERE batch_id=?',
                         (self.created, 'session-' + identifier, identifier))
        conn.execute("UPDATE automerge_batches SET status='running',phase='fixing',pending_prompt=?,prompt_command_id='secret-command' WHERE batch_id='primary'", (PROMPT,))
        conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='merged',finished_at=? WHERE batch_id='landed'", (self.finished,))
        conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='direct_rejected',finished_at=? WHERE batch_id='direct'", (self.finished,))
        for number in [123, 124, 125, 126, 127, 128]:
            item = {'number': number, 'state': 'OPEN', 'isDraft': number == 127, 'title': 'PR ' + str(number),
                    'author': {'login': 'fixture-author'}, 'headRefOid': HEAD,
                    'labels': [{'name': 'mergemarshall:high'}]}
            conn.execute('INSERT INTO automerge_pr_inventory(number,head_sha,data_json,blocked_reason) VALUES (?,?,?,?)',
                         (number, HEAD, json.dumps(item), 'prerequisite unavailable' if number == 125 else None))
        conn.execute('DELETE FROM automerge_github_outbox')
        conn.execute('''INSERT INTO automerge_github_outbox
            (intent_id,batch_id,kind,number,head_sha,payload_json,created_at,last_error,attempts)
            VALUES ('intent','primary','reject_head',126,?,'{}',?,?,2)''',
            (HEAD, self.created, '{"token": "' + SECRET + '"}'))
        conn.execute('''INSERT INTO known_failures
            (workflow,job_name,identity_kind,identity,first_seen_sha,first_seen_run_id,first_seen_run_url,
             first_seen_at,last_seen_sha,last_seen_run_id,last_seen_run_url,last_seen_at,status,updated_at,diagnosis)
            VALUES ('CI','tests','test','fixture_test',?,1,'https://github.com/run/1',?,?,2,
                    'https://github.com/run/2',?,'open',?,?)''',
            (SHA, self.created, HEAD, self.finished, self.finished, '{"token": "' + SECRET + '"}'))
        conn.execute('''INSERT INTO triage_jobs
            (id,title,base_sha,created_at,status,observations_json,session_id,recovery_json,last_error)
            VALUES ('triage','Fixture investigation',?,?,'running','[{},{}]','triage-session','{"stage":"clear"}',?)''',
            (SHA, self.created, 'Bearer ghp_' + SECRET.replace('-', '_')))
        conn.execute('''INSERT INTO issue_repairs
            (id,work_key,issue_number,issue_url,title,branch,base_sha,status,created_at,prompt,recovery_json)
            VALUES ('repair','work',555,'https://github.com/issue/555','Fix fixture','fixture',?,'running',?,?,'{"stage":"clear"}')''',
            (SHA, self.created, PROMPT))
        conn.execute('''INSERT INTO automerge_supervisor_actions
            (action_id,kind,batch_id,input_json,status,updated_at)
            VALUES ('action','observe','primary','{}','pending',?)''', (self.created,))
        conn.execute('''INSERT INTO automerge_executions VALUES ('primary','execution','session',?,?)''',
            (json.dumps({'command': 'python -m unittest', 'head': HEAD, 'exit_code': 0, 'duration_seconds': 25}), self.created))
        conn.execute('''INSERT INTO automerge_provider_failovers VALUES ('primary','session',?,?)''',
            (json.dumps({'stage': 'complete', 'token': SECRET}), self.finished))
        for entry in ['merger', 'monitor', 'triage']:
            conn.execute('''INSERT OR REPLACE INTO entrypoint_heartbeats
                (entry_point,last_started_at,last_finished_at,last_outcome) VALUES (?,?,?,'ok')''',
                (entry, self.created, self.finished))
        conn.commit()
        conn.close()
        for cache in [auth.TOKEN_CACHE, auth.PERMISSION_CACHE]:
            cache.clear()
        for name, value in [('github_user', 'fixture-user'), ('permission', 'write')]:
            patch = mock.patch.object(auth, name, return_value=value)
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)
        patch = mock.patch.object(data, 'skill_health', return_value=models.SkillServiceHealth(reachable=True, version='1'))
        patch.start()
        self.addCleanup(patch.stop)
        self.client = TestClient(create_app())
        self.addCleanup(self.client.close)
        self.headers = {'Authorization': 'Bearer ghp_fixture'}

    def get(self, path, **kwargs):
        return self.client.get('/api/v1' + path, headers=self.headers, **kwargs)

    def post(self, path, value=None, **kwargs):
        return self.client.post('/api/v1' + path, headers=self.headers, json=value, **kwargs)

    def session_cookie(self):
        secret = tokens.key(monitor.STATE_DIR / 'mm-web-session.key', create=True)
        claims = base64.b64encode(json.dumps({'login': 'fixture-user', 'issued_at': time.time()}).encode())
        signed = TimestampSigner(secret.hex()).sign(claims).decode()
        self.client.cookies.set('mm_web_session', signed, domain='testserver.local', path='/')

    def test_auth_levels_and_error_shape(self):
        self.assertEqual(self.client.get('/api/v1/healthz').status_code, 200)
        self.assertFalse((monitor.STATE_DIR / 'mm-web-session.key').exists())
        result = self.client.get('/api/v1/overview')
        self.assertEqual(result.status_code, 401)
        self.assertEqual(set(result.json()['error']), {'code', 'message'})
        self.assertEqual(self.client.get('/', follow_redirects=False).headers['location'], '/auth/login')
        self.assertEqual(self.client.get('/api/v1/healthz').status_code, 200)
        for level in ['write', 'maintain', 'admin']:
            with self.subTest(level=level):
                self.permission.return_value = level
                self.assertEqual(self.get('/me').json()['permission'], level)
                self.assertEqual(self.get('/me').json()['can_release'], level != 'write')
        self.permission.return_value = None
        self.assertEqual(self.get('/overview').status_code, 403)

    def test_service_tokens_read_only(self):
        value = tokens.mint('ci-agent')
        self.headers = {'Authorization': 'Bearer ' + value}
        result = self.get('/me')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['auth'], 'service_token')
        self.assertFalse(result.json()['can_release'])
        self.assertEqual(self.get('/overview').status_code, 200)
        self.assertEqual(self.post('/sql', {'sql': 'SELECT 1'}).status_code, 200)
        self.assertEqual(self.post('/releases', {'sha': SHA, 'idempotency_key': 'x'}).status_code, 403)
        self.assertEqual(self.post('/releases/campaign/abort', {}).status_code, 403)
        self.assertEqual(self.post('/releases/campaign/resume', {}).status_code, 403)
        self.github_user.assert_not_called()
        self.permission.assert_not_called()
        self.headers = {'Authorization': 'Bearer ' + value[:-1] + ('a' if value[-1] != 'a' else 'b')}
        self.assertEqual(self.get('/me').status_code, 401)
        self.assertEqual((monitor.STATE_DIR / 'mm-web-service.key').stat().st_mode & 0o777, 0o600)

    def test_csrf(self):
        self.session_cookie()
        self.assertEqual(self.client.get('/api/v1/me').status_code, 200)
        self.assertEqual(self.client.post('/api/v1/sql', content='{"sql":"SELECT 1"}').status_code, 403)
        self.assertEqual(self.client.post('/api/v1/sql', content='SELECT 1', headers={'Content-Type': 'text/plain'}).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/sql', json={'sql': 'SELECT 1'}, headers={'Origin': 'https://evil.example'}).status_code, 403)
        result = self.client.post('/api/v1/sql', json={'sql': 'SELECT 1'}, headers={'Origin': 'http://localhost:8770'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.post('/auth/logout', json={}).status_code, 200)
        self.assertEqual(self.client.get('/api/v1/me').status_code, 401)

    def test_oauth_cookie_contains_only_login_and_issue_time(self):
        from fastapi.responses import RedirectResponse
        provider = types.SimpleNamespace(
            authorize_redirect=mock.AsyncMock(return_value=RedirectResponse('/provider')),
            authorize_access_token=mock.AsyncMock(return_value={'access_token': 'ghp_private_oauth_fixture'}))
        with mock.patch.object(auth, 'oauth_client', return_value=provider):
            result = self.client.get('/auth/login', follow_redirects=False)
            self.assertEqual(result.status_code, 307)
            self.assertEqual(provider.authorize_redirect.call_args.args[1], 'http://localhost:8770/auth/callback')
            result = self.client.get('/auth/callback', follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertIn('httponly', result.headers['set-cookie'].lower())
        self.assertIn('samesite=lax', result.headers['set-cookie'].lower())
        cookie = self.client.cookies.get('mm_web_session')
        secret = tokens.key(monitor.STATE_DIR / 'mm-web-session.key')
        raw = TimestampSigner(secret.hex()).unsign(cookie)
        claims = json.loads(base64.b64decode(raw))
        self.assertEqual(set(claims), {'login', 'issued_at'})
        self.assertNotIn('ghp_private_oauth_fixture', result.headers['set-cookie'])
        self.assertEqual(self.client.get('/api/v1/me').status_code, 200)

    def test_secure_cookie_and_key_permissions(self):
        with mock.patch.dict('os.environ', {'MM_WEB_PUBLIC_URL': 'https://dashboard.example'}):
            client = TestClient(create_app(), base_url='https://dashboard.example')
            self.addCleanup(client.close)
            provider = types.SimpleNamespace(authorize_access_token=mock.AsyncMock(return_value={'access_token': 'fixture'}))
            with mock.patch.object(auth, 'oauth_client', return_value=provider):
                result = client.get('/auth/callback', follow_redirects=False)
            self.assertIn('secure', result.headers['set-cookie'].lower())
        for name in ['mm-web-session.key', 'mm-web-service.key']:
            path = monitor.STATE_DIR / name
            tokens.key(path, create=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                tokens.key(path)
            path.chmod(0o600)

    def test_every_read_endpoint(self):
        for path in ['/overview', '/batches', '/batches/primary', '/batches/speculative', '/batches/direct',
                     '/queue', '/outbox', '/ledger', '/triage', '/repairs', '/health', '/stats', '/schema', '/me', '/openapi.json', '/docs']:
            with self.subTest(path=path):
                result = self.get(path)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertNotIn(SECRET, result.text)
                self.assertNotIn(PROMPT, result.text)
        result = self.get('/overview').json()
        self.assertEqual(result['pipeline']['primary']['batch_id'], 'primary')
        self.assertEqual(result['pipeline']['speculative']['batch_id'], 'speculative')
        self.assertEqual(result['kpis']['landed_today'], 1)
        self.assertEqual(result['repairs']['concurrency_limit'], 5)
        self.assertEqual(self.get('/batches/primary').json()['executions'][0]['exit_code'], 0)
        self.assertNotIn('supervisor_instruction', self.get('/batches/primary').json()['state'])
        groups = {g['key']: g['items'] for g in self.get('/queue').json()['groups']}
        self.assertEqual([(p['number'], p['batch_id']) for p in groups['in_batch']], [(123, 'primary'), (124, 'speculative')])
        self.assertEqual(groups['waiting'][0]['number'], 128)
        self.assertEqual(groups['dependency_blocked'][0]['number'], 125)
        self.assertEqual(groups['rejected'][0]['number'], 126)
        self.assertEqual(groups['draft'][0]['number'], 127)
        self.assertEqual(self.get('/batches/missing').status_code, 404)
        for external in self.external:
            external.assert_not_called()

    def test_sql_and_schema_hide_sensitive_columns(self):
        for table in self.get('/schema').json()['tables']:
            for column in table['columns']:
                self.assertFalse(any(s in column['name'] for s in ['prompt', 'token', 'secret', 'connection']))
        result = self.post('/sql', {'sql': 'SELECT * FROM automerge_batches'}).json()
        self.assertNotIn('pending_prompt', result['columns'])
        self.assertNotIn(PROMPT, json.dumps(result))
        self.assertNotIn('prompt_command_id', result['columns'])
        result = self.post('/sql', {'sql': 'SELECT pending_prompt AS x FROM automerge_batches'}).json()
        self.assertTrue(all(row == [None] for row in result['rows']))
        result = self.post('/sql', {'sql': 'SELECT prompt AS x FROM issue_repairs'}).json()
        self.assertEqual(result['rows'], [[None]])
        result = self.post('/sql', {'sql': 'SELECT last_error FROM automerge_github_outbox'}).json()
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.post('/sql', {'sql': 'SELECT :p AS value', 'params': {'p': 42}}).json()['rows'], [[42]])
        result = self.post('/sql', {'sql': 'WITH values_cte(x) AS (SELECT 1 UNION ALL SELECT 2) SELECT * FROM values_cte', 'limit': 1}).json()
        self.assertTrue(result['truncated'])
        self.assertEqual(result['rows'], [[1]])

    def test_allowlists_drop_new_columns_and_nested_briefs(self):
        conn = sqlite3.connect(monitor.DB_PATH)
        conn.execute('ALTER TABLE automerge_batches ADD COLUMN future_payload TEXT')
        conn.execute('CREATE TABLE future_table(value TEXT)')
        conn.execute('INSERT INTO future_table VALUES (?)', (PROMPT,))
        conn.execute('UPDATE automerge_batches SET future_payload=?, agent_final_message=?, recovery_json=?',
                     (PROMPT, PROMPT, json.dumps({'stage': 'clear', 'brief': PROMPT, 'restart_text': PROMPT})))
        conn.execute('UPDATE issue_repairs SET report_json=?,recovery_json=?',
                     (json.dumps({'body': PROMPT}), json.dumps({'brief': PROMPT})))
        conn.execute('UPDATE triage_jobs SET report_json=?,recovery_json=?',
                     (json.dumps({'body': PROMPT}), json.dumps({'brief': PROMPT})))
        conn.execute('UPDATE automerge_github_outbox SET last_error=?',
                     ('quoted connection: {\\"token\\": \\"' + SECRET + '\\"}',))
        conn.commit()
        conn.close()
        schema = self.get('/schema').json()
        self.assertNotIn('future_table', [t['name'] for t in schema['tables']])
        result = self.post('/sql', {'sql': 'SELECT value FROM future_table'})
        self.assertEqual(result.json()['rows'], [[None]])
        for table in schema['tables']:
            self.assertNotIn('future_payload', [c['name'] for c in table['columns']])
            for name in ['recovery_json', 'report_json', 'agent_final_message']:
                self.assertNotIn(name, [c['name'] for c in table['columns']])
        result = self.post('/sql', {'sql': 'SELECT future_payload AS x, recovery_json AS y, agent_final_message AS z FROM automerge_batches'})
        self.assertTrue(all(row == [None, None, None] for row in result.json()['rows']))
        for path in ['/batches/primary', '/triage', '/repairs', '/outbox']:
            result = self.get(path)
            self.assertNotIn(SECRET, result.text)
        self.assertEqual(self.get('/batches/primary').json()['state']['recovery'], {'stage': 'clear'})

    def test_sql_rejects_writes_and_multiple_statements(self):
        before = monitor.DB_PATH.read_bytes()
        for sql in ['DELETE FROM automerge_batches', "UPDATE issue_repairs SET prompt='oops'",
                    'CREATE TABLE evil(x)', 'SELECT 1; SELECT 2', "ATTACH DATABASE ':memory:' AS evil",
                    'PRAGMA user_version=123', 'PRAGMA query_only=0',
                    'WITH c AS (SELECT 1) DELETE FROM automerge_batches',
                    "SELECT load_extension('/tmp/evil')", "SELECT * FROM pragma_table_info('issue_repairs')",
                    "SELECT * FROM pragma_table_info('issue_repairs') WHERE name='prompt'"]:
            with self.subTest(sql=sql):
                self.assertEqual(self.post('/sql', {'sql': sql}).status_code, 400)
        self.assertEqual(monitor.DB_PATH.read_bytes(), before)
        generator = db.connection()
        conn = next(generator)
        self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute('DELETE FROM automerge_batches')
        generator.close()

    def test_unexpected_errors_do_not_log_secret_payloads(self):
        with mock.patch.object(data, 'overview', side_effect=ValueError(PROMPT)), self.assertLogs('mm_web', level='ERROR') as captured:
            result = self.get('/overview')
        self.assertEqual(result.status_code, 503)
        self.assertNotIn(SECRET, result.text)
        self.assertNotIn(SECRET, str(captured.output))
        self.assertIn('ValueError', str(captured.output))

    def test_sql_timeout(self):
        with mock.patch.object(db, 'SQL_TIMEOUT', 0.005):
            result = self.post('/sql', {'sql': '''WITH RECURSIVE c(x) AS
                (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<100000000) SELECT sum(x) FROM c'''})
        self.assertEqual(result.status_code, 408)
        self.assertEqual(result.json()['error']['code'], 'sql_timeout')

    def test_health_missing_and_stale(self):
        conn = sqlite3.connect(monitor.DB_PATH)
        conn.execute('UPDATE entrypoint_heartbeats SET last_finished_at=? WHERE entry_point=?',
                     ((datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(), 'merger'))
        conn.execute("UPDATE entrypoint_heartbeats SET last_error='failed' WHERE entry_point='monitor'")
        conn.commit()
        conn.close()
        states = {v['name']: v['state'] for v in self.get('/health').json()['entry_points']}
        self.assertEqual(states, {'merger': 'stale', 'monitor': 'failing', 'triage': 'ok', 'stabilize': 'unknown'})
        with mock.patch.object(monitor, 'DB_PATH', self.root / 'missing.db'):
            result = self.get('/health')
            self.assertFalse(result.json()['db']['path_ok'])
            self.assertTrue(all(v['state'] == 'unknown' for v in result.json()['entry_points']))
            self.assertEqual(self.get('/overview').status_code, 503)

    def test_release_guard_and_unavailable(self):
        with mock.patch.dict(sys.modules, {'stabilize': None}):
            for path in ['/releases', '/releases/resolve', '/releases/missing']:
                self.assertEqual(self.get(path).status_code, 503)
            self.assertEqual(self.post('/releases', {'sha': SHA, 'idempotency_key': 'x'}).status_code, 403)
            for level in ['maintain', 'admin']:
                self.permission.return_value = level
                result = self.post('/releases', {'sha': SHA, 'idempotency_key': 'x'})
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json()['error']['code'], 'release_machinery_unavailable')
                self.assertEqual(self.post('/releases/id/abort', {}).status_code, 503)
                self.assertEqual(self.post('/releases/id/resume', {}).status_code, 503)

    def test_release_adapter_real_engine(self):
        self.permission.return_value = 'maintain'
        with mock.patch.object(stabilize, 'ensure_ref', return_value=True), mock.patch.object(stabilize, 'deliver_slack'):
            result = self.post('/releases', {'sha': SHA, 'idempotency_key': 'key', 'requested_by': 'attacker'})
            self.assertEqual(result.status_code, 200, result.text)
            campaign = result.json()
            identifier = campaign['id']
            self.assertEqual(campaign['requested_by'], 'fixture-user')
            self.assertEqual(campaign['status'], 'running')
            self.assertEqual(campaign['anchor_sha'], SHA)
            self.assertEqual(campaign['branch'], 'stabilize/' + identifier)
            self.assertEqual(campaign['latest_runs'], {'push': None, 'hourly': None, 'nightly': None})
            self.assertIsNone(campaign['start_error'])
            self.assertNotIn('idempotency_key', campaign)
            self.assertEqual(self.get('/releases').json()[0]['id'], identifier)
            self.assertEqual(self.get('/releases/' + identifier).json()['id'], identifier)
            with closing(sqlite3.connect(monitor.DB_PATH)) as conn, conn:
                conn.execute("UPDATE stabilize_campaigns SET status='paused',pause_reason='actions permission denied' WHERE id=?", (identifier,))
            self.assertEqual(self.get('/releases/' + identifier).json()['pause_reason'], 'actions permission denied')
            resumed = self.post('/releases/' + identifier + '/resume', {'requested_by': 'attacker'})
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertEqual(resumed.json()['status'], 'running')
            self.assertIsNone(resumed.json()['pause_reason'])
            with closing(sqlite3.connect(monitor.DB_PATH)) as conn:
                message = conn.execute("SELECT text FROM stabilize_messages WHERE campaign_id=? AND checkpoint LIKE 'resumed:%'", (identifier,)).fetchone()[0]
            self.assertIn('fixture-user', message)
            self.assertNotIn('attacker', message)
            self.assertEqual(self.post('/releases/' + identifier + '/abort', {}).json()['status'], 'aborted')
            self.assertEqual(self.get('/releases/missing').status_code, 404)

    def test_stabilize_exception_mapping_real_engine(self):
        self.permission.return_value = 'maintain'
        with mock.patch.object(stabilize, 'ensure_ref', return_value=True), mock.patch.object(stabilize, 'deliver_slack'):
            first = self.post('/releases', {'sha': SHA, 'idempotency_key': 'first'}).json()
            result = self.post('/releases', {'sha': SHA, 'idempotency_key': 'another'})
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json()['error']['campaign_id'], first['id'])
            self.assertEqual(self.post('/releases/missing/abort', {}).status_code, 404)
            self.assertEqual(self.post('/releases/missing/resume', {}).status_code, 404)
            with mock.patch.object(monitor, 'run_gh', side_effect=monitor.CommandError('HTTP 404 Not Found')):
                self.assertEqual(self.get('/releases/resolve?commitish=bad').status_code, 400)
            self.post('/releases/' + first['id'] + '/abort', {})
            result = self.post('/releases/' + first['id'] + '/resume', {})
            self.assertEqual(result.status_code, 502)
            with mock.patch.object(stabilize, 'resume_campaign', side_effect=stabilize.StabilizeError('provider says {"token": "' + SECRET + '"}')):
                result = self.post('/releases/' + first['id'] + '/resume', {})
            self.assertEqual(result.status_code, 502)
            self.assertNotIn(SECRET, result.text)

    def test_release_busy_header_and_resume_permission(self):
        with mock.patch.object(stabilize, 'lock', side_effect=stabilize.LockTimeout('busy')) as lock:
            self.assertEqual(self.post('/releases/missing/resume', {}).status_code, 403)
            lock.assert_not_called()
            for level in ['maintain', 'admin']:
                self.permission.return_value = level
                for path, payload in [('/releases', {'sha': SHA, 'idempotency_key': 'retry'}),
                                      ('/releases/missing/abort', {}), ('/releases/missing/resume', {})]:
                    with self.subTest(level=level, path=path):
                        result = self.post(path, payload)
                        self.assertEqual(result.status_code, 503)
                        self.assertEqual(result.json()['error']['code'], 'release_busy')
                        self.assertIn('retry', result.json()['error']['message'])
                        self.assertEqual(result.headers['Retry-After'], '5')

    def test_spa_fallback_rejects_escaping_index_symlink(self):
        self.session_cookie()
        with mock.patch('mm_web.app.ROOT', self.root):
            dist = self.root / 'web/dist'
            dist.mkdir(parents=True)
            outside = self.root / 'outside-private.txt'
            outside.write_text(SECRET)
            (dist / 'index.html').symlink_to(outside)
            for path in ['/', '/client-route', '/index.html']:
                result = self.client.get(path)
                self.assertEqual(result.status_code, 404)
                self.assertNotIn(SECRET, result.text)

    def test_static_and_openapi(self):
        self.assertEqual(self.get('/openapi.json').json(), app.openapi())
        self.assertEqual(json.loads((ROOT / 'web/openapi.json').read_text()), app.openapi())
        self.session_cookie()
        with mock.patch('mm_web.app.ROOT', self.root):
            result = self.client.get('/')
            self.assertEqual(result.status_code, 200)
            self.assertIn('Build web/', result.text)
            dist = self.root / 'web/dist'
            dist.mkdir(parents=True)
            (dist / 'index.html').write_text('<html>fixture UI</html>')
            (dist / 'asset.js').write_text('fixture_asset')
            self.assertEqual(self.client.get('/queue').text, '<html>fixture UI</html>')
            self.assertEqual(self.client.get('/asset.js').text, 'fixture_asset')
            self.assertEqual(self.client.get('/api/v1/missing').status_code, 404)
        self.assertEqual(self.get('/batches?limit=501').status_code, 422)


@unittest.skipIf(OPTIONAL_IMPORT_ERROR is not None, OPTIONAL_IMPORT_ERROR or '')
class ReadRegressionTests(unittest.TestCase):
    """Direct data tests also run when the sandbox blocks TestClient wakeups."""
    def setUp(self):
        fixture = WebTests()
        fixture.setUp()
        self.fixture = fixture
        self.addCleanup(fixture.doCleanups)
        generator = db.connection()
        self.conn = next(generator)
        self.addCleanup(generator.close)

    def write(self, sql, params=()):
        with closing(sqlite3.connect(monitor.DB_PATH)) as conn, conn:
            conn.execute('PRAGMA synchronous=OFF')
            conn.execute(sql, params)

    def query(self, sql, limit=1000):
        return db.query(self.conn, models.SqlRequest(sql=sql, limit=limit))

    def test_sql_diagnostic_expressions_are_masked_before_evaluation(self):
        credential = 'a1' * 32
        self.write('UPDATE automerge_github_outbox SET last_error=?',
                   (json.dumps({'token': credential}),))
        result = self.query("SELECT json_extract(last_error,'$.token') AS extracted, hex(last_error) AS encoded FROM automerge_github_outbox")
        self.assertEqual(result.rows, [[None, '']])
        self.assertNotIn(credential, result.model_dump_json())
        self.assertNotIn(json.dumps({'token': credential}).encode().hex().upper(), result.model_dump_json())
        hidden = {'last_error', 'abort_reason', 'diagnosis', 'details', 'pause_reason', 'title', 'data_json',
                  'command', 'evidence', 'blocked_reason', 'agent_final_message', 'recovery_json', 'report_json'}
        for table in db.schema(self.conn).tables:
            self.assertFalse(hidden & {column.name for column in table.columns}, table.name)

    def test_curated_scrub_redacts_values_and_unicode_escaped_keys(self):
        credential = 'a1' * 32
        prose = 'connection diagnostic: {"to\\u006ben":"' + credential + '"}'
        self.write('UPDATE automerge_github_outbox SET last_error=?', (prose,))
        rendered = data.outbox(self.conn).items[0].last_error
        self.assertNotIn(credential, rendered)
        for value in [credential, '\\u0061' + credential[1:],
                      '\\u005c' + 'u005c' * 12 + 'u0061' + credential[1:],
                      'ghp_fixture', 'gho_fixture', 'ghu_fixture', 'ghs_fixture', 'ghr_fixture',
                      'github_pat_fixture', 'mmw_fixture.' + credential, 'Bearer arbitrary-credential']:
            with self.subTest(value=value[:12]):
                self.assertIn('[redacted]', scrub('unrelated key: ' + value))
                self.assertNotIn(value, scrub('unrelated key: ' + value))
        self.assertEqual(scrub(HEAD), HEAD)
        self.assertEqual(scrub('a' * 63), 'a' * 63)
        self.assertEqual(scrub('a' * 65), 'a' * 65)

    def test_sql_allocation_and_statement_limits(self):
        for sql in ["SELECT hex(zeroblob(16777216)) AS large",
                    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<3000) SELECT group_concat(printf('%0400d',x),'') FROM c",
                    'SELECT 1 /*' + 'é' * 60000 + '*/']:
            with self.subTest(sql=sql[:50]), self.assertRaises(db.ApiError) as caught:
                self.query(sql)
            self.assertEqual(caught.exception.status_code, 400)
        observed = []
        self.conn.create_function('fixture_limits', 0, lambda: observed.append((
            self.conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), self.conn.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH))) or 1)
        self.query('SELECT fixture_limits()')
        self.assertEqual(observed, [(1024 * 1024, 100 * 1024)])

    def test_sql_response_byte_cap_and_scalar_deadline(self):
        result = self.query("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<4000) SELECT printf('%02000d',x) AS content FROM c", limit=4000)
        self.assertTrue(result.truncated)
        self.assertLess(len(result.rows), 4000)
        size = len(json.dumps(result.model_dump(), ensure_ascii=False, separators=(',', ':')).encode())
        self.assertLessEqual(size, 5 * 1024 * 1024)
        # No progress callback is required: post-execute/fetch/encode deadline checks apply to scalar work.
        clock = mock.Mock(side_effect=[0, 0, 0, 0, 0, 0, 6])
        with mock.patch.object(db.time, 'monotonic', clock), self.assertRaises(db.ApiError) as caught:
            self.query('SELECT hex(zeroblob(16))')
        self.assertEqual(caught.exception.status_code, 408)

    def test_sql_concurrency_limit(self):
        started = threading.Barrier(3)
        release = threading.Event()
        failures = []

        def run():
            generator = db.connection()
            conn = next(generator)
            def wait():
                started.wait(timeout=3)
                release.wait(timeout=3)
                return 1
            conn.create_function('fixture_wait', 0, wait)
            try:
                db.query(conn, models.SqlRequest(sql='SELECT fixture_wait()'))
            except Exception as exc:
                failures.append(type(exc).__name__)
            finally:
                generator.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            started.wait(timeout=3)
            with self.assertRaises(db.ApiError) as caught:
                self.query('SELECT 1')
            self.assertEqual(caught.exception.status_code, 429)
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=5)
        self.assertFalse(failures)
        self.assertEqual(self.query('SELECT 1').rows, [[1]])

    def test_compound_pagination_ties(self):
        expected_batches = ['speculative', 'primary', 'landed', 'direct']
        cases = [('batches', lambda cursor: data.batches(self.conn, limit=1, before=cursor), expected_batches, 'batch_id'),
                 ('outbox', lambda cursor: data.outbox(self.conn, limit=1, before=cursor), ['tie-z', 'tie-a', 'intent'], 'intent_id'),
                 ('triage', lambda cursor: data.triage(self.conn, limit=1, before=cursor), ['triage-z', 'triage-a', 'triage'], 'id')]
        for identifier in ['tie-a', 'tie-z']:
            self.write("INSERT INTO automerge_github_outbox(intent_id,batch_id,kind,number,head_sha,payload_json,created_at) VALUES (?,'primary','issue_comment',123,?,'{}',?)",
                       (identifier, HEAD, self.fixture.created))
        for identifier in ['triage-a', 'triage-z']:
            self.write("INSERT INTO triage_jobs(id,title,base_sha,created_at,status,observations_json) VALUES (?,'fixture',?,?,'completed','[]')",
                       (identifier, SHA, self.fixture.created))
        for name, fetch, expected, key in cases:
            with self.subTest(endpoint=name):
                found, cursor = [], None
                while True:
                    page = fetch(cursor)
                    found.extend(getattr(item, key) for item in page.items)
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                    self.assertIsInstance(json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4))), list)
                self.assertEqual(found, expected)
        for fetch in [lambda: data.batches(self.conn, before='bad!'), lambda: data.ledger(self.conn, before='bad!')]:
            with self.assertRaises(db.ApiError) as caught:
                fetch()
            self.assertEqual(caught.exception.status_code, 400)
        # Recent repairs have a separate page; active jobs remain a bounded snapshot.
        for identifier in ['repair-a', 'repair-z']:
            self.write("""INSERT INTO issue_repairs(id,work_key,issue_number,issue_url,title,branch,base_sha,status,
                created_at,prompt,cleanup_done) VALUES (?,?,555,'https://github.com/issue/555','fixture',?,?,'completed',?,'private',1)""",
                       (identifier, identifier, identifier, SHA, self.fixture.created))
        first = data.repairs(self.conn, limit=1)
        second = data.repairs(self.conn, limit=1, before=first.next_cursor)
        self.assertEqual([r.id for r in first.recent + second.recent], ['repair-z', 'repair-a'])
        self.assertIsNone(second.next_cursor)

    def test_ledger_pagination_matches_recency_and_identity_ties(self):
        self.write("DELETE FROM known_failures")
        for identity, seen in [('alpha', '2026-10-13T10:00:00+00:00'), ('beta', '2026-10-13T11:00:00+00:00'),
                               ('gamma', '2026-10-13T11:00:00+00:00')]:
            self.write("""INSERT INTO known_failures(workflow,job_name,identity_kind,identity,first_seen_sha,
                first_seen_run_id,first_seen_run_url,first_seen_at,last_seen_sha,last_seen_run_id,last_seen_run_url,
                last_seen_at,status,updated_at) VALUES ('CI','tests','test',?, ?,1,'https://github.com/run/1',?,
                ?,2,'https://github.com/run/2',?,'open',?)""", (identity, SHA, seen, HEAD, seen, seen))
        found, cursor = [], None
        for _ in range(3):
            page = data.ledger(self.conn, limit=1, before=cursor)
            found += [item.identity for item in page.items]
            cursor = page.next_cursor
        self.assertEqual(found, ['beta', 'gamma', 'alpha'])
        self.assertIsNone(cursor)

    def test_real_terminal_outcomes_and_stats(self):
        self.write("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='ci_failed',finished_at=? WHERE batch_id='primary'", (self.fixture.finished,))
        self.write("UPDATE automerge_batches SET status='completed',phase='terminal',terminal_status='ci_round_limit',finished_at=? WHERE batch_id='speculative'", (self.fixture.finished,))
        results = {row.batch_id: row.outcome for row in data.batches(self.conn).items}
        self.assertEqual(results, {'primary': 'failed', 'speculative': 'failed', 'landed': 'landed', 'direct': 'rejected'})
        day = data.stats(self.conn, days=1).days[0]
        self.assertEqual((day.batches_failed, day.rejected_prs, day.batches_completed), (2, 1, 1))
        for terminal, expected in [('integration_pr_not_mergeable', 'failed'), ('integration_pr_missing', 'failed'),
                                   ('mj_launch_ambiguous_expired', 'failed'), ('no_sources_remain', 'withdrawn'),
                                   ('direct_source_changed', 'withdrawn'), ('direct_fell_back_to_batch', 'withdrawn'),
                                   ('direct_rejected_at_head', 'rejected'), ('aborted', 'aborted'), ('future-status', 'other')]:
            with self.subTest(terminal=terminal):
                self.assertEqual(outcomes.category({'terminal_status': terminal, 'status': 'completed'}), expected)
        self.write("UPDATE automerge_batches SET terminal_status='aborted' WHERE batch_id IN ('primary','speculative')")
        self.assertEqual(data.stats(self.conn, days=1).days[0].batches_failed, 0)

    def test_spa_index_containment_without_testclient(self):
        from starlette.requests import Request
        from mm_web import app as web_app
        route = next(r for r in web_app.app.routes if getattr(r, 'path', None) == '/{path:path}')
        dist = self.fixture.root / 'web/dist'
        dist.mkdir(parents=True)
        outside = self.fixture.root / 'private.txt'
        outside.write_text(SECRET)
        (dist / 'index.html').symlink_to(outside)
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'session': {}})
        with mock.patch.object(web_app, 'ROOT', self.fixture.root), mock.patch.object(auth, 'identity',
                return_value=models.Me(login='fixture', permission='write', can_release=False, auth='session')):
            for path in ['', 'client-route', 'index.html']:
                with self.subTest(path=path), self.assertRaises(db.ApiError) as caught:
                    route.endpoint(request, path)
                self.assertEqual(caught.exception.status_code, 404)

    def test_bounded_dashboard_history_reads(self):
        for i in range(205):
            self.write("INSERT INTO automerge_executions VALUES ('primary',?,'session',?,?)",
                       (f'execution-{i:03}', json.dumps({'command': 'fixture', 'head': HEAD}), self.fixture.finished))
            self.write("INSERT INTO automerge_github_outbox(intent_id,batch_id,kind,number,head_sha,payload_json,created_at) VALUES (?,'primary','issue_comment',123,?,'{}',?)",
                       (f'intent-{i:03}', HEAD, self.fixture.finished))
        detail = data.detail(self.conn, 'primary')
        self.assertEqual((len(detail.executions), detail.execution_count), (200, 206))
        self.assertEqual((len(detail.outbox), detail.outbox_count), (200, 206))
        self.assertEqual(detail.executions[0].execution_id, 'execution-204')
        self.assertEqual(detail.outbox[0].intent_id, 'intent-204')
        queries = []
        self.conn.set_trace_callback(queries.append)
        data.overview(self.conn)
        data.repairs(self.conn)
        stats_start = len(queries)
        data.stats(self.conn, 7)
        stats_reads = queries[stats_start:]
        self.assertTrue(any('finished_at>=' in q and 'finished_at<' in q for q in stats_reads))
        self.assertFalse(any('SELECT * FROM "automerge_batches" WHERE 1' in q for q in queries))
        repair_reads = [q for q in queries if 'FROM "issue_repairs"' in q]
        self.assertTrue(repair_reads)
        self.assertTrue(all('LIMIT' in q for q in repair_reads))


@unittest.skipIf(OPTIONAL_IMPORT_ERROR is not None, OPTIONAL_IMPORT_ERROR or '')
class RealReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = WebTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        patch = mock.patch.object(stabilize, 'deliver_slack')
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, sql, params=()):
        with closing(sqlite3.connect(monitor.DB_PATH)) as conn, conn:
            conn.execute('PRAGMA synchronous=OFF')
            conn.execute(sql, params)

    def create(self, key='request'):
        with mock.patch.object(stabilize, 'ensure_ref', return_value=True):
            return releases.create(SHA, 'fixture-user', key)

    def test_real_create_idempotency_read_abort_and_resume(self):
        first = self.create()
        self.assertEqual(first.status, 'running')
        self.assertEqual(first.requested_by, 'fixture-user')
        self.assertEqual(self.create().model_dump(), first.model_dump())
        self.assertEqual(releases.get(first.id).id, first.id)
        self.assertEqual(releases.list_campaigns()[0].id, first.id)
        with self.assertRaises(db.ApiError) as caught:
            self.create('second')
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail['campaign_id'], first.id)
        self.write("UPDATE stabilize_campaigns SET status='paused',pause_reason='permission denied' WHERE id=?", (first.id,))
        resumed = releases.resume(first.id, 'authenticated-maintainer')
        self.assertEqual(resumed.status, 'running')
        self.assertIsNone(resumed.pause_reason)
        with closing(sqlite3.connect(monitor.DB_PATH)) as conn:
            message = conn.execute("SELECT text FROM stabilize_messages WHERE campaign_id=? AND checkpoint LIKE 'resumed:%'", (first.id,)).fetchone()[0]
        self.assertIn('authenticated-maintainer', message)
        self.assertEqual(releases.abort(first.id, 'fixture-user').status, 'aborted')
        for operation in [lambda: releases.get('missing'), lambda: releases.abort('missing', 'fixture-user'),
                          lambda: releases.resume('missing', 'fixture-user')]:
            with self.assertRaises(db.ApiError) as caught:
                operation()
            self.assertEqual(caught.exception.status_code, 404)

    def test_real_view_run_keys_urls_completion_and_start_error(self):
        created = self.create()
        self.write("UPDATE stabilize_campaigns SET thread_ts='123.456',requested_ref='tag',status='completed',stage='complete',finished_at=updated_at WHERE id=?", (created.id,))
        monitor.CHANNEL_PATH.write_text('C123')
        self.write("INSERT INTO stabilize_issues(campaign_id,issue_number,stage,source,round,state,pr_number,pr_head_sha,added_at,updated_at) VALUES (?,555,'push','observed',1,'pr_ready',888,?,?,?)", (created.id, HEAD, self.fixture.created, self.fixture.finished))
        self.write("INSERT INTO stabilize_gate_merges VALUES (?,'push',?,'stabilize-gate/snapshot',999,'merged',?,?)", (created.id, SHA, self.fixture.created, self.fixture.finished))
        for number, stage in [(1, 'push'), (2, 'hourly')]:
            self.write('INSERT INTO stabilize_runs(campaign_id,stage,run_id,tip_sha,conclusion,green,incomplete,processed_at,url) VALUES (?,?,?,?,?,1,0,?,?)',
                       (created.id, stage, number, SHA, 'success', self.fixture.finished, f'https://github.com/run/{number}'))
        self.write('UPDATE stabilize_runtime SET ref_error=? WHERE campaign_id=?', ('start failed ' + 'd' * 64, created.id))
        result = releases.get(created.id)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.stage, 'complete')
        self.assertEqual(result.requested_ref, 'tag')
        self.assertEqual(result.issues[0].number, 555)
        self.assertEqual(result.gate_merges[0].pr_number, 999)
        self.assertEqual(result.latest_runs['push'].model_dump(), {'sha': SHA, 'url': 'https://github.com/run/1', 'conclusion': 'success'})
        self.assertEqual(result.latest_runs['hourly'].sha, SHA)
        self.assertIsNone(result.latest_runs['nightly'])
        self.assertEqual(result.branch_url, f'https://github.com/{monitor.REPO_NAME}/tree/{created.branch}')
        self.assertEqual(result.slack_thread_url, 'https://app.slack.com/archives/C123/p123456')
        self.assertNotIn('d' * 64, result.start_error)
        self.assertIsNotNone(result.created_at)
        self.assertIsNotNone(result.updated_at)
        self.assertIsNotNone(result.finished_at)
        self.assertEqual(releases.list_campaigns()[0].model_dump(), result.model_dump())
        with mock.patch.object(stabilize, 'ensure_ref', side_effect=monitor.CommandError('Bearer ghp_fixture_error')):
            starting = releases.create(SHA, 'fixture-user', 'ambiguous')
        self.assertEqual(starting.status, 'starting')
        self.assertIsNone(starting.tip_sha)
        self.assertNotIn('ghp_fixture_error', starting.start_error)

    def test_real_read_paths_are_readonly_and_only_resolve_uses_github(self):
        created = self.create()
        before = monitor.DB_PATH.read_bytes()
        statements, opened = [], []
        real_connect = sqlite3.connect
        def ro_connect(path, *args, **kwargs):
            self.assertTrue(str(path).endswith('?mode=ro'))
            self.assertTrue(kwargs.get('uri'))
            conn = real_connect(path, *args, **kwargs)
            conn.set_trace_callback(statements.append)
            read_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
            conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_OK if action in read_actions else sqlite3.SQLITE_DENY)
            opened.append(conn)
            return conn
        try:
            with mock.patch.object(sqlite3, 'connect', side_effect=ro_connect), \
                 mock.patch.object(monitor, 'connect_db', side_effect=AssertionError('migration prohibited')), \
                 mock.patch.object(stabilize, 'ensure_schema', side_effect=AssertionError('migration prohibited')):
                self.assertEqual(releases.get(created.id).id, created.id)
                self.assertEqual(releases.list_campaigns()[0].id, created.id)
            def gh_read(args, **kwargs):
                self.assertNotIn('--method', args)
                if args[0] == 'api':
                    if 'compare/' in args[1]:
                        return json.dumps({'status': 'ahead'})
                    return json.dumps({'sha': HEAD if args[1].endswith('/master') else SHA,
                                       'commit': {'message': 'Anchor subject\nDetails', 'committer': {'date': self.fixture.created}}})
                return json.dumps([{'databaseId': 1, 'headSha': SHA, 'headBranch': monitor.BRANCH,
                                   'event': 'push', 'status': 'completed', 'conclusion': 'success', 'url': 'https://github.com/run/1'}])
            with mock.patch.object(sqlite3, 'connect', side_effect=ro_connect), \
                 mock.patch.object(monitor, 'connect_db', side_effect=AssertionError('migration prohibited')), \
                 mock.patch.object(stabilize, 'ensure_schema', side_effect=AssertionError('migration prohibited')), \
                 mock.patch.object(monitor, 'run_gh', side_effect=gh_read), mock.patch.object(monitor, 'git_cache', side_effect=AssertionError('cache writes prohibited')):
                resolution = releases.resolve('tag')
            self.assertEqual(resolution.sha, SHA)
            self.assertEqual(resolution.subject, 'Anchor subject')
            self.assertTrue(resolution.on_master)
            self.assertEqual(resolution.push_ci.run_id, 1)
            self.assertEqual(resolution.live_campaign_id, created.id)
            self.assertTrue(statements)
            self.assertEqual(monitor.DB_PATH.read_bytes(), before)
            with mock.patch.object(monitor, 'run_gh', side_effect=monitor.CommandError('HTTP 404 Not Found')):
                with self.assertRaises(db.ApiError) as caught:
                    releases.resolve('unknown')
            self.assertEqual(caught.exception.status_code, 400)
        finally:
            for conn in opened:
                conn.close()
        for external in self.fixture.external:
            external.assert_not_called()

    def test_real_locktimeout_mapping_for_all_mutations(self):
        # Real engine methods and real LockTimeout; force the lock seam to avoid a 120-second wait.
        for action in [lambda: releases.create(SHA, 'fixture-user', 'key'),
                       lambda: releases.abort('missing', 'fixture-user'), lambda: releases.resume('missing', 'fixture-user')]:
            with mock.patch.object(stabilize, 'lock', side_effect=stabilize.LockTimeout('busy')), \
                 mock.patch.object(monitor, 'connect_db') as connect, self.assertRaises(db.ApiError) as caught:
                action()
            connect.assert_not_called()
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(caught.exception.detail['code'], 'release_busy')
            self.assertIn('retry', caught.exception.detail['message'])
            self.assertEqual(caught.exception.headers, {'Retry-After': '5'})

    def test_real_allowlist_and_batch_targets_and_stabilize_health(self):
        self.write("UPDATE automerge_batches SET target_ref='stabilize/example' WHERE batch_id='primary'")
        generator = db.connection()
        conn = next(generator)
        try:
            summaries = {item.batch_id: item for item in data.batches(conn).items}
            self.assertEqual(summaries['primary'].target_ref, 'stabilize/example')
            self.assertEqual(summaries['direct'].target_ref, 'master')
            self.assertEqual(data.detail(conn, 'primary').target_ref, 'stabilize/example')
            health = {entry.name: entry for entry in data.health(conn).entry_points}
            self.assertEqual(health['stabilize'].state, 'unknown')
            tables = {table.name: {c.name for c in table.columns} for table in db.schema(conn).tables}
            self.assertIn('target_ref', tables['automerge_batches'])
            self.assertIn('pr_head_sha', tables['stabilize_issues'])
            self.assertIn('last_seen_run_url', tables['stabilize_failures'])
            self.assertIn('gate_branch', tables['stabilize_gate_merges'])
            self.assertNotIn('pause_reason', tables['stabilize_campaigns'])
            self.assertNotIn('requested_by', tables['stabilize_campaigns'])
            self.assertNotIn('verdict_status_description', tables['automerge_batches'])
            self.assertNotIn('diagnosis', tables['stabilize_failures'])
            private = {'stabilize_runtime', 'stabilize_dispatches', 'stabilize_runs', 'stabilize_issue_identities',
                       'stabilize_fix_commits', 'stabilize_messages', 'stabilize_writes', 'stabilize_gate_intents'}
            self.assertFalse(private & tables.keys())
            for table in ['stabilize_campaigns', 'stabilize_issues', 'stabilize_gate_merges', 'stabilize_failures']:
                actual = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
                self.assertTrue(tables[table] <= actual)
        finally:
            generator.close()
        self.write("INSERT INTO entrypoint_heartbeats(entry_point,last_finished_at,last_outcome) VALUES ('stabilize',?,'ok')", (self.fixture.finished,))
        generator = db.connection()
        conn = next(generator)
        try:
            self.assertEqual(next(e for e in data.health(conn).entry_points if e.name == 'stabilize').state, 'ok')
        finally:
            generator.close()
        self.write('ALTER TABLE automerge_batches DROP COLUMN target_ref')
        generator = db.connection()
        conn = next(generator)
        try:
            self.assertTrue(all(batch.target_ref == 'master' for batch in data.batches(conn).items))
        finally:
            generator.close()




@unittest.skipIf(OPTIONAL_IMPORT_ERROR is not None, OPTIONAL_IMPORT_ERROR or '')
class IdentityTests(unittest.TestCase):
    def setUp(self):
        auth.TOKEN_CACHE.clear()
        auth.PERMISSION_CACHE.clear()

    def response(self, payload, status=200):
        import httpx
        return httpx.Response(status, json=payload, request=httpx.Request('GET', 'https://api.github.com/user'))

    def test_bearer_cache_stores_only_digest_and_login(self):
        token = 'ghp_never_store_raw_fixture'
        with mock.patch.object(auth.httpx, 'Client') as factory:
            get = factory.return_value.__enter__.return_value.get
            get.return_value = self.response({'login': 'fixture-user'})
            self.assertEqual(auth.github_user(token), 'fixture-user')
            self.assertEqual(auth.github_user(token), 'fixture-user')
            self.assertEqual(get.call_count, 1)
        self.assertNotIn(token, repr(auth.TOKEN_CACHE))
        self.assertEqual(set(auth.TOKEN_CACHE), {hashlib.sha256(token.encode()).hexdigest()})

    def test_permissions_use_app_token_and_cache(self):
        for key, level in [('push', 'write'), ('maintain', 'maintain'), ('admin', 'admin')]:
            auth.PERMISSION_CACHE.clear()
            with mock.patch.object(monitor, 'github_app_token', return_value='installation-fixture') as token, mock.patch.object(auth.httpx, 'Client') as factory:
                get = factory.return_value.__enter__.return_value.get
                get.return_value = self.response({'user': {'permissions': {key: True}}})
                self.assertEqual(auth.permission('fixture-user'), level)
                self.assertEqual(auth.permission('fixture-user'), level)
                self.assertEqual(get.call_count, 1)
                token.assert_called_once_with()
                self.assertEqual(get.call_args.kwargs['headers']['Authorization'], 'Bearer installation-fixture')
        with mock.patch.object(monitor, 'github_app_token', return_value=None), mock.patch.object(auth.httpx, 'Client') as factory:
            auth.PERMISSION_CACHE.clear()
            with self.assertRaises(auth.ApiError) as caught:
                auth.permission('fixture-user')
            self.assertEqual(caught.exception.status_code, 503)
            factory.assert_not_called()

    def test_identity_lookup_timeout_and_cache_expiry(self):
        import httpx
        with mock.patch.object(auth.httpx, 'Client') as factory:
            get = factory.return_value.__enter__.return_value.get
            get.side_effect = httpx.ReadTimeout('fixture provider timeout')
            with self.assertRaises(auth.ApiError) as caught:
                auth.github_user('ghp_timeout_fixture')
            self.assertEqual(caught.exception.status_code, 503)
            get.side_effect = None
            get.return_value = self.response({'login': 'fixture-user'})
            auth.github_user('ghp_expired_fixture')
            fingerprint = hashlib.sha256(b'ghp_expired_fixture').hexdigest()
            auth.TOKEN_CACHE[fingerprint] = (0, 'old-user')
            self.assertEqual(auth.github_user('ghp_expired_fixture'), 'fixture-user')
            get.return_value = self.response({'message': 'bad credentials'}, 401)
            with self.assertRaises(auth.ApiError) as caught:
                auth.github_user('ghp_invalid_fixture')
            self.assertEqual(caught.exception.status_code, 401)
            self.assertNotIn(hashlib.sha256(b'ghp_invalid_fixture').hexdigest(), auth.TOKEN_CACHE)


if __name__ == '__main__':
    unittest.main()
