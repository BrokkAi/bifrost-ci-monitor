"""Supersession reads authenticated rejection state without clearing it."""
import sqlite3
from types import SimpleNamespace
from unittest import TestCase, mock

import automerge


class SupersessionRejectionTests(TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE automerge_github_outbox '
                          '(number INTEGER, kind TEXT, head_sha TEXT, cancelled_at TEXT)')
        def node(number, head):
            return SimpleNamespace(number=number, head=head, data={
                'head': {'sha': head, 'repo': {'full_name': automerge.REPO_NAME}},
                'base': {'repo': {'full_name': automerge.REPO_NAME}},
            })
        self.retired = node(1, 'a' * 40)
        self.replacement = node(2, 'b' * 40)
        self.comments = {1: [], 2: []}
        patch = mock.patch.object(automerge, 'list_pull_comments',
                                  side_effect=lambda number: self.comments[number])
        patch.start()
        self.addCleanup(patch.stop)
        self.comparisons = []

    def guard(self):
        def compare(ancestor, descendant):
            self.comparisons.append((ancestor, descendant))
            return ancestor in {'a' * 40, 'c' * 40}
        return automerge._verify_supersession_rejections(
            self.conn, self.retired, self.replacement, compare)

    def marker(self, head, login=automerge.TRUSTED_REJECTION_LOGIN):
        return {'user': {'login': login}, 'body': f'automerge-rejected-head: {head}\nproof'}

    def test_no_rejection_allows_audit_without_state_writes(self):
        self.assertTrue(self.guard())
        self.assertEqual(self.conn.total_changes, 0)

    def test_closed_retired_trusted_rejected_ancestor_remains_blocking(self):
        self.comments[1] = [self.marker('c' * 40)]
        self.assertFalse(self.guard())
        self.assertEqual(self.comparisons, [('c' * 40, self.retired.head)])

    def test_replacement_rejected_ancestor_also_remains_blocking(self):
        self.comments[2] = [self.marker('c' * 40)]
        self.assertFalse(self.guard())

    def test_untrusted_comment_cannot_authorize_or_reject(self):
        self.comments[1] = [self.marker('c' * 40, 'untrusted')]
        self.assertTrue(self.guard())
        self.assertEqual(self.comparisons, [])

    def test_active_intent_is_authority_even_without_comment(self):
        self.conn.execute('INSERT INTO automerge_github_outbox VALUES (?,?,?,?)',
                          (1, 'reject_head', 'c' * 40, None))
        self.assertFalse(self.guard())

    def test_authenticated_cancelled_intent_releases_matching_marker(self):
        self.comments[1] = [self.marker('c' * 40)]
        self.conn.execute('INSERT INTO automerge_github_outbox VALUES (?,?,?,?)',
                          (1, 'reject_head', 'c' * 40, '2026-10-11T00:00:00Z'))
        self.assertTrue(self.guard())
        self.assertEqual(self.comparisons, [])

    def test_active_intent_dominates_cancelled_intent_for_same_head(self):
        self.comments[1] = [self.marker('c' * 40)]
        self.conn.executemany('INSERT INTO automerge_github_outbox VALUES (?,?,?,?)',
                              [(1, 'reject_head', 'c' * 40, 'earlier'),
                               (1, 'reject_head', 'c' * 40, None)])
        self.assertFalse(self.guard())

    def test_unknown_rejected_label_fails_closed(self):
        self.retired.data['labels'] = [{'name': automerge.REJECTED_LABEL}]
        self.assertFalse(self.guard())

    def test_unavailable_comment_read_is_not_a_negative_rejection_fact(self):
        with mock.patch.object(automerge, 'list_pull_comments', side_effect=RuntimeError('unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                self.guard()

    def test_missing_repository_metadata_is_not_supersession_authority(self):
        self.retired.data.pop('head')
        self.assertFalse(self.guard())

    def test_fork_repository_is_not_supersession_authority(self):
        self.replacement.data['head']['repo']['full_name'] = 'fork/bifrost-dev'
        self.assertFalse(self.guard())

    def test_mutable_head_metadata_must_match_exact_loaded_head(self):
        self.replacement.data['head']['sha'] = 'f' * 40
        self.assertFalse(self.guard())
