"""Supervisor classification and documentation-only validation policy."""

import hashlib
import json
import subprocess
from pathlib import Path
from unittest import TestCase, mock

import automerge
import monitor
from test_automerge import BASE_SHA, HEAD_ONE, HEAD_TWO, HEAD_THREE, make_db, pull, row_for, unchanged_queue


# A small exported-function fixture tests the Node import/input contract; path
# policy itself remains owned by Bifrost rather than copied into this project.
CLASSIFIER = """
export function classifyChangeSet({eventName, changedPaths}) {
  if (eventName !== 'pull_request') throw new Error('wrong event');
  const docs = changedPaths.length > 0 && changedPaths.every(p => p.startsWith('docs/'));
  return {schemaVersion: '4', mode: docs ? 'docs' : 'impact',
          changedPaths, selected: new Set(['fixture']), reasons: ['fixture']};
}
"""
DOCS_REPORT = "automerge-local: pass\nTests run: none\nBaseline failures: none"


def impact(mode='docs', base=BASE_SHA, heads=None):
    return {'base_sha': base, 'heads': sorted(heads or [HEAD_ONE]), 'mode': mode}


def store_impact(conn, value):
    with conn:
        conn.execute('UPDATE automerge_batches SET validation_impact_json=?', (json.dumps(value),))


class AncestryCacheFake:
    """Small hand fake for the immutable reads used by run_ci_impact."""

    def __init__(self, paths, script=CLASSIFIER, *, failure=None):
        self.paths = paths
        self.script = script
        self.failure = failure
        self.required = []
        self.changed_path_calls = []
        self.file_calls = []
        self.memo_keys = []

    def require(self, heads):
        self.required.append(list(heads))
        if self.failure == 'require':
            raise OSError('required immutable objects unavailable')

    def changed_paths(self, base, head):
        self.changed_path_calls.append((base, head))
        if self.failure == 'changed_paths':
            raise OSError('changed paths unavailable')
        return self.paths[(base, head)]

    def file(self, head, path):
        self.file_calls.append((head, path))
        if self.failure == 'file':
            raise OSError('classifier file unavailable')
        return self.script

    def memo(self, key, compute):
        self.memo_keys.append(key)
        if self.failure == 'memo':
            raise OSError('memo read unavailable')
        return compute()


