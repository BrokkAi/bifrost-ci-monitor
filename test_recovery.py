#!/usr/bin/env python3
"""Focused tests for the durable recovery package."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import recovery


GIT = shutil.which("git") or "/usr/bin/git"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [GIT, *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def git_bytes(cwd: Path, *args: str) -> bytes:
    result = subprocess.run(
        [GIT, *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    return result.stdout


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        git(self.root, "init", "--initial-branch=master", str(self.repo))
        git(self.repo, "config", "user.name", "Recovery Test")
        git(self.repo, "config", "user.email", "recovery@example.invalid")
        (self.repo / "tracked.txt").write_bytes(b"base\n")
        (self.repo / "delete.txt").write_bytes(b"delete me\n")
        (self.repo / "sibling.txt").write_bytes(b"sibling base\n")
        git(self.repo, "add", "tracked.txt", "delete.txt", "sibling.txt")
        git(self.repo, "commit", "-m", "base")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def package(self, name: str = "package") -> Path:
        return self.root / name

    def preserve(self, package: Path | None = None, *, run_id: int = 10) -> recovery.RecoveryResult:
        return recovery.preserve(
            self.repo,
            package or self.package(),
            run_id=run_id,
            attempt=1,
            sha="test-sha",
            session_id="session-test",
            transcript="repair transcript\n",
            git_bin=GIT,
        )

    def test_ordinary_restore_keeps_staged_unstaged_and_untracked(self) -> None:
        (self.repo / "tracked.txt").write_bytes(b"staged\n")
        git(self.repo, "add", "tracked.txt")
        (self.repo / "tracked.txt").write_bytes(b"staged then unstaged\n")
        (self.repo / "new file\nname").write_bytes(b"binary\0payload\xff")
        before_index = (self.repo / ".git" / "index").read_bytes()
        result = self.preserve()

        self.assertEqual(result.preservation_status, "complete")
        self.assertIsNotNone(result.stash_ref)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), before_index)
        self.assertIn("MM tracked.txt", git(self.repo, "status", "--short"))

        restored = recovery.restore(
            Path(result.manifest_path), self.root / "restored", git_bin=GIT
        )
        self.assertEqual((restored / "tracked.txt").read_bytes(), b"staged then unstaged\n")
        self.assertEqual((restored / "new file\nname").read_bytes(), b"binary\0payload\xff")
        self.assertIn("MM tracked.txt", git(restored, "status", "--short"))
        self.assertIn("new file", git(restored, "status", "--short"))

    def test_conflict_restore_recreates_index_deletion_and_merge_metadata(self) -> None:
        git(self.repo, "checkout", "-b", "side")
        (self.repo / "tracked.txt").write_text("side\n", encoding="utf-8")
        (self.repo / "sibling.txt").write_text("side sibling\n", encoding="utf-8")
        (self.repo / "delete.txt").write_text("side delete\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "side")
        git(self.repo, "checkout", "master")
        (self.repo / "tracked.txt").write_text("master\n", encoding="utf-8")
        (self.repo / "sibling.txt").write_text("master sibling\n", encoding="utf-8")
        (self.repo / "delete.txt").write_text("master delete\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "master")
        subprocess.run([GIT, "merge", "side"], cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Resolve one file, stage a deletion for another, and leave a sibling
        # conflict unresolved.  This exercises all index stages and absent
        # worktree paths in one package.
        (self.repo / "tracked.txt").write_text("manual resolution\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "rm", "delete.txt")
        source_index = git_bytes(self.repo, "ls-files", "--stage", "-z")
        result = self.preserve(run_id=11)

        self.assertEqual(result.preservation_status, "complete")
        self.assertIsNotNone(result.conflict_snapshot)
        self.assertIsNone(result.stash_sha)
        restored = recovery.restore(
            Path(result.manifest_path), self.root / "conflict-restored", git_bin=GIT
        )
        self.assertEqual(git_bytes(restored, "ls-files", "--stage", "-z"), source_index)
        self.assertEqual(git(restored, "ls-files", "-u").count("sibling.txt"), 3)
        self.assertEqual(git(restored, "ls-files", "-u").count("tracked.txt"), 0)
        self.assertFalse((restored / "delete.txt").exists())
        self.assertEqual((restored / "tracked.txt").read_text(encoding="utf-8"), "manual resolution\n")
        self.assertIn("<<<<<<< HEAD", (restored / "sibling.txt").read_text(encoding="utf-8"))
        merge_head = Path(git(restored, "rev-parse", "--git-path", "MERGE_HEAD"))
        if not merge_head.is_absolute():
            merge_head = restored / merge_head
        self.assertTrue(merge_head.exists())

    def test_refs_survive_stash_reordering_and_gc(self) -> None:
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (self.repo / "ignored-build").mkdir()
        (self.repo / "ignored-build" / "output").write_text("ignored", encoding="utf-8")
        (self.repo / ".gitignore").write_text("ignored-build/\n", encoding="utf-8")
        result = self.preserve(run_id=12)
        self.assertEqual(result.preservation_status, "complete")
        head_ref, stash_ref = result.head_ref, result.stash_ref
        git(self.repo, "stash", "push", "--include-untracked", "-m", "later")
        git(self.repo, "gc", "--prune=now")
        loaded = recovery.load(self.package())
        self.assertEqual(loaded.head_ref, head_ref)
        self.assertEqual(loaded.stash_ref, stash_ref)
        self.assertEqual(recovery.restore(Path(loaded.manifest_path), self.root / "gc-restored", git_bin=GIT).joinpath("tracked.txt").read_text(), "changed\n")

    def test_modes_symlinks_and_literal_pathspec_filename_round_trip(self) -> None:
        executable = self.repo / "run.sh"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o751)
        os.symlink("run.sh", self.repo / "run-link")
        odd = self.repo / ":(literal)"
        odd.write_bytes(b"odd\0binary\xff")
        result = self.preserve(run_id=15)
        self.assertEqual(result.preservation_status, "complete")

        restored = recovery.restore(
            Path(result.manifest_path), self.root / "modes-restored", git_bin=GIT
        )
        self.assertEqual(stat.S_IMODE(os.stat(restored / "run.sh").st_mode), 0o751)
        self.assertTrue((restored / "run-link").is_symlink())
        self.assertEqual(os.readlink(restored / "run-link"), "run.sh")
        self.assertEqual((restored / ":(literal)").read_bytes(), b"odd\0binary\xff")
        self.assertIn(":(literal)", git(restored, "status", "--short"))

    def test_directory_replaced_by_symlink_does_not_escape_archive(self) -> None:
        nested = self.repo / "nested"
        nested.mkdir()
        (nested / "tracked.txt").write_bytes(b"nested\n")
        git(self.repo, "add", "nested/tracked.txt")
        git(self.repo, "commit", "-m", "nested")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"must not be copied\n")
        (nested / "tracked.txt").unlink()
        nested.rmdir()
        os.symlink(outside, nested)

        result = self.preserve(run_id=16)
        self.assertEqual(result.preservation_status, "complete")
        restored = recovery.restore(
            Path(result.manifest_path), self.root / "symlink-restored", git_bin=GIT
        )
        # The replacement symlink itself is an archived untracked path; the
        # tracked descendant is absent and must never be read through it.
        self.assertTrue((restored / "nested").is_symlink())
        self.assertEqual(os.readlink(restored / "nested"), str(outside))
        archived_bytes = [path.read_bytes() for path in (self.package() / "archive" / "blobs").iterdir()]
        self.assertNotIn(b"must not be copied\n", archived_bytes)

    def test_failed_preservation_does_not_touch_source(self) -> None:
        (self.repo / "tracked.txt").write_text("local\n", encoding="utf-8")
        before = git(self.repo, "status", "--porcelain=v1", "--untracked-files=all")
        result = recovery.preserve(
            self.repo,
            self.package(),
            run_id=13,
            attempt=1,
            sha="test-sha",
            session_id=None,
            transcript="failure",
            git_bin=str(self.root / "does-not-exist-git"),
        )
        self.assertEqual(result.preservation_status, "failed")
        self.assertFalse(result.complete)
        self.assertEqual(before, git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"))
        manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
        self.assertFalse(manifest["complete"])

    def test_complete_archive_corruption_is_rejected_and_not_recaptured(self) -> None:
        result = self.preserve(run_id=17)
        self.assertEqual(result.preservation_status, "complete")
        manifest_before = Path(result.manifest_path).read_bytes()
        blob = next((self.package() / "archive" / "blobs").iterdir())
        blob.write_bytes(blob.read_bytes() + b"tampered")
        with self.assertRaises(recovery.RecoveryError):
            recovery.load(self.package())
        # A complete package is immutable: preserve must fail closed rather
        # than silently taking a new snapshot over a damaged artifact.
        again = self.preserve(run_id=17)
        self.assertEqual(again.preservation_status, "failed")
        self.assertEqual(Path(result.manifest_path).read_bytes(), manifest_before)

    def test_clean_snapshot_is_complete_without_a_misleading_stash(self) -> None:
        result = self.preserve(run_id=18)
        self.assertEqual(result.preservation_status, "complete")
        self.assertTrue(result.extra.get("no_changes"))
        self.assertIsNone(result.stash_sha)
        restored = recovery.restore(
            Path(result.manifest_path), self.root / "clean-restored", git_bin=GIT
        )
        self.assertEqual(git(restored, "status", "--short"), "")

    def test_ref_publication_interruption_leaves_pending_package_for_resume(self) -> None:
        (self.repo / "tracked.txt").write_text("pending\n", encoding="utf-8")
        with mock.patch.object(recovery, "_pin_ref", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.preserve(run_id=19)
        pending_manifest = self.package() / "manifest.json"
        self.assertTrue(pending_manifest.exists())
        pending = json.loads(pending_manifest.read_text(encoding="utf-8"))
        self.assertFalse(pending["complete"])
        self.assertEqual(pending["preservation_status"], "pending")
        planned_stash = pending.get("stash_sha")
        resumed = self.preserve(run_id=19)
        self.assertEqual(resumed.preservation_status, "complete")
        self.assertEqual(resumed.stash_sha, planned_stash)

    def test_retry_reuses_complete_package_and_cleanup_status_is_atomic(self) -> None:
        result = self.preserve(run_id=14)
        again = self.preserve(run_id=14)
        self.assertEqual(result.archive_id, again.archive_id)
        recovery.mark_cleanup(again, "complete")
        loaded = recovery.load(self.package())
        self.assertEqual(loaded.cleanup_status, "complete")


if __name__ == "__main__":
    unittest.main()
