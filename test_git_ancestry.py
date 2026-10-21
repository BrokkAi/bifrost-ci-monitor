"""Git-only reads against disposable repositories; no GitHub or credentials."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import automerge as a
import git_ancestry as g
import monitor


class GitReads(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'master')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'uploadpack.allowFilter', 'true')
        self.git('config', 'uploadpack.allowAnySHA1InWant', 'true')
        self.put('docs/old name.md', 'identical text\n')
        self.put('scripts/public/ci-impact.mjs', "export function classifyChangeSet({changedPaths}) {"
                 "return {mode: changedPaths.some(p=>p.endsWith('.rs'))?'impact':'docs', "
                 "selected: new Set(), paths: changedPaths};}")
        self.base = self.commit('base')
        self.cache = g.Cache(self.root / 'cache', str(self.repo), lambda: None)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL,
                                       text=True).strip()

    def put(self, path, text):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def warm(self, heads, kind='commit'):
        if not self.cache.prepare(heads, kind=kind):
            self.assertTrue(self.cache.wait_for_fetch(10))
            self.cache.worker.wait(timeout=10)
        self.assertTrue(self.cache.prepare(heads, kind=kind))
        self.assertEqual(self.cache.missing(heads, kind=kind), [])

    def test_bulk_ref_fetch_populates_other_tips_and_full_parents(self):
        self.git('checkout', '-qb', 'feature')
        self.put('one.rs', 'one')
        one = self.commit('one')
        self.git('update-ref', 'refs/pull/7/head', one)
        self.git('checkout', '-q', 'master')
        self.put('two.rs', 'two')
        two = self.commit('two')
        self.warm([two])
        self.assertEqual(self.cache.missing([one, two, self.base]), [])
        self.assertFalse((self.cache.git_dir / 'shallow').exists())
        info = self.cache.git_dir / 'objects/info'
        self.assertTrue((info / 'commit-graph').exists()
                        or (info / 'commit-graphs/commit-graph-chain').exists())
        self.assertEqual(self.cache.command('commit-graph', 'verify').returncode, 0)
        with mock.patch.object(g, 'launch_worker', side_effect=AssertionError('unexpected network read')):
            self.assertTrue(self.cache.ancestor(self.base, one, self.fail))
            self.assertFalse(self.cache.ancestor(two, one, self.fail))
        self.assertEqual(self.cache.command('rev-parse', 'refs/pull/7/head').stdout.strip(), one)

    def test_many_ancestry_pairs_share_one_complete_walk(self):
        self.put('first.rs', 'first')
        first = self.commit('first')
        self.put('second.rs', 'second')
        second = self.commit('second')
        self.git('checkout', '-qb', 'diverged', self.base)
        self.put('other.rs', 'other')
        other = self.commit('other')
        self.warm([first, second, other])
        with mock.patch.object(self.cache, 'command', wraps=self.cache.command) as command:
            self.assertTrue(self.cache.ancestor(first, second, self.fail))
            self.assertTrue(self.cache.ancestor(self.base, second, self.fail))
            self.assertFalse(self.cache.ancestor(other, second, self.fail))
        self.assertEqual([call.args for call in command.call_args_list],
                         [('rev-list', second, '--')])
        # The same immutable receipts remain readable by a different process.
        other_cache = g.Cache(self.root / 'cache', str(self.repo), lambda: None)
        with mock.patch.object(other_cache, 'command', side_effect=AssertionError('recomputed pair')):
            self.assertFalse(other_cache.ancestor(other, second, self.fail))

    def test_reachability_matches_git_across_merge_and_divergent_history(self):
        self.git('checkout', '-qb', 'feature')
        self.put('feature.rs', 'feature')
        feature = self.commit('feature')
        self.git('checkout', '-q', 'master')
        self.put('master.rs', 'master')
        master = self.commit('master')
        self.git('merge', '--no-ff', '-qm', 'merge feature', 'feature')
        merged = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-qb', 'unmerged', self.base)
        self.put('unmerged.rs', 'unmerged')
        unmerged = self.commit('unmerged')
        heads = [self.base, feature, master, merged, unmerged]
        self.warm(heads)
        for descendant in heads:
            for ancestor in heads:
                expected = subprocess.run(
                    ['git', '-C', str(self.repo), 'merge-base', '--is-ancestor', ancestor, descendant],
                    check=False).returncode == 0
                with self.subTest(ancestor=ancestor, descendant=descendant):
                    self.assertEqual(self.cache.ancestor(ancestor, descendant, self.fail), expected)
        self.assertLessEqual(len(self.cache._reachable_commits), 4)

    def test_failed_or_incomplete_walk_never_produces_a_negative_fact(self):
        self.put('next.rs', 'next')
        head = self.commit('next')
        self.warm([self.base, head])
        failures = [subprocess.CompletedProcess([], 128, head + '\n'),
                    subprocess.CompletedProcess([], 0, self.base + '\n'),
                    subprocess.CompletedProcess([], 0, head + '\ninvalid\n')]
        for result in failures:
            with self.subTest(result=result), mock.patch.object(self.cache, 'command', return_value=result):
                with self.assertRaisesRegex(RuntimeError, 'ancestry could not be established'):
                    self.cache.ancestor(self.base, head, self.fail)
            self.assertNotIn(head, self.cache._reachable_commits)
            self.assertFalse(list(self.cache.directory.glob('value-*.json')))
        with mock.patch.object(self.cache, 'command', side_effect=g.Deferred('unfinished')):
            with self.assertRaises(g.Deferred):
                self.cache.ancestor(self.base, head, self.fail)
        self.assertTrue(self.cache.ancestor(self.base, head, self.fail))

    def test_positive_object_presence_is_cached_by_type_but_missing_is_not(self):
        self.warm([self.base])
        with mock.patch.object(self.cache, 'command', wraps=self.cache.command) as command:
            self.assertEqual(self.cache.missing([self.base]), [])
            self.assertEqual(self.cache.missing([self.base], kind='blob'), [self.base])
            self.assertEqual(self.cache.missing([self.base], kind='blob'), [self.base])
        self.assertEqual(len(command.call_args_list), 2)
        self.put('later.rs', 'later')
        later = self.commit('later')
        self.assertEqual(self.cache.missing([later]), [later])
        self.warm([later])
        self.assertEqual(self.cache.missing([later]), [])

    def test_fresh_remote_head_and_behind_count_for_diverged_heads(self):
        self.git('checkout', '-qb', 'feature')
        self.put('feature', 'feature')
        feature = self.commit('feature')
        self.git('checkout', '-q', 'master')
        self.put('master', '1')
        old = self.commit('master one')
        self.warm([old, feature])
        self.assertEqual(self.cache.behind(feature, old), 1)
        self.assertEqual(g.remote_head(str(self.repo), 'master', lambda: None), old)
        self.put('master', '2')
        new = self.commit('master two')
        self.assertEqual(g.remote_head(str(self.repo), 'master', lambda: None), new)
        self.warm([new, feature])
        self.assertEqual(self.cache.behind(feature, new), 2)
        self.assertEqual(self.cache.behind(feature, old), 1)

    def test_changed_paths_include_rename_endpoints_and_more_than_300_files(self):
        self.git('mv', 'docs/old name.md', 'docs/new name\n界.md')
        for number in range(305):
            self.put(f'docs/file {number}.md', 'documentation')
        head = self.commit('docs')
        self.warm([head])
        paths = self.cache.changed_paths(self.base, head)
        self.assertEqual(len(paths), 307)
        self.assertIn('docs/old name.md', paths)
        self.assertIn('docs/new name\n界.md', paths)
        with mock.patch.object(g, 'launch_worker', side_effect=AssertionError('unexpected blob fetch')):
            self.assertEqual(self.cache.changed_paths(self.base, head), paths)

    def test_changed_paths_match_merge_base_semantics(self):
        self.git('checkout', '-qb', 'feature')
        self.put('docs/feature.md', 'feature')
        head = self.commit('feature')
        self.git('checkout', '-q', 'master')
        self.put('unrelated.rs', 'master-only')
        master = self.commit('master')
        self.warm([head, master])
        self.assertEqual(self.cache.changed_paths(master, head), ['docs/feature.md'])

    def test_pinned_file_fetches_only_its_blob_and_reuses_it(self):
        self.put('scripts/public/ci-impact.mjs', 'new classifier')
        head = self.commit('new classifier')
        self.warm([head])
        blob = self.git('rev-parse', self.base + ':scripts/public/ci-impact.mjs')
        self.assertEqual(self.cache.missing([blob], kind='blob'), [blob])
        with self.assertRaises(g.Deferred):
            self.cache.file(self.base, 'scripts/public/ci-impact.mjs')
        self.assertTrue(self.cache.wait_for_fetch(10))
        self.cache.worker.wait(timeout=10)
        self.assertIn('classifyChangeSet', self.cache.file(self.base, 'scripts/public/ci-impact.mjs'))
        with mock.patch.object(g, 'launch_worker', side_effect=AssertionError('unexpected fetch')):
            self.assertIn('classifyChangeSet', self.cache.file(self.base, 'scripts/public/ci-impact.mjs'))

    def test_results_reused_by_another_client_and_failures_are_not_cached(self):
        self.warm([self.base])
        tree = self.cache.tree(self.base)
        other = g.Cache(self.root / 'cache', str(self.repo), lambda: None)
        with mock.patch.object(other, 'command', side_effect=AssertionError('recomputed tree')):
            self.assertEqual(other.tree(self.base), tree)
        with self.assertRaises(OSError):
            self.cache.memo(['error'], lambda: (_ for _ in ()).throw(OSError('failed read')))
        self.assertEqual(other.memo(['error'], lambda: 'complete'), 'complete')

    def test_concurrent_clients_adopt_pending_request(self):
        other = g.Cache(self.root / 'cache', str(self.repo), lambda: None)
        with mock.patch.object(g, 'launch_worker') as launch:
            self.assertFalse(self.cache.prepare([self.base]))
            self.assertFalse(other.prepare([self.base]))
        launch.assert_called_once()

    def test_network_failures_defer_and_do_not_fan_out_to_rest(self):
        with mock.patch.object(g, 'launch_worker'):
            self.assertFalse(self.cache.prepare([self.base]))
        job = json.loads((self.cache.directory / 'fetch.json').read_text())
        g.atomic_json(self.cache.directory / (job['id'] + '.json'), {'ok': False, 'unavailable': []})
        fallback = mock.Mock()
        with self.assertRaises(g.Deferred):
            self.cache.ancestor(self.base, self.base, fallback)
        fallback.assert_not_called()
        self.assertFalse(list(self.cache.directory.glob('value-*.json')))

    def test_confirmed_unavailable_tip_falls_back_once_and_reuses_the_fact(self):
        with mock.patch.object(g, 'launch_worker'):
            self.assertFalse(self.cache.prepare([self.base]))
        job = json.loads((self.cache.directory / 'fetch.json').read_text())
        g.atomic_json(self.cache.directory / (job['id'] + '.json'),
                      {'ok': False, 'unavailable': [self.base]})
        fallback = mock.Mock(return_value=False)
        self.assertFalse(self.cache.ancestor(self.base, self.base, fallback))
        self.assertFalse(self.cache.ancestor(self.base, self.base, fallback))
        fallback.assert_called_once()

    def test_worker_accepts_old_request_format(self):
        job = {'id': 'old-request', 'repository': str(self.repo), 'heads': [self.base], 'started': 0}
        request = self.cache.directory / 'old-request.request.json'
        g.atomic_json(request, job)
        with mock.patch.dict('os.environ', {'GIT_NO_LAZY_FETCH': '1'}):
            g.fetch_worker(request)
        self.assertEqual(json.loads((self.cache.directory / 'old-request.json').read_text()),
                         {'ok': True, 'unavailable': []})
        self.assertEqual(self.cache.missing([self.base]), [])
        self.assertFalse(request.exists())

    def test_read_only_does_not_initialize_cache_fetch_or_save_comparisons(self):
        directory = self.root / 'absent'
        with g.read_only(), mock.patch.object(g, 'launch_worker') as launch:
            cache = g.Cache(directory, str(self.repo), lambda: None)
            self.assertFalse(cache.ancestor(self.base, self.base, lambda *_: False))
            self.assertEqual(g.remote_head(str(self.repo), 'master', lambda: None), self.base)
        launch.assert_not_called()
        self.assertFalse(directory.exists())
        self.warm([self.base])
        before = set(self.cache.directory.iterdir())
        with g.read_only():
            self.cache.tree(self.base)
        self.assertEqual(set(self.cache.directory.iterdir()), before)

    def test_classifier_uses_git_only_and_cross_batch_cache(self):
        self.git('mv', 'docs/old name.md', 'docs/new.md')
        head = self.commit('docs')
        self.warm([head])
        blob = self.git('rev-parse', self.base + ':scripts/public/ci-impact.mjs')
        self.warm([blob], 'blob')
        with (mock.patch.object(a, '_ancestry_cache', return_value=self.cache),
              mock.patch.object(a, 'gh_json', side_effect=AssertionError('REST read')),
              mock.patch.object(a, 'run_gh', side_effect=AssertionError('REST read'))):
            result = a.run_ci_impact(self.base, [head])
            self.assertEqual(result['mode'], 'docs')
            self.assertEqual(result['paths'], ['docs/new.md', 'docs/old name.md'])
            with mock.patch.object(monitor, 'run_command', side_effect=AssertionError('reran classifier')):
                self.assertEqual(a.run_ci_impact(self.base, [head]), result)

    def test_tree_and_ancestry_callers_use_shared_cache_outside_cron(self):
        self.warm([self.base])
        with (mock.patch.object(a, '_ancestry_cache', return_value=self.cache),
              mock.patch.object(a, 'SUPERVISOR_RUNNING', False),
              mock.patch.object(a, 'gh_json', side_effect=AssertionError('REST read'))):
            self.assertEqual(a.commit_tree_sha(self.base), self.git('rev-parse', self.base + '^{tree}'))
            self.assertTrue(a.compare_commit_ancestry(self.base, self.base))

    def test_classifier_imports_transitive_helpers_from_base_not_candidate(self):
        # Hard-won: the lookahead's pinned classifier imported a checksum
        # helper, but only the entry module was materialized for Node.
        self.put('scripts/public/ci-impact.mjs', '''
            import { classifyChangeSet } from './helpers/export.mjs';
            export { classifyChangeSet };
        ''')
        self.put('scripts/public/helpers/export.mjs',
                 "export { classifyChangeSet } from '../sync-release-checksums.mjs';")
        self.put('scripts/public/sync-release-checksums.mjs',
                 "export function classifyChangeSet() { return {mode:'docs', selected:new Set()}; }")
        base = self.commit('base imports helpers')
        self.put('scripts/public/sync-release-checksums.mjs', 'throw new Error("candidate helper");')
        head = self.commit('candidate changes helper')
        self.warm([base, head])
        for path in ['scripts/public/ci-impact.mjs', 'scripts/public/helpers/export.mjs',
                     'scripts/public/sync-release-checksums.mjs']:
            self.warm([self.git('rev-parse', base + ':' + path)], 'blob')
        with mock.patch.object(a, '_ancestry_cache', return_value=self.cache):
            self.assertEqual(a.run_ci_impact(base, [head])['mode'], 'docs')
            with mock.patch.object(monitor, 'run_command', side_effect=AssertionError('reran Node')):
                self.assertEqual(a.run_ci_impact(base, [head])['mode'], 'docs')

        self.put('scripts/public/ci-impact.mjs', "import './missing.mjs';")
        missing_base = self.commit('missing classifier dependency')
        self.warm([missing_base])
        self.warm([self.git('rev-parse', missing_base + ':scripts/public/ci-impact.mjs')], 'blob')
        with mock.patch.object(a, '_ancestry_cache', return_value=self.cache):
            self.assertEqual(a.run_ci_impact(missing_base, [missing_base])['mode'], 'unknown')

    def test_remote_auth_is_environment_only_and_error_output_is_withheld(self):
        result = subprocess.CompletedProcess([], 0, self.base + '\trefs/heads/master\n', '')
        with mock.patch.object(g.subprocess, 'run', side_effect=[mock.Mock(returncode=0), result]) as run:
            self.assertEqual(g.remote_head('owner/repo', 'master', lambda: 'private-test-token'), self.base)
        self.assertNotIn('private-test-token', str(run.call_args.args))
        self.assertNotIn('private-test-token', str(run.call_args.kwargs.get('input')))
        self.assertIn('AUTHORIZATION: basic ', str(run.call_args.kwargs['env']))
        result.returncode = 128
        result.stderr = 'private-test-token'
        with mock.patch.object(g.subprocess, 'run', side_effect=[mock.Mock(returncode=0), result]):
            with self.assertRaisesRegex(OSError, r'^remote Git branch could not be read \(unknown\) \[exit=128\]$'):
                g.remote_head('owner/repo', 'master', lambda: 'private-test-token')

    def test_remote_auth_failure_renews_once_and_reads_a_fresh_head(self):
        rejected = subprocess.CompletedProcess([], 128, '', 'Authentication failed: private-old-token')
        accepted = subprocess.CompletedProcess([], 0, self.base + '\trefs/heads/master\n', '')
        refresh = mock.Mock(return_value='private-new-token')
        with mock.patch.object(g.subprocess, 'run', side_effect=[mock.Mock(returncode=0), rejected, accepted]) as run:
            self.assertEqual(g.remote_head('owner/repo', 'master', lambda: 'private-old-token',
                                           refresh_token=refresh), self.base)
        refresh.assert_called_once_with()
        key = 'GIT_CONFIG_VALUE_1'
        self.assertNotEqual(run.call_args_list[1].kwargs['env'][key], run.call_args_list[2].kwargs['env'][key])
        self.assertNotIn('private-new-token', str(run.call_args.args))

    def test_git_http_401_without_a_prompt_renews_once_and_withholds_the_trace(self):
        tokens = {base64.b64encode(('x-access-token:' + token).encode()).decode(): name
                  for token, name in [('private-old-token', 'old'), ('private-new-token', 'new')]}
        requests = []
        head = self.base
        def packet(value):
            return f'{len(value) + 4:04x}'.encode() + value
        class GitHTTP(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                name = tokens.get(self.headers.get('Authorization', '').removeprefix('basic '), 'missing')
                requests.append(name)
                if name != 'new':
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', 'Basic realm="fixture"')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                body = (packet(b'# service=git-upload-pack\n') + b'0000'
                        + packet((head + ' refs/heads/master\0agent=fixture\n').encode()) + b'0000')
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-git-upload-pack-advertisement')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), GitHTTP)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        original_env = g.authenticated_env
        original_run = subprocess.run
        traces = []
        def env(token):
            value = original_env(token)
            value['GIT_CONFIG_KEY_' + str(int(value['GIT_CONFIG_COUNT']) - 1)] = 'http.extraHeader'
            return value
        def run(*args, **kwargs):
            result = original_run(*args, **kwargs)
            if 'ls-remote' in args[0]:
                traces.append(result.stderr)
            return result
        refresh = mock.Mock(return_value='private-new-token')
        with (mock.patch.object(g, 'repository_url', return_value=f'http://127.0.0.1:{server.server_port}/repo.git'),
              mock.patch.object(g, 'authenticated_env', side_effect=env),
              mock.patch.object(g.subprocess, 'run', side_effect=run)):
            self.assertEqual(g.remote_head('fixture/repo', 'master', lambda: 'private-old-token',
                                           refresh_token=refresh), head)
        self.assertEqual(requests, ['old', 'new'])
        refresh.assert_called_once_with()
        self.assertEqual([g.remote_http_status(subprocess.CompletedProcess([], 0, '', t))
                          for t in traces], [401, 200])
        for trace in traces:
            for secret in ['private-old-token', 'private-new-token', *tokens]:
                self.assertNotIn(secret, trace)

    def test_remote_failure_categories_withhold_secrets_and_retry_only_authentication(self):
        cases = [('Authentication failed', 'authentication'),
                 ('The requested URL returned error: 401', 'authentication'),
                 ('API rate limit exceeded', 'rate_limit'),
                 ('The requested URL returned error: 403', 'repository_access'),
                 ('Repository not found', 'repository_access'),
                 ('Could not resolve host', 'transport'),
                 ('GnuTLS connection terminated', 'transport'),
                 ('RPC failed; curl 92 HTTP/2 stream 3 was not closed cleanly: INTERNAL_ERROR', 'transport'),
                 ('Empty reply from server', 'transport'),
                 ('http.c:1 == Info: Connection left intact\nhttp.c:1 <= Recv header: HTTP/2 401\n'
                  'fatal: could not read Username: terminal prompts disabled', 'authentication'),
                 ('http.c:1 <= Recv header: HTTP/2 401\nhttp.c:1 <= Recv header: HTTP/2 404',
                  'repository_access'),
                 ('http.c:1 <= Recv header: HTTP/2 429', 'rate_limit'),
                 ('http.c:1 <= Recv header: HTTP/2 503', 'transport'),
                 ('http.c:1 == Info: Connection left intact\nhttp.c:1 <= Recv header: HTTP/2 200\n'
                  'unexpected failure', 'unknown'),
                 ('remote: <= Recv header: HTTP/2 401', 'unknown'),
                 ('unexpected failure', 'unknown')]
        for detail, category in cases:
            with self.subTest(category=category, detail=detail):
                rejected = subprocess.CompletedProcess([], 128, '', detail + ' private-test-token')
                refresh = mock.Mock(return_value='fresh-test-token')
                outcomes = [mock.Mock(returncode=0), rejected]
                if category == 'authentication':
                    outcomes.append(rejected)
                with mock.patch.object(g.subprocess, 'run', side_effect=outcomes) as run:
                    with self.assertRaises(g.RemoteReadError) as error:
                        g.remote_head('owner/repo', 'master', lambda: 'private-test-token',
                                      refresh_token=refresh)
                self.assertEqual(error.exception.category, category)
                self.assertNotIn('private-test-token', str(error.exception))
                self.assertNotIn(detail, str(error.exception))
                self.assertEqual(refresh.call_count, int(category == 'authentication'))
                self.assertEqual(run.call_count, 3 if category == 'authentication' else 2)

    def test_remote_protocol_diagnostics_are_numeric_and_never_expose_stderr(self):
        for detail, diagnostics in [
                ('RPC failed; curl 92 HTTP/2 stream 3 private-test-token', {'exit': 128, 'curl': 92}),
                ('The requested URL returned error: 503 https://private-test-token@github.com',
                 {'exit': 128, 'http': 503}),
                ('unknown private-test-token curl secret returned error: token', {'exit': 128})]:
            with self.subTest(detail=detail):
                rejected = subprocess.CompletedProcess([], 128, '', detail)
                refresh = mock.Mock()
                with mock.patch.object(g.subprocess, 'run', side_effect=[mock.Mock(returncode=0), rejected]) as run:
                    with self.assertRaises(g.RemoteReadError) as error:
                        g.remote_head('owner/repo', 'master', lambda: 'private-test-token',
                                      refresh_token=refresh)
                self.assertEqual(error.exception.diagnostics, diagnostics)
                self.assertNotIn('private-test-token', str(error.exception))
                self.assertNotIn('https://', str(error.exception))
                self.assertFalse(hasattr(error.exception, 'stderr'))
                refresh.assert_not_called()
                self.assertEqual(run.call_count, 2)

    def test_remote_missing_branch_and_malformed_response_never_renew_or_return_a_head(self):
        for result, category in [(subprocess.CompletedProcess([], 2, '', ''), 'missing_branch'),
                                 (subprocess.CompletedProcess([], 0, '', ''), 'invalid_response')]:
            refresh = mock.Mock()
            with mock.patch.object(g.subprocess, 'run', side_effect=[mock.Mock(returncode=0), result]):
                with self.assertRaises(g.RemoteReadError) as error:
                    g.remote_head('owner/repo', 'master', lambda: None, refresh_token=refresh)
            self.assertEqual(error.exception.category, category)
            refresh.assert_not_called()

    def test_remote_auth_retry_shares_the_original_read_budget(self):
        clock = {'at': 0}
        def renew():
            clock['at'] = 29
            return 'fresh-test-token'
        rejected = subprocess.CompletedProcess([], 128, '', 'Authentication failed')
        accepted = subprocess.CompletedProcess([], 0, self.base + '\trefs/heads/master\n', '')
        with (mock.patch.object(g.time, 'monotonic', side_effect=lambda: clock['at']),
              mock.patch.object(g.subprocess, 'run', side_effect=[mock.Mock(returncode=0), rejected, accepted]) as run):
            self.assertEqual(g.remote_head('owner/repo', 'master', lambda: 'old-test-token',
                                           refresh_token=renew), self.base)
        self.assertEqual(run.call_args_list[1].kwargs['timeout'], 30)
        self.assertEqual(run.call_args_list[2].kwargs['timeout'], 1)

    def test_remote_renewal_spending_the_read_budget_cannot_start_another_probe(self):
        clock = {'at': 0}
        def renew():
            clock['at'] = 31
            return 'fresh-test-token'
        rejected = subprocess.CompletedProcess([], 128, '', 'Authentication failed')
        with (mock.patch.object(g.time, 'monotonic', side_effect=lambda: clock['at']),
              mock.patch.object(g.subprocess, 'run', side_effect=[mock.Mock(returncode=0), rejected]) as run):
            with self.assertRaisesRegex(OSError, 'remote Git read timed out'):
                g.remote_head('owner/repo', 'master', lambda: 'old-test-token', refresh_token=renew)
        self.assertEqual(run.call_count, 2)

    def test_remote_branch_callers_renew_through_the_same_required_app_provider(self):
        def read(repository, branch, token, *, refresh_token):
            self.assertEqual(repository, monitor.REPO_NAME)
            self.assertEqual(branch, 'master')
            self.assertEqual(token(), 'cached-test-token')
            self.assertEqual(refresh_token(), 'fresh-test-token')
            return self.base
        for caller in [monitor.remote_branch_sha, a.remote_branch_sha]:
            with (mock.patch.object(g, 'remote_head', side_effect=read),
                  mock.patch.object(monitor, 'github_app_token',
                                    side_effect=['cached-test-token', 'fresh-test-token']) as tokens):
                self.assertEqual(caller('master'), self.base)
                self.assertEqual(tokens.call_args_list, [mock.call(), mock.call(force_refresh=True)])


if __name__ == '__main__':
    unittest.main()