class ClassifierTests(TestCase):
    def test_imports_base_script_with_union_of_exact_source_diffs(self):
        cache = AncestryCacheFake({
            (BASE_SHA, HEAD_ONE): ['docs/a.md'],
            (BASE_SHA, HEAD_TWO): ['docs/b.md'],
        })
        with mock.patch.object(automerge, '_ancestry_cache', return_value=cache):
            result = automerge.run_ci_impact(BASE_SHA, [HEAD_TWO, HEAD_ONE, HEAD_ONE])
        self.assertEqual(result['mode'], 'docs')
        self.assertEqual(result['schemaVersion'], '4')
        self.assertEqual(result['heads'], [HEAD_ONE, HEAD_TWO])
        self.assertEqual(result['changedPaths'], ['docs/a.md', 'docs/b.md'])
        self.assertEqual(result['selected'], ['fixture'])
        self.assertEqual(cache.required, [[BASE_SHA, HEAD_ONE, HEAD_TWO]])
        self.assertEqual(cache.changed_path_calls, [
            (BASE_SHA, HEAD_ONE), (BASE_SHA, HEAD_TWO),
        ])
        self.assertEqual(cache.file_calls, [(BASE_SHA, 'scripts/public/ci-impact.mjs')])
        self.assertEqual(cache.memo_keys, [[
            'ci-impact-v1', BASE_SHA, [HEAD_ONE, HEAD_TWO],
            hashlib.sha256(CLASSIFIER.encode()).hexdigest(),
        ]])

    def test_mixed_sources_and_code_renamed_into_docs_do_not_skip_tests(self):
        for files in (
            ['docs/a.md', 'src/lib.rs'],
            ['docs/new.md', 'src/lib.rs'],
            [],
        ):
            with (
                self.subTest(files=files),
                mock.patch.object(
                    automerge,
                    '_ancestry_cache',
                    return_value=AncestryCacheFake({(BASE_SHA, HEAD_ONE): files}),
                ),
            ):
                result = automerge.run_ci_impact(BASE_SHA, [HEAD_ONE])
            self.assertEqual(result['mode'], 'impact')

    def test_cache_read_errors_fall_back_to_judgment(self):
        for failure in ('require', 'changed_paths', 'file', 'memo'):
            with (
                self.subTest(failure=failure),
                mock.patch.object(
                    automerge,
                    '_ancestry_cache',
                    return_value=AncestryCacheFake(
                        {(BASE_SHA, HEAD_ONE): ['docs/a.md']}, failure=failure
                    ),
                ),
                mock.patch.object(automerge, 'log'),
            ):
                self.assertEqual(automerge.run_ci_impact(BASE_SHA, [HEAD_ONE])['mode'], 'unknown')

    def test_classifier_errors_and_invalid_output_fall_back_to_judgment(self):
        for output in ('[]', '{}', '{"mode":"unexpected"}', 'invalid JSON'):
            with (
                self.subTest(output=output),
                mock.patch.object(
                    automerge,
                    '_ancestry_cache',
                    return_value=AncestryCacheFake({(BASE_SHA, HEAD_ONE): []}),
                ),
                mock.patch.object(monitor, 'run_command', return_value=output),
                mock.patch.object(automerge, 'log'),
            ):
                self.assertEqual(automerge.run_ci_impact(BASE_SHA, [HEAD_ONE])['mode'], 'unknown')
        with (
            mock.patch.object(
                automerge,
                '_ancestry_cache',
                return_value=AncestryCacheFake({(BASE_SHA, HEAD_ONE): []}),
            ),
            mock.patch.object(monitor, 'run_command', side_effect=monitor.CommandError('node timeout')),
            mock.patch.object(automerge, 'log'),
        ):
            self.assertEqual(automerge.run_ci_impact(BASE_SHA, [HEAD_ONE])['mode'], 'unknown')

    def test_cache_is_bound_to_exact_base_and_heads(self):
        conn = make_db()
        self.addCleanup(conn.close)
        store_impact(conn, impact())
        row = row_for(conn)
        with mock.patch.object(automerge, 'run_ci_impact', return_value=impact('full')) as run:
            self.assertEqual(automerge._validation_impact(row)['mode'], 'docs')
            run.assert_not_called()
            automerge._validation_impact(row, heads=[HEAD_TWO])
            run.assert_called_with(BASE_SHA, [HEAD_TWO])
            automerge._validation_impact(row, base_sha=HEAD_THREE)
            run.assert_called_with(HEAD_THREE, [HEAD_ONE])


