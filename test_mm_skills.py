"""Real Git and state-service tests for the MergeMarshall skill helpers."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import automerge
import mm_service
from test_automerge import make_db, pull, row_for, HEAD_ONE, HEAD_TWO


ROOT = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge = load("mm_merge", "skills/mm-merge/scripts/mm_merge.py")
autopr = load("mm_autopr", "skills/mm-autopr/scripts/mm_autopr.py")
compare = load("mm_compare", "skills/mm-compare/scripts/mm_compare.py")
db = load("mm_db_client", "skills/mm-db/scripts/mm_db.py")
installer = load("mm_installer", "scripts/install-mm-skills.py")


class InstallerTests(unittest.TestCase):
    def test_service_directory_is_an_absolute_path_without_quotes(self):
        unit = installer.service_unit(ROOT, "172.31.3.117")
        self.assertIn(f"\nWorkingDirectory={ROOT}\n", unit)
        self.assertIn(f'ExecStart=/usr/bin/python3 "{ROOT}/mm_service.py"', unit)

    def test_service_rejects_public_or_unspecified_addresses(self):
        for address in ["0.0.0.0", "8.8.8.8"]:
            with self.assertRaises(ValueError):
                installer.service_unit(ROOT, address)

    def test_service_uses_the_configured_github_cli(self):
        with mock.patch.dict(installer.os.environ, {"BIFROST_GH_BIN": "/opt/github/gh"}):
            self.assertIn('Environment="BIFROST_GH_BIN=/opt/github/gh"',
                          installer.service_unit(ROOT, "172.31.3.117"))
            self.assertIn('Environment="BIFROST_GH_BIN=/opt/override/gh"',
                          installer.service_unit(ROOT, "172.31.3.117", "/opt/override/gh"))
        with (mock.patch.dict(installer.os.environ, {}, clear=True),
              mock.patch.object(installer.Path, "home", return_value=Path("/home/test")),
              mock.patch.object(installer.os, "access", return_value=True)):
            self.assertIn('Environment="BIFROST_GH_BIN=/home/test/.local/bin/gh"',
                          installer.service_unit(ROOT, "172.31.3.117"))

    def test_install_is_idempotent_and_preserves_existing_skills(self):
        with tempfile.TemporaryDirectory() as root:
            profile = Path(root) / "profile"
            config = Path(root) / "config.toml"
            config.write_text(f'[profiles.test]\nhome = "{profile}"\n')
            installed = installer.install(config, ROOT)
            self.assertEqual(len(installed), 4)
            self.assertEqual(installer.install(config, ROOT), [])
            target = profile / "skills/mm-db"
            self.assertEqual(target.resolve(), ROOT / "skills/mm-db")
            target.unlink()
            target.mkdir()
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                installer.install(config, ROOT)


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.run_git("init", "--initial-branch=master")
        self.run_git("config", "user.name", "Skill test")
        self.run_git("config", "user.email", "test@example.invalid")
        (self.repo / "marker").write_text("base\n")
        self.run_git("add", "marker")
        self.run_git("commit", "-m", "Base")
        self.base = self.run_git("rev-parse", "HEAD")
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.run_git("remote", "add", "origin", str(self.remote))
        self.run_git("symbolic-ref", "HEAD", "refs/heads/mergemarshall/batch-test")
        self.run_git("reset", "--hard", self.base)
        self.state = {"batch_id": "batch-test", "branch": "mergemarshall/batch-test",
                      "base_sha": self.base, "sources": [], "excluded": []}

    def run_git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def source(self, number, filename, content):
        (self.repo / filename).write_text(content)
        self.run_git("add", filename)
        tree = self.run_git("write-tree")
        head = self.run_git("commit-tree", tree, "-p", self.base, "-m", f"Source {number}")
        self.run_git("push", "origin", head + f":refs/pull/{number}/head")
        self.run_git("reset", "--hard", self.base)
        self.state["sources"].append({"number": number, "head_sha": head,
                                      "title": f"Source {number}", "url": f"https://example.invalid/{number}"})
        return head

    def assemble(self, **kwargs):
        with mock.patch.object(merge, "git", side_effect=lambda *a, **kw: db.git(*a, cwd=self.repo, **kw)):
            return merge.assemble(self.state, **kwargs)


class MergeTests(GitFixture):
    def test_octopus_retains_heads_trailer_and_repeated_call_is_noop(self):
        a = self.source(1, "one", "one\n")
        b = self.source(2, "two", "two\n")
        result = self.assemble()
        self.assertFalse(result["manual_required"])
        self.assertEqual(len(self.run_git("rev-list", "--parents", "-n", "1", "HEAD").split()), 4)
        self.assertIn(a, self.run_git("rev-list", "HEAD"))
        self.assertIn(b, self.run_git("rev-list", "HEAD"))
        self.assertIn("Automerge-Batch: batch-test", self.run_git("log", "-1", "--format=%B"))
        self.assertEqual(self.assemble()["head"], result["head"])

    def test_failed_octopus_restores_tree_and_manual_merge_can_resume(self):
        self.source(1, "marker", "one\n")
        self.source(2, "marker", "two\n")
        self.assertTrue(self.assemble()["manual_required"])
        self.assertEqual(self.run_git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.run_git("status", "--porcelain"), "")
        result = self.assemble(manual=True)
        self.assertEqual(result["pr"], 2)
        (self.repo / "marker").write_text("both intents resolved\n")
        self.run_git("add", "marker")
        self.run_git("commit", "--no-edit")
        self.assertFalse(self.assemble(manual=True)["manual_required"])
        self.assertIn("Automerge-Batch: batch-test", self.run_git("log", "-1", "--format=%B"))

    def test_changed_source_does_not_merge(self):
        self.source(1, "one", "one\n")
        self.state["sources"][0]["head_sha"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "head changed"):
            self.assemble()
        self.assertEqual(self.run_git("rev-parse", "HEAD"), self.base)

    def test_rebuild_really_removes_an_excluded_source(self):
        a = self.source(1, "one", "one\n")
        self.source(2, "two", "two\n")
        self.assemble()
        self.state["sources"] = self.state["sources"][1:]
        self.state["excluded"] = [{"number": 1, "head_sha": a, "kind": "removed"}]
        with self.assertRaisesRegex(ValueError, "still present"):
            self.assemble()
        self.assemble(rebuild=True)
        self.assertFalse((self.repo / "one").exists())
        self.assertTrue((self.repo / "two").exists())


class CompareTests(GitFixture):
    def test_parallel_and_sequential_capture_failures_and_leave_checkout_untouched(self):
        b = self.source(1, "marker", "candidate\n")
        script = self.root / "check.sh"
        script.write_text('cat marker\necho diagnostic >&2\ntest "$(cat marker)" = base\n')
        for sequential in [False, True]:
            result = compare.compare(self.repo, self.base, b, script,
                                     self.root / ("seq" if sequential else "parallel"), sequential=sequential)
            self.assertEqual(result["results"]["a"]["exit_code"], 0)
            self.assertEqual(result["results"]["b"]["exit_code"], 1)
            self.assertIn("+candidate", Path(result["stdout_diff"]).read_text())
            self.assertFalse(result["results"]["b"]["tracked_source_changed"])
            self.assertEqual(self.run_git("rev-parse", "HEAD"), self.base)
            self.assertEqual(len(self.run_git("worktree", "list", "--porcelain").split("worktree ")) - 1, 1)

    def test_script_source_edits_are_flagged(self):
        script = self.root / "check.sh"
        script.write_text('echo edited > marker\n')
        result = compare.compare(self.repo, self.base, self.base, script, self.root / "edited")
        self.assertTrue(result["results"]["a"]["tracked_source_changed"])
        self.assertEqual((self.repo / "marker").read_text(), "base\n")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(pulls=[pull(7, HEAD_ONE), pull(8, HEAD_TWO)], ci_mode="async")
        self.addCleanup(self.conn.close)
        mm_service.ensure_schema(self.conn)

    def state(self):
        return mm_service.state(self.conn, "batch-test")

    def call(self, operation, **payload):
        return mm_service.dispatch(self.conn, "batch-test", operation, payload)

    def assessment(self):
        return self.call("tests", revision=self.state()["revision"], head=HEAD_ONE,
                         verdict="pass", tests="bash /tmp/check.sh", baseline="none")

    def test_stale_revision_and_wrong_head_exclusions_are_rejected(self):
        revision = self.state()["revision"]
        updated = self.call("exclude", revision=revision, number=7, head=HEAD_ONE,
                            kind="removed", reason="head changed")
        self.assertEqual([p["number"] for p in updated["sources"]], [8])
        with self.assertRaisesRegex(ValueError, "stale revision"):
            self.call("exclude", revision=revision, number=8, head=HEAD_TWO, kind="removed", reason="closed")
        with self.assertRaisesRegex(ValueError, "captured source"):
            self.call("exclude", revision=updated["revision"], number=8, head=HEAD_ONE, kind="removed", reason="closed")

    def test_stale_supervisor_row_does_not_restore_an_exclusion(self):
        stale = row_for(self.conn)
        automerge._persist_excluded_source_heads(self.conn, stale, [{"number": 7, "head_sha": HEAD_ONE, "kind": "removed"}])
        automerge._persist_excluded_source_heads(self.conn, stale, [{"number": 8, "head_sha": HEAD_TWO, "kind": "removed"}])
        self.assertEqual(self.state()["sources"], [])
        self.assertEqual(len(self.state()["excluded"]), 2)

    def test_changed_membership_invalidates_assessment(self):
        current = self.assessment()
        self.call("exclude", revision=current["revision"], number=8, head=HEAD_TWO,
                  kind="removed", reason="closed")
        self.assertIsNone(self.state()["tests"])

    def test_explicit_reassessment_can_replace_failure_with_same_prior_pass_summary(self):
        self.assessment()
        self.call("tests", revision=self.state()["revision"], head=HEAD_ONE,
                  verdict="fail", tests="bash /tmp/check.sh", baseline="none")
        self.assertEqual(self.state()["tests"]["verdict"], "fail")
        self.assertEqual(self.assessment()["tests"]["verdict"], "pass")

    def test_publication_is_recorded_and_final_report_is_accepted(self):
        current = self.assessment()
        publication = {"number": 211, "url": "https://example.invalid/211", "head": HEAD_ONE}
        with mock.patch.object(mm_service, "reconcile_publication", return_value=publication) as reconcile:
            updated = self.call("publish", revision=current["revision"], head=HEAD_ONE, notes="resolved conflict")
        self.assertEqual(reconcile.call_args.args[0]["sources"], current["sources"])
        self.assertEqual(updated["publication"], publication)
        self.assertEqual(updated["pending_github_writes"],
                         [{"kind": "integration_metadata", "number": 211, "head_sha": HEAD_ONE}])
        self.assertEqual(row_for(self.conn)["integration_pr_number"], 211)
        self.assertEqual(automerge._async_local_result(db.render_report(updated)), "pass")

    def test_rejected_source_is_recorded_without_waiting_for_github(self):
        current = self.state()
        with mock.patch.object(automerge, "run_gh", side_effect=AssertionError("network write")):
            updated = self.call("exclude", revision=current["revision"], number=7, head=HEAD_ONE,
                                kind="rejected", reason="isolated failure",
                                evidence="base passes; base plus PR fails")
        self.assertEqual([p["number"] for p in updated["sources"]], [8])
        self.assertEqual(updated["pending_github_writes"],
                         [{"kind": "reject_head", "number": 7, "head_sha": HEAD_ONE}])
        self.call("exclude", revision=updated["revision"], number=7, head=HEAD_ONE,
                  kind="rejected", reason="isolated failure", evidence="base passes; base plus PR fails")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM automerge_github_outbox").fetchone()[0], 1)

    def test_comment_is_recorded_once_and_does_not_change_batch_revision(self):
        current = self.state()
        updated = self.call("comment", revision=current["revision"], number=4519,
                            body="New diagnosis for this batch.")
        self.assertEqual(updated["revision"], current["revision"])
        self.assertEqual(updated["pending_github_writes"][0]["kind"], "issue_comment")
        self.call("comment", revision=current["revision"], number=4519,
                  body="New diagnosis for this batch.")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM automerge_github_outbox").fetchone()[0], 1)

    def test_inspect_reads_pr_through_supervisor_identity(self):
        with (mock.patch.object(automerge, "gh_json", return_value={"number": 7, "body": "Intent"}) as read,
              mock.patch.object(automerge, "list_pull_comments", return_value=[]) as comments):
            result = self.call("inspect", number=7, kind="pull")
        self.assertEqual(result["item"]["body"], "Intent")
        self.assertEqual(read.call_args.args[0],
                         ["api", f"repos/{automerge.REPO_NAME}/pulls/7"])
        comments.assert_called_once_with(7)

    def test_terminal_batch_rejects_updates(self):
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal'")
        with self.assertRaisesRegex(ValueError, "no longer"):
            self.assessment()

    def test_landing_phase_rejects_agent_mutations(self):
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET phase='merging'")
        with self.assertRaisesRegex(ValueError, "no longer"):
            self.assessment()

    def test_rebuild_cannot_overwrite_an_exclusion_recorded_during_rescan(self):
        original = row_for(self.conn)

        def rescan(**_kwargs):
            automerge._persist_excluded_source_heads(self.conn, original,
                                                     [{"number": 7, "head_sha": HEAD_ONE, "kind": "removed"}])
            return []

        with (mock.patch.object(automerge, "_recheck_sources", return_value=([pull(7), pull(8, HEAD_TWO)], [])),
              mock.patch.object(automerge, "select_eligible_pull_requests", side_effect=rescan)):
            with self.assertRaisesRegex(automerge.AutomergeError, "membership changed"):
                automerge._request_rebuild(self.conn, original, [pull(7), pull(8, HEAD_TWO)], "rebuild")
        self.assertEqual([p["number"] for p in self.state()["sources"]], [8])

    def test_publication_reconciles_existing_pr_using_rest_and_actual_membership(self):
        current = self.assessment()
        pr = {"number": 211, "html_url": "https://example.invalid/211", "state": "open", "draft": False,
              "head": {"sha": HEAD_ONE, "ref": current["branch"]}, "base": {"ref": "master"}}
        calls = []

        def api(endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            if endpoint.startswith("git/ref/"):
                return {"object": {"sha": HEAD_ONE}}
            if endpoint.startswith("pulls?"):
                return [pr]
            if endpoint == "pulls/211":
                return pr
            return {}

        with mock.patch.object(mm_service, "gh_api", side_effect=api):
            result = mm_service.reconcile_publication(current, HEAD_ONE, "notes")
        self.assertEqual(result["number"], 211)
        metadata = mm_service.integration_metadata(current, HEAD_ONE, "notes")
        self.assertEqual(metadata["title"], "Merge batch: #7 #8")
        self.assertIn(HEAD_TWO, metadata["body"])
        self.assertFalse(any(kwargs.get("method") == "PATCH" for _, kwargs in calls))
        self.assertFalse(any(endpoint == "pulls" and kwargs.get("method") == "POST" for endpoint, kwargs in calls))


class ClientPublicationTests(GitFixture):
    def test_client_pushes_only_recorded_branch_and_enforces_tested_head(self):
        self.source(1, "one", "one\n")
        result = self.assemble()
        self.state.update(tests={"head": result["head"], "verdict": "pass"}, revision="rev")
        client = mock.Mock()
        client.call.side_effect = [self.state, {"publication": "recorded"}]
        with mock.patch.object(autopr, "git", side_effect=lambda *a, **kw: db.git(*a, cwd=self.repo, **kw)):
            autopr.publish(client)
        self.assertEqual(self.run_git("ls-remote", "origin", "refs/heads/" + self.state["branch"]).split()[0], result["head"])
        client.call.assert_called_with("publish", revision="rev", head=result["head"], notes="")
        self.state["tests"]["head"] = self.base
        client.call.side_effect = [self.state]
        with mock.patch.object(autopr, "git", side_effect=lambda *a, **kw: db.git(*a, cwd=self.repo, **kw)):
            with self.assertRaisesRegex(ValueError, "exact HEAD"):
                autopr.publish(client)


class HttpTests(unittest.TestCase):
    def test_authenticated_client_reads_and_records_shared_state(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.db"
            with mock.patch.object(automerge, "DB_PATH", path):
                conn = automerge.connect_db()
                automerge.create_batch(conn, [pull()], "a" * 40, batch_id="http-test", ci_mode="async")
                conn.close()

            def connect():
                import sqlite3
                connection = sqlite3.connect(path)
                connection.row_factory = sqlite3.Row
                return connection

            key = b"k" * 32
            server = mm_service.Server(("127.0.0.1", 0), key, connect)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = db.Client({"url": f"http://127.0.0.1:{server.server_port}", "batch_id": "http-test",
                                    "token": mm_service.batch_token(key, "http-test")})
                current = client.call("state")
                updated = client.call("tests", revision=current["revision"], head=HEAD_ONE,
                                      verdict="pass", tests="bash check.sh", baseline="none")
                self.assertEqual(client.call("state")["tests"], updated["tests"])
                self.assertEqual(automerge._async_local_result(db.render_report(updated)), "pass")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_authentication_is_scoped_to_one_batch(self):
        key = b"k" * 32
        server = mm_service.Server(("127.0.0.1", 0), key, mock.Mock())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = db.Client({"url": f"http://127.0.0.1:{server.server_port}", "batch_id": "other-batch",
                                "token": mm_service.batch_token(key, "batch-test")})
            with self.assertRaisesRegex(RuntimeError, "invalid batch token"):
                client.call("state")
            server.connect.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
