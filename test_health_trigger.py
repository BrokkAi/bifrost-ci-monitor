import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import health_trigger as ht


class HealthTriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.args = argparse.Namespace(mode="shadow", state_dir=str(self.directory),
                                       session="test-session", ssh_target="ubuntu@host", _prompt="")
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sender = Mock(return_value={})
        self.probe = {"format": 1, "probed_at": "2026-01-01T00:00:00Z",
                      "findings": [{"kind": "stalled", "entity": "merger", "cause": "stale",
                                    "fingerprint": "ignored", "persist_seconds": 0,
                                    "summary": "Merger is stalled"}], "check_errors": []}

    def runner(self, *, aws=None, ssh=None):
        return lambda *a, **k: ssh or completed(json.dumps(self.probe))

    def tick(self, run=None, delta=0):
        return ht.execute(self.args, sender=self.sender, run=run or self.runner(),
                          now=self.now + timedelta(seconds=delta))

    def state(self):
        return json.loads((self.directory / "state.json").read_text())

    def test_first_and_second_sighting_and_accepted_not_resent(self):
        self.tick()
        self.sender.assert_not_called()
        self.tick(delta=60)
        self.assertEqual(self.sender.call_count, 1)
        self.tick(delta=120)
        self.assertEqual(self.sender.call_count, 1)
        self.assertIsNotNone(next(iter(self.state()["findings"].values()))["triggered_at"])

    def test_probe_fingerprint_is_the_episode_identity(self):
        # A retry counter in the raw cause must not restart the debounce.
        self.tick()
        self.probe["findings"][0]["cause"] = "stale after 7 attempts"
        self.tick(delta=900)
        self.sender.assert_called_once()
        self.assertEqual(list(self.state()["findings"]), ["ignored"])

    def test_persistence_threshold_and_absence_reset(self):
        self.probe["findings"][0]["persist_seconds"] = 100
        self.tick()
        self.tick(delta=99)
        self.sender.assert_not_called()
        self.tick(delta=101)
        self.assertEqual(self.sender.call_count, 1)
        empty = {"format": 1, "findings": [], "check_errors": []}
        self.tick(run=self.runner(ssh=completed(json.dumps(empty))), delta=110)
        self.assertEqual(self.state()["findings"], {})
        self.sender.reset_mock()
        self.tick(delta=120)
        self.sender.assert_not_called()
        self.tick(delta=221)
        self.sender.assert_called_once()

    def test_twelve_hour_retrigger_has_new_request_id_generation(self):
        self.tick()
        self.tick(delta=1)
        first = self.sender.call_args.kwargs["request_id"]
        self.tick(delta=12 * 3600 + 2)
        second = self.sender.call_args.kwargs["request_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(next(iter(self.state()["findings"].values()))["generation"], 1)

    def test_failed_send_retries_same_id_and_text_before_new_due(self):
        self.sender.side_effect = [RuntimeError("offline"), {}, {}]
        self.tick()
        self.tick(delta=1)
        original_id, original_text = self.sender.call_args.kwargs["request_id"], self.sender.call_args.args[1]
        pending = self.state()["pending_trigger"]
        self.assertEqual(pending["request_id"], original_id)
        self.assertEqual(pending["text"], original_text)
        self.tick(delta=2)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.sender.call_args_list[1].kwargs["request_id"], original_id)
        self.assertEqual(self.sender.call_args_list[1].args[1], original_text)
        self.assertIsNone(self.state()["pending_trigger"])

    def test_message_modes_cap_and_size(self):
        entries = [{"kind": "k", "entity": str(i), "summary": "x" * 2000,
                    "fingerprint": str(i), "first_seen": "now", "generation": 0} for i in range(45)]
        shadow = ht.format_message("shadow", entries, self.directory)
        self.assertIn("SHADOW MODE", shadow)
        self.assertIn("verdicts.jsonl", shadow)
        self.assertIn("and 5 more", shadow)
        self.assertLessEqual(len(shadow.encode()), ht.TEXT_LIMIT)
        live = ht.format_message("live", entries[:1], self.directory, "full prompt")
        self.assertIn("Triggered CI-agent health check", live)
        self.assertIn("full prompt", live)
        self.assertLessEqual(len(live.encode()), ht.TEXT_LIMIT)

    def test_discovery_and_probe_failures_are_synthetic(self):
        for count in (0, 2):
            target, finding = ht.discover_target(lambda *a, **k: completed(json.dumps([{}] * count)))
            self.assertIsNone(target)
            self.assertEqual(finding["kind"], "probe_ec2_discovery")
            self.assertIn(f"{count} running instances", finding["cause"])
        _, findings = ht.run_probe("host", lambda *a, **k: completed("", 255))
        self.assertEqual(findings[0]["cause"], "exit 255")
        _, findings = ht.run_probe("host", lambda *a, **k: completed("no json"))
        self.assertEqual(findings[0]["cause"], "invalid json")
        self.probe["check_errors"] = [{"check": "merger", "error": "stuck"}]
        _, findings = ht.run_probe("host", lambda *a, **k: completed(json.dumps(self.probe)))
        self.assertEqual(findings[-1]["kind"], "probe_check_failed")

    def test_lock_corrupt_state_permissions_and_rotation(self):
        lock_path = self.directory / "lock"
        with lock_path.open("a+") as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.tick()
        self.sender.assert_not_called()
        (self.directory / "state.json").write_text("{")
        self.tick()
        self.assertTrue(list(self.directory.glob("state.json.corrupt-*")))
        self.assertEqual(os.stat(self.directory / "state.json").st_mode & 0o777, 0o600)
        log = self.directory / "probe-log.jsonl"
        log.write_text("x" * (ht.LOG_LIMIT + 1))
        self.tick()
        self.assertTrue((self.directory / "probe-log.jsonl.1").exists())


def completed(stdout, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "never expose stderr")


if __name__ == "__main__":
    unittest.main()