class ValidationPolicyTests(TestCase):
    def test_docs_prompt_forbids_tests_and_builds_in_both_modes(self):
        for mode in ('sync', 'async'):
            prompt = automerge.build_prompt('batch', [pull()], BASE_SHA, ci_mode=mode,
                                            validation_impact=impact())
            self.assertIn('Do not run tests, builds, or baseline reproductions', prompt)
            self.assertIn('Tests run: none', prompt)
            self.assertNotIn('eatmydata cargo', prompt)
            self.assertNotIn('Baseline from existing evidence', prompt)

    def test_every_other_mode_delegates_selection_without_full_suite_requirement(self):
        for mode in ('impact', 'full', 'unknown'):
            prompt = automerge.build_prompt('batch', [pull()], BASE_SHA, ci_mode='async',
                                            validation_impact=impact(mode))
            self.assertIn('Use your best judgment', prompt)
            self.assertIn('no full suite is mandatory', prompt)
            self.assertIn('Do not rerun ci-impact', prompt)
            self.assertIn('Baseline from existing evidence', prompt)
            self.assertNotIn('Run the full selected scope', prompt)

    def test_docs_gate_still_requires_verdict_and_nonempty_summaries(self):
        self.assertIsNone(automerge._async_local_result(DOCS_REPORT))
        self.assertEqual(automerge._async_local_result(DOCS_REPORT, allow_no_tests=True), 'pass')
        for report in (
            'Tests run: none\nBaseline failures: none',
            'automerge-local: pass\nTests run: none',
            'automerge-local: pass\nTests run:\nBaseline failures: none',
            DOCS_REPORT + '\nautomerge-local: fail',
        ):
            self.assertIsNone(automerge._async_local_result(report, allow_no_tests=True))

    def test_launch_classifies_before_launch_intent_and_persists_policy(self):
        conn = make_db(session_id=None, ci_mode='async')
        self.addCleanup(conn.close)
        with conn:
            conn.execute("UPDATE automerge_batches SET validation_impact_json='{}'")
        def classify(base, heads):
            self.assertEqual(row_for(conn)['launch_attempted'], 0)
            return impact(base=base, heads=heads)
        def launch(args, **kwargs):
            self.assertEqual(row_for(conn)['launch_attempted'], 1)
            prompt = Path(args[args.index('--prompt-file') + 1]).read_text()
            self.assertIn('Do not run tests, builds, or baseline reproductions', prompt)
            return subprocess.CompletedProcess(args, 0, '{"session_id":"launched"}', '')
        with (
            mock.patch.object(automerge, 'lookup_batch_session', return_value=None),
            mock.patch.object(automerge, 'run_ci_impact', side_effect=classify),
            mock.patch.object(automerge, 'skills_connection_prompt', return_value=''),
            mock.patch.object(monitor, 'mj_command', side_effect=launch),
        ):
            self.assertEqual(automerge.launch_batch_session(row_for(conn), [pull()], conn=conn), 'launched')
        self.assertEqual(automerge._stored_impact(row_for(conn)), impact())

    def test_docs_report_checks_actual_published_diff_before_acceptance(self):
        for mode, phase in (('docs', 'merging'), ('impact', 'fixing'), ('unknown', 'fixing')):
            with self.subTest(mode=mode):
                conn = make_db(ci_mode='async')
                self.addCleanup(conn.close)
                store_impact(conn, impact())
                with (
                    mock.patch.object(automerge, 'find_integration_pr', return_value={'number': 211}),
                    mock.patch.object(automerge, 'integration_pr_view', return_value={'headRefOid': HEAD_TWO}),
                    mock.patch.object(automerge, '_try_post_verdict_status'),
                    mock.patch.object(automerge, 'run_ci_impact', return_value=impact(mode, heads=[HEAD_TWO])) as run,
                ):
                    automerge._finish_async_agent_turn(conn, None, row_for(conn), DOCS_REPORT)
                run.assert_called_once_with(BASE_SHA, [HEAD_TWO])
                self.assertEqual(row_for(conn)['phase'], phase)
                self.assertEqual(automerge._stored_impact(row_for(conn))['heads'], [HEAD_TWO])
                if mode != 'docs':
                    self.assertIn('Use your best judgment', row_for(conn)['pending_prompt'])

    def test_docs_publication_retry_keeps_skip_policy_without_reclassification(self):
        conn = make_db(ci_mode='async')
        self.addCleanup(conn.close)
        store_impact(conn, impact())
        with mock.patch.object(automerge, 'run_ci_impact') as run:
            automerge._queue_async_gate_retry(conn, row_for(conn), DOCS_REPORT, 'publication needed')
        run.assert_not_called()
        prompt = row_for(conn)['pending_prompt']
        self.assertIn('publication needs attention', prompt)
        self.assertIn('Do not run tests, builds, or baseline reproductions', prompt)

    def test_docs_merge_uses_confirmed_candidate_without_running_tests_or_reclassifying(self):
        conn = make_db(phase='merging', ci_mode='async', ci_head_sha=HEAD_TWO)
        self.addCleanup(conn.close)
        store_impact(conn, impact(heads=[HEAD_TWO]))
        with conn:
            conn.execute('UPDATE automerge_batches SET agent_final_message=?', (DOCS_REPORT,))
        view = {'state': 'OPEN', 'isDraft': False, 'baseRefName': 'master',
                'headRefOid': HEAD_TWO, 'baseRefOid': BASE_SHA}
        with (
            mock.patch.object(automerge, '_session_status', side_effect=AssertionError('no idle gate')),
            mock.patch.object(automerge, 'integration_pr_view', return_value=view),
            mock.patch.object(automerge, 'current_master_sha', return_value=BASE_SHA),
            mock.patch.object(automerge, '_record_trusted_rejection_markers', return_value=False),
            mock.patch.object(automerge, '_recheck_sources', return_value=([pull()], [])),
            mock.patch.object(automerge, 'verify_source_ancestry', return_value=(True, 'ok')),
            mock.patch.object(automerge, '_try_post_verdict_status', return_value=True) as verdict,
            mock.patch.object(automerge, 'run_ci_impact') as classify,
            mock.patch.object(automerge, 'run_gh') as gh,
            mock.patch.object(automerge, '_complete_landed_batch'),
        ):
            automerge._merge_integration(conn, None, row_for(conn))
        classify.assert_not_called()
        gh.assert_called_once()
        self.assertEqual(gh.call_args.args[0][:3], ['pr', 'merge', '211'])
        self.assertIn('local tests skipped', verdict.call_args.args[5])

    def test_stale_docs_classification_cannot_authorize_no_tests_at_new_base(self):
        conn = make_db(ci_mode='async')
        self.addCleanup(conn.close)
        store_impact(conn, impact(base=HEAD_THREE))
        self.assertFalse(automerge._docs_validation(row_for(conn)))

    @unchanged_queue()
    def test_docs_retry_and_rebuild_prompts_keep_no_test_policy(self):
        conn = make_db(ci_mode='async')
        self.addCleanup(conn.close)
        store_impact(conn, impact())
        automerge._queue_async_gate_retry(conn, row_for(conn), DOCS_REPORT.replace('pass', 'fail'), 'retry')
        self.assertIn('Do not run tests, builds, or baseline reproductions', row_for(conn)['pending_prompt'])
        automerge._request_rebuild(conn, row_for(conn), [pull()], 'rebuild unchanged docs')
        prompt = row_for(conn)['pending_prompt']
        self.assertIn('Do not run tests, builds, or baseline reproductions', prompt)
        self.assertNotIn('eatmydata cargo', prompt)

    @unchanged_queue()
    def test_rebuild_and_master_update_refresh_classification(self):
        conn = make_db(ci_mode='async')
        self.addCleanup(conn.close)
        store_impact(conn, impact())
        with mock.patch.object(automerge, 'run_ci_impact', return_value=impact('impact', heads=[HEAD_ONE, HEAD_TWO])) as run:
            automerge._request_rebuild(conn, row_for(conn), [pull(), pull(8, HEAD_TWO)], 'added code')
        run.assert_called_once_with(BASE_SHA, [HEAD_ONE, HEAD_TWO])
        self.assertIn('Use your best judgment', row_for(conn)['pending_prompt'])
        with (
            mock.patch.object(automerge, '_try_post_verdict_status'),
            mock.patch.object(automerge, 'run_ci_impact', return_value=impact(base=HEAD_THREE, heads=[HEAD_ONE, HEAD_TWO])) as run,
        ):
            automerge._queue_master_update(conn, None, row_for(conn), HEAD_THREE, HEAD_TWO)
        run.assert_called_once_with(HEAD_THREE, [HEAD_ONE, HEAD_TWO])
        self.assertEqual(row_for(conn)['base_sha'], HEAD_THREE)
        self.assertIn('Do not run tests, builds, or baseline reproductions', row_for(conn)['pending_prompt'])
