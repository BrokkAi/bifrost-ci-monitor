"""Attest scope, provenance, and retryable CI evidence intake."""
import base64
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import attest_ci
import attest_store
import mm_service
from test_automerge import make_db, pull, BASE_SHA, HEAD_ONE
import automerge


class AttestIntegrationTests(unittest.TestCase):
    def test_proxy_authenticates_batch_before_cloud_access(self):
        conn = make_db()
        with mock.patch.object(attest_store, 'request') as cloud:
            with self.assertRaises(ValueError):
                mm_service.dispatch(conn, 'missing', 'attest', {'operation': 'list'})
            cloud.assert_not_called()
        automerge.create_batch(conn, [pull(7, HEAD_ONE)], BASE_SHA, batch_id='attest-test')
        with mock.patch.object(attest_store, 'request', return_value={'objects': []}) as cloud:
            self.assertEqual(mm_service.dispatch(conn, 'attest-test', 'attest', {'operation': 'list'}),
                             {'objects': []})
            cloud.assert_called_once_with({'operation': 'list'})

    def test_foreign_runs_and_ordinary_prs_cannot_publish_evidence(self):
        conn = make_db()
        automerge.create_batch(conn, [pull(7, HEAD_ONE)], BASE_SHA, batch_id='attest-test')
        run = {'head_repository': {'full_name': automerge.REPO_NAME}, 'event': 'pull_request',
               'head_sha': HEAD_ONE, 'head_branch': 'source'}
        conn.execute("UPDATE automerge_batches SET ci_head_sha=NULL, ready_json='{}'")
        self.assertFalse(attest_ci.trusted_run(conn, run, automerge.REPO_NAME))
        conn.execute("UPDATE automerge_batches SET ci_head_sha=?", (HEAD_ONE,))
        self.assertTrue(attest_ci.trusted_run(conn, run, automerge.REPO_NAME))
        run['head_repository']['full_name'] = 'someone/fork'
        self.assertFalse(attest_ci.trusted_run(conn, run, automerge.REPO_NAME))

    def test_deferred_import_retries_without_duplicate_publication(self):
        import monitor
        conn = make_db()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'storage.toml'; config.write_text('configured')
            run = {'id': 10, 'run_attempt': 1, 'head_repository': {'full_name': monitor.REPO_NAME},
                   'event': 'push', 'head_branch': 'master', 'head_sha': HEAD_ONE}
            bundle = {'repository': 'bifrost-dev', 'source': {'head': HEAD_ONE, 'tree': BASE_SHA, 'clean': True},
                      'suite': 'unit', 'run_id': 'a' * 32}
            def gh(args, **kwargs):
                if args[0] == 'run':
                    root = Path(args[args.index('--dir') + 1]); (root / 'result.json').write_text(json.dumps(bundle))
                    return ''
                if 'artifacts' in args[1]:
                    return json.dumps({'artifacts': [{'name': 'attest-results-rust-host-1', 'expired': False}]})
                return json.dumps({'workflow_runs': [run]})
            with mock.patch.dict('os.environ', ATTEST_STORAGE_CONFIG=str(config)), \
                    mock.patch.object(monitor, 'run_gh', side_effect=gh), \
                    mock.patch.object(attest_store, 'request', side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError): attest_ci.tick(conn)
                self.assertEqual(conn.execute('SELECT status FROM attest_ci_imports').fetchone()[0], 'pending')
            original_gh = gh
            def gh(args, **kwargs):
                if args[0] == 'api' and 'artifacts' not in args[1]:
                    return json.dumps({'workflow_runs': []})  # The failed attempt left discovery.
                return original_gh(args, **kwargs)
            with mock.patch.dict('os.environ', ATTEST_STORAGE_CONFIG=str(config)), \
                    mock.patch.object(monitor, 'run_gh', side_effect=gh), \
                    mock.patch.object(attest_store, 'request', return_value={'reference': 's3://bucket/result'}) as put:
                attest_ci.tick(conn); attest_ci.tick(conn)
                put.assert_called_once()
                sent = put.call_args.args[0]
                self.assertEqual(json.loads(gzip.decompress(base64.b64decode(sent['data']))), bundle)
                self.assertEqual(conn.execute('SELECT status FROM attest_ci_imports').fetchone()[0], 'done')

    def test_discovery_reaches_results_beyond_twenty_and_preserves_legacy_receipts(self):
        import monitor
        conn = make_db()
        conn.execute('CREATE TABLE attest_ci_imports (run_id INTEGER,attempt INTEGER,status TEXT,'
                     'updated_at TEXT,objects_json TEXT,PRIMARY KEY(run_id,attempt))')
        conn.execute("INSERT INTO attest_ci_imports VALUES (9,1,'done','old','[\"original\"]')")
        run = {'id': 10, 'run_attempt': 1, 'head_repository': {'full_name': monitor.REPO_NAME},
               'event': 'push', 'head_branch': 'master', 'head_sha': HEAD_ONE,
               'unreviewed_payload': 'must not persist'}
        foreign = dict(run, head_repository={'full_name': 'someone/fork'})
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'storage.toml'; config.touch()
            def gh(args, **kwargs):
                if 'artifacts' in args[1]: return json.dumps({'artifacts': []})
                self.assertIn('per_page=100', args[1])
                return json.dumps({'workflow_runs': [foreign] * 24 + [run]})
            with mock.patch.dict('os.environ', ATTEST_STORAGE_CONFIG=str(config)), \
                    mock.patch.object(monitor, 'run_gh', side_effect=gh):
                attest_ci.tick(conn)
        self.assertEqual(conn.execute('SELECT objects_json FROM attest_ci_imports WHERE run_id=9').fetchone()[0],
                         '["original"]')
        saved = conn.execute('SELECT status,run_json FROM attest_ci_imports WHERE run_id=10').fetchone()
        self.assertEqual(saved[0], 'done')
        self.assertNotIn('unreviewed_payload', json.loads(saved[1]))

    def test_discovery_checkpoints_pages_and_revisits_older_completion_days(self):
        import monitor
        conn = make_db(); attest_ci.ensure_schema(conn)
        today = attest_ci.datetime.datetime.now(attest_ci.datetime.timezone.utc).date()
        run = {'id': 10, 'run_attempt': 1, 'head_repository': {'full_name': monitor.REPO_NAME},
               'event': 'push', 'head_branch': 'master', 'head_sha': HEAD_ONE}
        foreign = dict(run, head_repository={'full_name': 'someone/fork'})
        requests = []
        def gh(args, **kwargs):
            url = args[1]; requests.append(url)
            if f'created={today}&per_page=100&page=1' in url:
                return json.dumps({'workflow_runs': [foreign] * 100})
            return json.dumps({'workflow_runs': [run]})
        with mock.patch.object(monitor, 'run_gh', side_effect=gh):
            attest_ci.discover(conn)
            self.assertEqual(conn.execute('SELECT page FROM attest_ci_scan').fetchone()[0], 2)
            attest_ci.discover(conn)
            self.assertEqual(conn.execute('SELECT status FROM attest_ci_imports').fetchone()[0], 'pending')
            self.assertEqual(tuple(conn.execute('SELECT day,page FROM attest_ci_scan').fetchone()),
                             ((today - attest_ci.datetime.timedelta(days=1)).isoformat(), 1))
            attest_ci.discover(conn)
        self.assertTrue(any(f'created={today - attest_ci.datetime.timedelta(days=1)}' in u for u in requests))
        self.assertEqual(conn.execute('SELECT count(*) FROM attest_ci_imports').fetchone()[0], 1)

    def test_failed_pending_import_does_not_starve_another_attempt(self):
        import monitor
        conn = make_db(); attest_ci.ensure_schema(conn)
        for identifier in (10, 11):
            run = {'id': identifier, 'run_attempt': 1, 'event': 'push', 'head_branch': 'master',
                   'head_sha': HEAD_ONE, 'head_repository': {'full_name': monitor.REPO_NAME}}
            conn.execute("INSERT INTO attest_ci_imports VALUES (?,1,'pending','2000-01-01T00:00:00+00:00','[]',?)",
                         (identifier, json.dumps(run)))
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'storage.toml'; config.touch()
            def gh(args, **kwargs):
                if '/10/artifacts' in args[1]: raise RuntimeError('unavailable')
                if 'artifacts' in args[1]: return json.dumps({'artifacts': []})
                return json.dumps({'workflow_runs': []})
            with mock.patch.dict('os.environ', ATTEST_STORAGE_CONFIG=str(config)), \
                    mock.patch.object(monitor, 'run_gh', side_effect=gh):
                with self.assertRaises(RuntimeError): attest_ci.tick(conn)
                attest_ci.tick(conn)
        self.assertEqual([tuple(r) for r in conn.execute('SELECT run_id,status FROM attest_ci_imports ORDER BY run_id')],
                         [(10, 'pending'), (11, 'done')])


if __name__ == '__main__':
    unittest.main()
