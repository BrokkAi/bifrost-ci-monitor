"""Git-only reads against disposable repositories; no GitHub or credentials."""
import json
from pathlib import Path
import subprocess
import tempfile
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
        with mock.patch.object(g, 'launch_worker', side_effect=AssertionError('unexpected network read')):
            self.assertTrue(self.cache.ancestor(self.base, one, self.fail))
            self.assertFalse(self.cache.ancestor(two, one, self.fail))
        self.assertEqual(self.cache.command('rev-parse', 'refs/pull/7/head').stdout.strip(), one)

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
            with self.assertRaisesRegex(OSError, '^remote Git branch could not be read$'):
                g.remote_head('owner/repo', 'master', lambda: 'private-test-token')


if __name__ == '__main__':
    unittest.main()
