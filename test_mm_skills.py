"""Real Git and state-service tests for the MergeMarshall skill helpers."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import automerge
import mm_service
from test_automerge import make_db, pull, row_for, BASE_SHA, HEAD_ONE, HEAD_TWO


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
    def test_dependent_head_requires_prerequisite_in_batch_or_base(self):
        a = self.source(1, 'one', 'one\n')
        self.run_git('reset', '--hard', a)
        (self.repo / 'two').write_text('two\n')
        self.run_git('add', 'two')
        tree = self.run_git('write-tree')
        b = self.run_git('commit-tree', tree, '-p', a, '-m', 'Dependent source')
        self.run_git('push', 'origin', b + ':refs/pull/2/head')
        self.run_git('reset', '--hard', self.base)
        child = {'number': 2, 'head_sha': b, 'title': 'Dependent source',
                 'dependencies': [{'number': 1, 'head_sha': a}]}
        self.state['sources'].append(child)
        original = self.state['sources'][:]
        self.state['sources'] = [child]
        with self.assertRaisesRegex(ValueError, 'needs prerequisite #1'):
            self.assemble()
        self.assertEqual(self.run_git('rev-parse', 'HEAD'), self.base)
        self.state['sources'] = original
        self.assertFalse(self.assemble()['manual_required'])
        self.assertIn(a, self.run_git('rev-list', 'HEAD'))

    def test_declared_dependency_missing_from_source_head_stops_before_merge(self):
        a = self.source(1, 'one', 'one\n')
        self.source(2, 'two', 'two\n')
        self.state['sources'][1]['dependencies'] = [{'number': 1, 'head_sha': a}]
        with self.assertRaisesRegex(ValueError, 'does not contain captured prerequisite'):
            self.assemble()
        self.assertEqual(self.run_git('rev-parse', 'HEAD'), self.base)

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
            receipt = json.loads(Path(result["results"]["b"]["receipt"]).read_text())
            self.assertEqual(receipt["head"], b)
            self.assertEqual(receipt["exit_code"], 1)
            self.assertGreaterEqual(receipt["duration_seconds"], 0)
            self.assertEqual(receipt["stdout"]["path"], result["results"]["b"]["stdout"])
            self.assertEqual(self.run_git("rev-parse", "HEAD"), self.base)
            self.assertEqual(len(self.run_git("worktree", "list", "--porcelain").split("worktree ")) - 1, 1)

    def test_script_source_edits_are_flagged(self):
        script = self.root / "check.sh"
        script.write_text('echo edited > marker\n')
        result = compare.compare(self.repo, self.base, self.base, script, self.root / "edited")
        self.assertTrue(result["results"]["a"]["tracked_source_changed"])
        self.assertEqual((self.repo / "marker").read_text(), "base\n")


class ExecutionTests(GitFixture):
    def context(self):
        return dict(self.state, source_revision="a" * 64, attempt_generation=0,
                    status="running", phase="building", ready=None, predecessor=None)

    def client(self):
        client = mock.Mock(connection={"batch_id": "batch-test"})
        client.call.return_value = self.context()
        return client

    def test_runner_records_actual_arguments_output_failure_and_git_identity(self):
        client = self.client()
        command = ["bash", "-c", 'printf "%s" "$1"; echo diagnostic >&2; exit 7', "--", "a space; $(literal)"]
        output = self.root / "execution"
        receipt = db.mm_execution.execute(command, self.repo, output, client=client, state=self.context())
        self.assertEqual(receipt["exit_code"], 7)
        self.assertEqual(receipt["argv"], command)
        self.assertEqual(receipt["head"], self.base)
        self.assertEqual(receipt["tree"], self.run_git("rev-parse", "HEAD^{tree}"))
        self.assertFalse(receipt["tracked_source_changed"])
        self.assertEqual(Path(receipt["stdout"]["path"]).read_text(), "a space; $(literal)")
        self.assertEqual(Path(receipt["stderr"]["path"]).read_text(), "diagnostic\n")
        self.assertEqual(json.loads((output / "receipt.json").read_text()), receipt)
        uploads = [call.kwargs["receipt"] for call in client.call.call_args_list]
        self.assertEqual([r["status"] for r in uploads], ["running", "completed"])
        self.assertNotIn("sha256", json.dumps(receipt))

    def test_failed_delivery_keeps_result_and_replay_never_reruns_command(self):
        client = self.client()
        client.call.side_effect = RuntimeError("service unavailable")
        output = self.root / "pending"
        with mock.patch.dict(db.mm_execution.os.environ, {"GH_TOKEN": "private-token"}):
            receipt = db.mm_execution.execute(["bash", "-c", "echo once"], self.repo, output,
                                              client=client, state=self.context())
        self.assertEqual(receipt["exit_code"], 0)
        self.assertNotIn("private-token", json.dumps(receipt))
        client.call.side_effect = None
        with mock.patch.object(db.mm_execution.subprocess, "run", side_effect=AssertionError("reran check")):
            db.mm_execution.upload(client, output / "receipt.json")
        self.assertEqual(client.call.call_args.kwargs["receipt"], receipt)
        other = mock.Mock(connection={"batch_id": "other"})
        with self.assertRaisesRegex(ValueError, "different batch"):
            db.mm_execution.upload(other, output / "receipt.json")

    def test_script_edits_and_signalled_commands_are_recorded(self):
        script = self.root / "check.sh"
        script.write_text("echo edited > marker\n")
        result = db.mm_execution.execute([], self.repo, self.root / "edit", script=script)
        self.assertTrue(result["tracked_source_changed"])
        self.assertEqual((self.root / "edit/check.sh").read_text(), script.read_text())
        self.run_git("reset", "--hard", self.base)
        result = db.mm_execution.execute(["bash", "-c", "kill -TERM $$"], self.repo, self.root / "signal")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["exit_code"], -15)

    def test_wrapper_refuses_new_checks_after_handoff(self):
        client = self.client()
        client.call.return_value = dict(self.context(), ready={"head": self.base})
        with mock.patch.object(db.mm_execution, "execute", side_effect=AssertionError("started after handoff")):
            with self.assertRaisesRegex(ValueError, "not accepting new checks"):
                db.mm_execution.run(client, command=["true"])


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

    def execution(self, *, identifier="1" * 32, head=HEAD_ONE, **changes):
        current = self.state()
        result = {"id": identifier, "batch_id": "batch-test", "head": head, "tree": "c" * 40,
                  "source_revision": current["source_revision"], "attempt_generation": current["attempt_generation"],
                  "argv": ["bash", "check.sh"], "command": "bash check.sh", "cwd": "/repo",
                  "environment": {"system": "Linux"}, "status": "completed", "exit_code": 0,
                  "started_at": "2026-10-08T12:00:00+00:00", "finished_at": "2026-10-08T12:00:01+00:00",
                  "duration_seconds": 1.0, "head_after": head, "tree_after": "c" * 40,
                  "tracked_source_changed": False, "stdout": {"path": "/logs/out"}, "stderr": {"path": "/logs/err"}}
        return dict(result, **changes)

    def test_execution_intake_is_idempotent_and_preserves_revision_and_assessment(self):
        before = self.assessment()
        receipt = self.execution()
        first = self.call("execution", receipt=receipt)
        second = self.call("execution", receipt=receipt)
        self.assertEqual(first, second)
        self.assertEqual(self.state()["revision"], before["revision"])
        self.assertEqual(self.state()["tests"], before["tests"])
        self.assertEqual(self.call("executions")["executions"], [receipt])
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal'")
        terminal = self.state()
        self.call("execution", receipt=self.execution(identifier="2" * 32))
        self.assertEqual(self.state()["revision"], terminal["revision"])

    def test_execution_start_finish_and_late_start_preserve_completed_evidence(self):
        complete = self.execution()
        start = dict(complete, status="running", finished_at=None, duration_seconds=None, exit_code=None)
        self.call("execution", receipt=start)
        self.call("execution", receipt=complete)
        self.assertEqual(self.call("execution", receipt=start)["execution"], complete)
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.call("execution", receipt=dict(complete, exit_code=1))
        with self.assertRaisesRegex(ValueError, "different check"):
            self.call("execution", receipt=dict(complete, command="another check"))
        with self.assertRaisesRegex(ValueError, "different batch"):
            self.call("execution", receipt=dict(complete, batch_id="other"))

    def test_assessment_snapshots_executions_and_readiness_publication_retains_them(self):
        self.call("execution", receipt=self.execution())
        self.call("execution", receipt=self.execution(identifier="2" * 32, head=BASE_SHA, exit_code=1))
        current = self.assessment()
        self.assertEqual(len(current["tests"]["executions"]), 2)
        self.assertIn("exit 1", db.render_report(current))
        self.assertIn("/logs/out", mm_service.integration_metadata(current, HEAD_ONE, "")["body"])
        publication = {"number": 211, "url": "https://example.invalid/211", "head": HEAD_ONE}
        with mock.patch.object(mm_service, "reconcile_publication", return_value=publication):
            final = self.call("publish", revision=current["revision"], head=HEAD_ONE, notes="")
        self.assertEqual(final["ready"]["assessment"]["executions"], current["tests"]["executions"])
        self.assertIn("Execution evidence:", automerge.ready_report(final["ready"]))
        self.assertEqual(automerge._async_local_result(db.render_report(final)), "pass")

    def test_reused_checks_require_reason_and_dirty_or_incomplete_checks_cannot_be_linked(self):
        old = self.execution(head=HEAD_TWO)
        self.call("execution", receipt=old)
        args = dict(revision=self.state()["revision"], head=HEAD_ONE, verdict="pass", tests="reused check", baseline="none")
        with self.assertRaisesRegex(ValueError, "applicability reason"):
            self.call("tests", **args, executions=[{"id": old["id"]}])
        updated = self.call("tests", **args, executions=[{"id": old["id"], "reuse_reason": "covered code and settings unchanged"}])
        self.assertIn("covered code and settings unchanged", db.render_report(updated))
        args["revision"] = updated["revision"]
        for changes in ({"tracked_source_changed": True},
                        {"status": "running", "finished_at": None, "exit_code": None, "duration_seconds": None}):
            bad = self.execution(identifier="3" * 32 if changes.get("tracked_source_changed") else "4" * 32, **changes)
            self.call("execution", receipt=bad)
            with self.assertRaisesRegex(ValueError, "completed check"):
                self.call("tests", **args, executions=[{"id": bad["id"]}])
        with self.assertRaisesRegex(ValueError, "unknown execution"):
            self.call("tests", **args, executions=[{"id": "5" * 32}])

    def test_changed_attempt_retains_execution_history_without_automatic_reuse(self):
        old = self.execution()
        self.call("execution", receipt=old)
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET attempt_generation=1")
        fresh = self.assessment()
        self.assertNotIn("executions", fresh["tests"])
        self.assertEqual(self.call("executions")["executions"], [old])
        with self.assertRaisesRegex(ValueError, "applicability reason"):
            self.call("tests", revision=fresh["revision"], head=HEAD_ONE, verdict="pass", tests="check", baseline="none",
                      executions=[{"id": old["id"]}])

    def test_local_baseline_finding_preserves_assessment_and_is_idempotent(self):
        before = self.assessment()
        payload = dict(revision=before['revision'], kind='baseline', head=BASE_SHA,
                       identity='policy_cli_test', command='eatmydata cargo nextest run -E test(policy_cli_test)',
                       evidence='At the captured base: expected exit 2, got 1; log /tmp/base.log.')
        first = self.call('finding', **payload)
        second = self.call('finding', **payload)
        self.assertEqual(first['recorded_finding'], second['recorded_finding'])
        self.assertEqual(first['revision'], before['revision'])
        self.assertEqual(first['tests'], before['tests'])
        self.assertEqual(first['sources'], before['sources'])
        self.assertEqual(first['pending_github_writes'], [])
        self.assertEqual(len(self.call('findings')['findings']), 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM known_failures').fetchone()[0], 0)
        self.assertEqual(row_for(self.conn)['retry_rescan_pending'], 0)

    def test_local_findings_validate_revision_base_and_evidence(self):
        payload = dict(revision=self.state()['revision'], kind='baseline', head=BASE_SHA,
                       identity='test_name', command='check test_name', evidence='actual failure')
        for change, error in [({'revision': 'stale'}, 'stale revision'),
                              ({'head': HEAD_ONE}, 'captured base'),
                              ({'kind': 'infrastructure'}, 'baseline or flaky'),
                              ({'command': ''}, 'command'),
                              ({'evidence': ''}, 'evidence'),
                              ({'evidence': 'x' * 12001}, '12000'),
                              ({'evidence': '\u754c' * 12000}, 'encoded limit')]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, error):
                self.call('finding', **dict(payload, **change))
        self.assertEqual(self.call('findings')['findings'], [])

    def test_flake_can_be_recorded_after_completion_without_reopening_batch(self):
        before = self.assessment()
        with self.conn:
            self.conn.execute("UPDATE automerge_batches SET status='completed',phase='terminal'")
        before = self.state()
        updated = self.call('finding', revision=before['revision'], kind='flaky', head=HEAD_ONE,
                            identity='golden_flake', command='check golden_flake',
                            evidence='Failed once: 836 != 837; passed all three subsequent reruns.')
        self.assertEqual(updated['revision'], before['revision'])
        self.assertEqual(updated['phase'], 'terminal')
        self.assertEqual(updated['tests'], before['tests'])
        self.assertEqual(updated['local_findings'][0]['kind'], 'flaky')
        self.assertEqual(updated['local_findings'][0]['head_sha'], HEAD_ONE)

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
        self.assertIn('mergemarshall:local: pass', db.render_report(updated))
        self.assertEqual(updated['ready']['head'], HEAD_ONE)
        self.assertEqual(updated['ready']['assessment'], current['tests'])
        with mock.patch.object(mm_service, 'reconcile_publication', side_effect=AssertionError('duplicate publication')):
            again = self.call('publish', revision=current['revision'], head=HEAD_ONE, notes='resolved conflict')
        self.assertEqual(again['ready'], updated['ready'])
        with self.assertRaisesRegex(ValueError, 'handed to supervisor'):
            self.call('comment', revision=updated['revision'], number=7, body='late mutation')
        # A durable handoff advances even while ACP reports an active turn.
        with (mock.patch.object(automerge, '_wait_agent_turn', side_effect=AssertionError('unexpected wait')),
              mock.patch.object(automerge, '_session_is_idle', side_effect=AssertionError('unexpected idle gate')),
              mock.patch.object(automerge, 'send_start_notification'),
              mock.patch.object(automerge, '_merge_integration') as land):
            automerge.process_batch(self.conn, mock.Mock(), 'batch-test')
        self.assertEqual(land.call_args.args[2]['ci_head_sha'], HEAD_ONE)
        self.assertEqual(row_for(self.conn)['phase'], 'merging')
        with mock.patch.object(mm_service, 'reconcile_publication', side_effect=AssertionError('duplicate publication')):
            self.assertEqual(self.call('publish', revision=current['revision'], head=HEAD_ONE,
                                       notes='resolved conflict')['ready'], updated['ready'])

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
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM automerge_github_outbox "
                                           "WHERE kind='reject_head'").fetchone()[0], 1)

    def test_rejection_evidence_cannot_supply_old_or_new_head_markers(self):
        for marker in ('automerge-rejected-head', 'mergemarshall:rejected-head'):
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, 'without rejection-marker'):
                self.call("exclude", revision=self.state()["revision"], number=7, head=HEAD_ONE,
                          kind="rejected", reason="isolated failure",
                          evidence=f"base passes\n{marker}: {HEAD_TWO}")
        self.assertEqual([p["number"] for p in self.state()["sources"]], [7, 8])
        self.assertEqual(self.state()["pending_github_writes"], [])

    def test_comment_is_recorded_once_and_does_not_change_batch_revision(self):
        current = self.state()
        updated = self.call("comment", revision=current["revision"], number=4519,
                            body="New diagnosis for this batch.")
        self.assertEqual(updated["revision"], current["revision"])
        self.assertEqual(updated["pending_github_writes"][0]["kind"], "issue_comment")
        self.call("comment", revision=current["revision"], number=4519,
                  body="New diagnosis for this batch.")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM automerge_github_outbox "
                                           "WHERE kind='issue_comment'").fetchone()[0], 1)

    def test_cancelled_write_is_not_reported_as_pending(self):
        self.call("comment", revision=self.state()["revision"], number=4519,
                  body="New diagnosis for this batch.")
        self.conn.execute("UPDATE automerge_github_outbox SET cancelled_at=?",
                          (automerge.utc_now(),))
        self.conn.commit()
        self.assertEqual(self.state()["pending_github_writes"], [])

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
                finding = client.call('finding', revision=updated['revision'], kind='baseline',
                                      head=BASE_SHA, identity='local_cli_failure', command='check cli',
                                      evidence='expected status 2, got 1 at captured base')
                self.assertEqual(finding['revision'], updated['revision'])
                self.assertEqual(client.call('findings')['findings'][0]['identity'], 'local_cli_failure')
                with connect() as connection:
                    row = connection.execute("SELECT * FROM automerge_batches WHERE batch_id='http-test'").fetchone()
                    feedback = "CI evidence: " + "é" * 40000
                    automerge.queue_agent_prompt(connection, row, feedback)
                    command_id = connection.execute("SELECT prompt_command_id FROM automerge_batches "
                                                    "WHERE batch_id='http-test'").fetchone()[0]
                instructed = client.call('state')
                self.assertEqual(instructed['supervisor_instruction'], {'id': command_id, 'text': feedback})
                self.assertEqual(instructed['source_revision'], updated['source_revision'])
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


class ExecutionHttpTests(GitFixture):
    def test_cli_preserves_exit_status_and_links_actual_execution_through_http(self):
        path = self.root / "state.db"
        with mock.patch.object(automerge, "DB_PATH", path):
            conn = automerge.connect_db()
            automerge.create_batch(conn, [pull(7, self.base)], self.base, batch_id="execution-http", ci_mode="async")
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
            connection = {"url": f"http://127.0.0.1:{server.server_port}", "batch_id": "execution-http",
                          "token": mm_service.batch_token(key, "execution-http")}
            client = db.Client(connection)
            context = self.root / "connection.json"
            context.write_text(json.dumps(connection))
            script = str(ROOT / "skills/mm-db/scripts/mm_db.py")
            subprocess.run([sys.executable, script, "configure", "--connection-file", str(context)],
                           cwd=self.repo, check=True, capture_output=True)
            before = client.call("state")
            command = subprocess.run([sys.executable, script, "run", "--", "bash", "-c", "echo checked; exit 7"],
                                     cwd=self.repo, text=True, capture_output=True)
            self.assertEqual(command.returncode, 7, command.stderr)
            receipt = json.loads(command.stdout)
            self.assertEqual(client.call("executions")["executions"], [receipt])
            self.assertEqual(client.call("state")["revision"], before["revision"])
            self.assertEqual(Path(receipt["stdout"]["path"]).read_text(), "checked\n")
            assessment = subprocess.run([sys.executable, script, "tests", "--revision", before["revision"],
                                         "--head", self.base, "--verdict", "fail", "--tests", "check exited 7",
                                         "--baseline", "none", "--execution", receipt["id"]],
                                        cwd=self.repo, text=True, capture_output=True)
            self.assertEqual(assessment.returncode, 0, assessment.stderr)
            self.assertEqual(json.loads(assessment.stdout)["tests"]["executions"][0]["id"], receipt["id"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
