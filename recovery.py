#!/usr/bin/env python3
"""Durable, local recovery packages for a Bifrost repair attempt.

The monitor can be interrupted at any point while it is repairing a checkout.
This module deliberately separates preservation from cleanup.  Preservation
only reads the checkout (apart from creating objects and private recovery
refs); cleanup is an explicit operation owned by the caller.

Recovery packages use a small content addressed archive.  A synthetic stash
commit is also made for ordinary (non-conflict) checkouts, so a restored
checkout can use Git's normal ``stash apply --index`` machinery.  The archive
remains the source of truth, including for an unmerged index, merge metadata,
and filenames that are awkward to pass through line-oriented Git commands.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_GIT_BIN = "/usr/bin/git"
RECOVERY_REF_ROOT = "refs/tags/bifrost-ci-recovery"
ZERO_OID = "0" * 40


class RecoveryError(RuntimeError):
    """A preservation or restoration invariant could not be established."""


class GitError(RecoveryError):
    """A Git command failed while collecting or restoring a package."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise RecoveryError("invalid base64 path or payload in recovery manifest") from exc


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "surrogateescape")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_text_atomic(path: Path, text: str) -> None:
    """Write UTF-8 text atomically and durably.

    The monitor uses this for its handoff note.  It is public because callers
    should not need to duplicate the fsync-and-replace details.
    """

    _atomic_write_bytes(Path(path), text.encode("utf-8", "surrogateescape"))


def _write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(
        Path(path),
        (_canonical(value) + b"\n"),
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"could not read recovery manifest/artifact {path}: {exc}") from exc


def _decode_path(root: Path, encoded: str) -> tuple[bytes, Path]:
    raw = _unb64(encoded)
    if not raw or b"\0" in raw or raw.startswith(b"/"):
        raise RecoveryError("invalid absolute or empty path in recovery archive")
    parts = raw.split(b"/")
    if any(part in (b"", b".", b"..") for part in parts):
        raise RecoveryError("invalid path component in recovery archive")
    decoded = os.fsdecode(raw)
    return raw, root.joinpath(*decoded.split("/"))


def _run_git(
    git_bin: str,
    args: Sequence[str | bytes],
    *,
    cwd: Path | bytes,
    input_bytes: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> bytes:
    command: list[str | bytes] = [os.fspath(git_bin), *args]
    merged_env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        merged_env.pop(name, None)
    merged_env["GIT_OPTIONAL_LOCKS"] = "0"
    if env:
        merged_env.update(env)
    try:
        process = subprocess.run(
            command,
            cwd=cwd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"{os.fspath(git_bin)} failed to run: {exc}") from exc
    if process.returncode:
        output = process.stderr or process.stdout
        detail = output.decode("utf-8", "surrogateescape").strip()
        command_text = " ".join(os.fsdecode(item) for item in command[:4])
        raise GitError(
            f"{command_text} exited {process.returncode}"
            + (f": {detail}" if detail else "")
        )
    return process.stdout


def _git_text(
    git_bin: str,
    args: Sequence[str | bytes],
    *,
    cwd: Path | bytes,
    input_bytes: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    return _run_git(git_bin, args, cwd=cwd, input_bytes=input_bytes, env=env).decode(
        "utf-8", "surrogateescape"
    ).strip()


def _git_path(git_bin: str, root: Path, name: str) -> Path:
    value = _git_text(git_bin, ["rev-parse", "--git-path", name], cwd=root)
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path


def _git_oid(git_bin: str, root: Path, ref: str) -> str | None:
    try:
        return _git_text(git_bin, ["rev-parse", "--verify", ref], cwd=root)
    except GitError:
        return None


def _safe_path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _has_unsafe_parent(root: Path, path: Path) -> bool:
    """Whether a relative path would traverse a symlink or non-directory."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            return True
    return False


def _remove_path(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _write_blob(blob_root: Path, raw: bytes) -> tuple[str, str]:
    digest = _sha256(raw)
    relative = f"archive/blobs/{digest}"
    target = blob_root / digest
    if target.exists():
        try:
            existing_digest = _sha256(target.read_bytes())
        except OSError as exc:
            raise RecoveryError(f"could not verify existing recovery blob {target}: {exc}") from exc
        if existing_digest != digest:
            raise RecoveryError(f"recovery blob collision or corruption at {target}")
        return relative, digest
    _atomic_write_bytes(target, raw)
    return relative, digest


def _parse_ls_files(data: bytes) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in data.split(b"\0"):
        if not item:
            continue
        try:
            left, raw_path = item.split(b"\t", 1)
            mode_raw, oid_raw, stage_raw = left.split()
            mode = int(mode_raw, 8)
            stage = int(stage_raw)
            oid = oid_raw.decode("ascii")
        except (ValueError, UnicodeError) as exc:
            raise RecoveryError("Git returned an invalid ls-files --stage record") from exc
        if len(oid) not in (40, 64) or any(c not in "0123456789abcdef" for c in oid):
            raise RecoveryError("Git returned an invalid object id in the index")
        records.append(
            {
                "path": _b64(raw_path),
                "raw_path": raw_path,
                "mode": mode,
                "stage": stage,
                "oid": oid,
                "line": _b64(item),
            }
        )
    return records


def _path_record(root: Path, raw_path: bytes, *, blob_root: Path) -> dict[str, Any]:
    encoded = _b64(raw_path)
    _, path = _decode_path(root, encoded)
    if _has_unsafe_parent(root, path):
        return {"path": encoded, "state": "absent"}
    if not _safe_path_exists(path):
        return {"path": encoded, "state": "absent"}
    if path.is_dir() and not path.is_symlink():
        if (path / ".git").exists():
            raise RecoveryError(f"nested repository cannot be preserved: {path}")
        return {"path": encoded, "state": "directory"}
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RecoveryError(f"could not stat preserved path {path}: {exc}") from exc
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(os.fsencode(path))
        artifact, digest = _write_blob(blob_root, target)
        return {
            "path": encoded,
            "state": "present",
            "kind": "symlink",
            "mode": mode,
            "artifact": artifact,
            "blob_id": digest,
        }
    if stat.S_ISREG(info.st_mode):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RecoveryError(f"could not read preserved path {path}: {exc}") from exc
        artifact, digest = _write_blob(blob_root, raw)
        return {
            "path": encoded,
            "state": "present",
            "kind": "file",
            "mode": mode,
            "artifact": artifact,
            "blob_id": digest,
        }
    raise RecoveryError(f"unsupported filesystem object in recovery path {path}")


MERGE_METADATA = ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "AUTO_MERGE")


def _metadata_targets(git_bin: str, root: Path) -> list[dict[str, Any]]:
    for name in ("CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_START", "sequencer", "rebase-merge", "rebase-apply"):
        if _safe_path_exists(_git_path(git_bin, root, name)):
            raise RecoveryError(f"unsupported Git operation during recovery: {name}")
    targets = []
    for name in MERGE_METADATA:
        path = _git_path(git_bin, root, name)
        if not _safe_path_exists(path):
            continue
        if path.is_symlink() or not path.is_file():
            raise RecoveryError(f"unsupported Git metadata object: {path}")
        targets.append({"name": name, "path": path})
    return targets


def _capture_metadata(
    targets: Iterable[dict[str, Any]], *, blob_root: Path
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for target in targets:
        path = Path(target["path"])
        try:
            raw = path.read_bytes()
            mode = stat.S_IMODE(os.lstat(path).st_mode)
        except OSError as exc:
            raise RecoveryError(f"could not read Git metadata {path}: {exc}") from exc
        artifact, digest = _write_blob(blob_root, raw)
        files.append(
            {
                "name": str(target["name"]),
                "mode": mode,
                "artifact": artifact,
                "blob_id": digest,
            }
        )
    files.sort(key=lambda item: item["name"])
    return {"format": "bifrost-recovery-metadata-v1", "files": files}


def _capture_index(
    git_bin: str, root: Path, records: list[dict[str, Any]], *, blob_root: Path,
) -> list[dict[str, Any]]:
    # Read unique blobs in batches rather than spawning two processes per file.
    if any(record["mode"] == 0o160000 for record in records):
        raise RecoveryError("submodule worktrees are not supported by recovery; source left intact")
    oids = sorted({record["oid"] for record in records if set(record["oid"]) != {"0"}})
    blobs = {}
    for offset in range(0, len(oids), 128):
        batch = oids[offset:offset + 128]
        payload = _run_git(git_bin, ["cat-file", "--batch"], cwd=root,
                           input_bytes=("\n".join(batch) + "\n").encode())
        cursor = 0
        for oid in batch:
            newline = payload.index(b"\n", cursor)
            header = payload[cursor:newline].split()
            if len(header) != 3 or header[0].decode() != oid or header[1] != b"blob":
                raise RecoveryError(f"missing or invalid index blob: {oid}")
            size = int(header[2])
            raw = payload[newline + 1:newline + 1 + size]
            if len(raw) != size:
                raise RecoveryError(f"truncated index blob: {oid}")
            cursor = newline + size + 2
            blobs[oid] = _write_blob(blob_root, raw)
    result = []
    for record in records:
        item = {key: value for key, value in record.items() if key != "raw_path"}
        if record["oid"] in blobs:
            artifact, digest = blobs[record["oid"]]
            item.update(object_type="blob", artifact=artifact, blob_id=digest)
        else:
            raise RecoveryError("intent-to-add index entries are not supported by recovery")
        result.append(item)
    return result


def _source_state(
    root: Path,
    *,
    git_bin: str,
    archive: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], bool, str, str | None]:
    index_raw = _run_git(git_bin, ["ls-files", "--stage", "-z"], cwd=root)
    index_records = _parse_ls_files(index_raw)
    unmerged = _run_git(git_bin, ["ls-files", "--unmerged", "-z"], cwd=root)
    unmerged_present = bool(unmerged.strip(b"\0"))
    # The index can omit a path that was staged for deletion.  Include the
    # HEAD tree so that the archive records that path as absent and conflict
    # restoration removes the checkout's copy.
    head_raw_paths = [
        item
        for item in _run_git(git_bin, ["ls-tree", "-r", "--name-only", "-z", "HEAD"], cwd=root).split(b"\0")
        if item
    ]
    tracked_raw_paths = sorted(
        set(head_raw_paths) | {bytes(item["raw_path"]) for item in index_records}
    )
    untracked_raw_paths = sorted(
        item for item in _run_git(
            git_bin,
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=root,
        ).split(b"\0")
        if item
    )
    blob_root = archive / "blobs"
    blob_root.mkdir(parents=True, exist_ok=True)
    index = _capture_index(git_bin, root, index_records, blob_root=blob_root)
    paths = tracked_raw_paths + [item for item in untracked_raw_paths if item not in tracked_raw_paths]
    worktree = [_path_record(root, item, blob_root=blob_root) for item in paths]
    worktree.sort(key=lambda item: _unb64(item["path"]))
    metadata = _capture_metadata(_metadata_targets(git_bin, root), blob_root=blob_root)
    unsupported_metadata = [
        item["name"]
        for item in metadata["files"]
        if item["name"] in {"CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_START", "BISECT_HEAD", "BISECT_LOG"}
        or item["name"].startswith("sequencer/")
        or item["name"].startswith("rebase-merge/")
        or item["name"].startswith("rebase-apply/")
    ]
    if unsupported_metadata:
        raise RecoveryError(
            "unsupported non-merge Git metadata: " + ", ".join(sorted(unsupported_metadata))
        )
    conflict = unmerged_present or bool(metadata["files"])
    head_sha = _git_text(git_bin, ["rev-parse", "--verify", "HEAD"], cwd=root)
    source_head_ref: str | None
    try:
        source_head_ref = _git_text(git_bin, ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=root)
    except GitError:
        source_head_ref = None
    return index, worktree, metadata, conflict, head_sha, source_head_ref


def _temporary_index(
    root: Path,
    git_bin: str,
    stage: Path,
    *,
    copy_current: bool,
) -> tuple[Path, dict[str, str]]:
    index_path = _git_path(git_bin, root, "index")
    temporary = stage / f".index-{uuid.uuid4().hex}"
    if copy_current:
        if not index_path.exists():
            raise RecoveryError(f"Git index does not exist: {index_path}")
        shutil.copyfile(index_path, temporary)
        os.chmod(temporary, 0o600)
    return temporary, {"GIT_INDEX_FILE": str(temporary)}


def _write_tree_from_paths(
    root: Path,
    git_bin: str,
    stage: Path,
    paths: Sequence[bytes],
) -> str:
    temporary, env = _temporary_index(root, git_bin, stage, copy_current=False)
    try:
        _run_git(git_bin, ["read-tree", "--empty"], cwd=root, env=env)
        if paths:
            payload = b"\0".join(paths) + b"\0"
            _run_git(
                git_bin,
                ["add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
                cwd=root,
                input_bytes=payload,
                env={**env, "GIT_LITERAL_PATHSPECS": "1"},
            )
        return _git_text(git_bin, ["write-tree"], cwd=root, env=env)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _commit_tree(
    git_bin: str,
    root: Path,
    tree: str,
    parents: Sequence[str],
    message: str,
) -> str:
    env = {
        "GIT_AUTHOR_NAME": "Bifrost CI recovery",
        "GIT_AUTHOR_EMAIL": "bifrost-ci-recovery@localhost",
        "GIT_COMMITTER_NAME": "Bifrost CI recovery",
        "GIT_COMMITTER_EMAIL": "bifrost-ci-recovery@localhost",
    }
    args: list[str] = ["commit-tree", tree]
    for parent in parents:
        args.extend(["-p", parent])
    return _git_text(git_bin, args, cwd=root, input_bytes=message.encode(), env=env)


def _make_stash(
    root: Path,
    git_bin: str,
    stage: Path,
    *,
    head_sha: str,
    index_records: list[dict[str, Any]],
    worktree_records: list[dict[str, Any]],
    conflict: bool,
    run_id: int,
    attempt: int,
) -> str:
    tracked = [bytes(_unb64(item["path"])) for item in index_records]
    present_tracked: list[bytes] = []
    untracked: list[bytes] = []
    tracked_set = set(tracked)
    for item in worktree_records:
        if item.get("state") != "present":
            continue
        raw = _unb64(item["path"])
        if raw in tracked_set:
            present_tracked.append(raw)
        else:
            untracked.append(raw)
    worktree_tree = _write_tree_from_paths(root, git_bin, stage, present_tracked)
    untracked_tree = (
        _write_tree_from_paths(root, git_bin, stage, untracked) if untracked else None
    )
    if conflict:
        index_tree = _git_text(git_bin, ["rev-parse", f"{head_sha}^{{tree}}"], cwd=root)
    else:
        temporary, env = _temporary_index(root, git_bin, stage, copy_current=True)
        try:
            index_tree = _git_text_with_env(git_bin, ["write-tree"], root, env)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    index_commit = _commit_tree(
        git_bin,
        root,
        index_tree,
        [head_sha],
        f"Bifrost recovery index {run_id}/{attempt}",
    )
    parents = [head_sha, index_commit]
    if untracked_tree:
        untracked_commit = _commit_tree(
            git_bin,
            root,
            untracked_tree,
            [],
            f"Bifrost recovery untracked {run_id}/{attempt}",
        )
        parents.append(untracked_commit)
    return _commit_tree(
        git_bin,
        root,
        worktree_tree,
        parents,
        f"Bifrost recovery stash {run_id}/{attempt}",
    )


def _git_text_with_env(
    git_bin: str, args: Sequence[str | bytes], root: Path, env: Mapping[str, str]
) -> str:
    return _git_text(git_bin, args, cwd=root, env=env)


def _pin_ref(git_bin: str, root: Path, ref: str, oid: str) -> None:
    current = _git_oid(git_bin, root, ref)
    if current is not None:
        if current != oid:
            raise RecoveryError(f"recovery ref already points elsewhere: {ref}")
        return
    try:
        _run_git(git_bin, ["update-ref", ref, oid, ""], cwd=root)
    except GitError:
        # If another recovery invocation won the race, accepting the same
        # object is safe.  A different object is never silently adopted.
        current = _git_oid(git_bin, root, ref)
        if current != oid:
            raise


def _compute_archive_id(artifacts: Mapping[str, str]) -> str:
    return _sha256(_canonical({"schema_version": SCHEMA_VERSION, "artifacts": dict(sorted(artifacts.items()))}))


def _artifact_path(package: Path, relative: str) -> Path:
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise RecoveryError(f"invalid recovery artifact path {relative!r}")
    return package / relative


def _verify_artifacts(package: Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RecoveryError("unsupported recovery manifest schema")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise RecoveryError("recovery manifest has no immutable artifact ids")
    normalized: dict[str, str] = {}
    for relative, expected in artifacts.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise RecoveryError("invalid artifact id in recovery manifest")
        target = _artifact_path(package, relative)
        try:
            actual = _sha256(target.read_bytes())
        except OSError as exc:
            raise RecoveryError(f"missing or unreadable recovery artifact {target}: {exc}") from exc
        if actual != expected:
            raise RecoveryError(f"recovery artifact checksum mismatch: {target}")
        normalized[relative] = expected
    if manifest.get("archive_id") != _compute_archive_id(normalized):
        raise RecoveryError("recovery archive id does not match its artifact ids")
    for relative in ("archive/index.json", "archive/worktree.json", "archive/metadata.json", "transcript.txt"):
        if relative not in normalized:
            raise RecoveryError(f"recovery manifest omits required artifact {relative}")
    index = _read_json(_artifact_path(package, "archive/index.json"))
    worktree = _read_json(_artifact_path(package, "archive/worktree.json"))
    metadata = _read_json(_artifact_path(package, "archive/metadata.json"))
    if not isinstance(index, list) or not isinstance(worktree, list) or not isinstance(metadata, dict):
        raise RecoveryError("invalid recovery archive record format")
    for item in [*index, *worktree, *metadata.get("files", [])]:
        if not isinstance(item, dict):
            raise RecoveryError("invalid recovery archive record")
        artifact = item.get("artifact")
        if artifact:
            if artifact not in normalized:
                raise RecoveryError(f"archive record references missing artifact {artifact}")
            if item.get("blob_id") != normalized[artifact]:
                raise RecoveryError(f"archive record has wrong immutable blob id {artifact}")
    for item in worktree:
        if not isinstance(item, dict) or "path" not in item:
            raise RecoveryError("invalid worktree record")
        _decode_path(Path("."), item["path"])
        if item.get("state") == "present" and item.get("kind") not in ("file", "symlink"):
            raise RecoveryError("invalid present worktree record")
    for item in index:
        if not isinstance(item, dict) or "path" not in item or "line" not in item:
            raise RecoveryError("invalid index record")
        _decode_path(Path("."), item["path"])
    if manifest.get("conflict_snapshot"):
        conflict_path = Path(str(manifest["conflict_snapshot"]))
        if not conflict_path.is_absolute():
            conflict_path = package / conflict_path
        elif not conflict_path.exists():
            # During the staged verification the manifest already contains
            # the final package path.  Resolve that immutable relative
            # artifact against the staging directory for this check.
            conflict_path = package / "archive" / conflict_path.name
        if not conflict_path.exists():
            raise RecoveryError(f"missing conflict snapshot {conflict_path}")


def _verify_refs(result: "RecoveryResult", *, git_bin: str) -> None:
    if not result.repo_root:
        raise RecoveryError("recovery manifest has no source repository")
    root = Path(result.repo_root)
    if not result.head_ref or not result.head_sha:
        raise RecoveryError("recovery manifest has no pinned HEAD")
    if bool(result.stash_ref) != bool(result.stash_sha):
        raise RecoveryError("recovery manifest has incomplete stash pointers")
    if result.head_ref and result.head_sha:
        if _git_oid(git_bin, root, result.head_ref) != result.head_sha:
            raise RecoveryError(f"pinned HEAD ref is missing or changed: {result.head_ref}")
    if result.stash_ref and result.stash_sha:
        if _git_oid(git_bin, root, result.stash_ref) != result.stash_sha:
            raise RecoveryError(f"pinned stash ref is missing or changed: {result.stash_ref}")
    refs = result.extra.get("merge_refs", [])
    if not isinstance(refs, list):
        raise RecoveryError("invalid merge refs in recovery manifest")
    for pair in refs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise RecoveryError("invalid merge ref entry in recovery manifest")
        ref, oid = pair
        if _git_oid(git_bin, root, ref) != oid:
            raise RecoveryError(f"pinned merge parent ref is missing or changed: {ref}")
    refs = result.extra.get("metadata_refs", [])
    if not isinstance(refs, list):
        raise RecoveryError("invalid metadata refs in recovery manifest")
    for pair in refs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise RecoveryError("invalid metadata ref entry in recovery manifest")
        ref, oid = pair
        if _git_oid(git_bin, root, ref) != oid:
            raise RecoveryError(f"pinned metadata ref is missing or changed: {ref}")


@dataclass
class RecoveryResult:
    manifest_path: str
    preservation_status: str = "failed"
    cleanup_status: str = "pending"
    error: str = ""
    head_sha: str | None = None
    head_ref: str | None = None
    stash_sha: str | None = None
    stash_ref: str | None = None
    conflict_snapshot: str | None = None
    run_id: int | None = None
    attempt: int | None = None
    sha: str | None = None
    session_id: str | None = None
    transcript_path: str | None = None
    package_dir: str | None = None
    repo_root: str | None = None
    host: str | None = None
    source_head_ref: str | None = None
    archive_id: str | None = None
    complete: bool = False
    created_at: str | None = None
    git_bin: str = DEFAULT_GIT_BIN
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def detail(self) -> str:
        lines = [
            f"Recovery preservation: {self.preservation_status}",
            f"Cleanup: {self.cleanup_status}",
            f"Manifest: {self.manifest_path}",
        ]
        if self.repo_root:
            lines.append(f"Repository: {self.repo_root}")
        if self.host:
            lines.append(f"Host: {self.host}")
        if self.session_id:
            lines.append(f"Session: {self.session_id}")
        if self.head_sha:
            lines.append(f"HEAD: {self.head_sha} ({self.head_ref or 'unpinned'})")
        if self.stash_sha:
            lines.append(f"Stash: {self.stash_sha} ({self.stash_ref or 'unpinned'})")
        if self.conflict_snapshot:
            lines.append(f"Conflict snapshot: {self.conflict_snapshot}")
        if self.transcript_path:
            lines.append(f"Transcript: {self.transcript_path} (local-only)")
        if self.error:
            lines.append(f"Error: {self.error}")
        if self.preservation_status == "complete":
            lines.append("Preservation made no edits to the source worktree or its index.")
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryResult":
        known = {item.name for item in cls.__dataclass_fields__.values() if item.name != "extra"}
        values = {key: value[key] for key in known if key in value}
        values["extra"] = dict(value.get("extra", {}))
        values["extra"].update({key: item for key, item in value.items() if key not in known and key != "extra"})
        return cls(**values)

    @classmethod
    def from_manifest(cls, manifest_path: Path) -> "RecoveryResult":
        value = _read_json(Path(manifest_path))
        if not isinstance(value, dict):
            raise RecoveryError("recovery manifest is not a JSON object")
        result = cls.from_dict(value)
        result.manifest_path = str(Path(manifest_path).resolve())
        return result

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("extra", None)
        value.update(self.extra)
        return value


def load(package_dir: Path) -> RecoveryResult:
    """Load and fully verify a complete recovery package."""

    package = Path(package_dir)
    manifest_path = package if package.name == "manifest.json" and package.is_file() else package / "manifest.json"
    result = RecoveryResult.from_manifest(manifest_path)
    if not result.complete or result.preservation_status != "complete":
        raise RecoveryError(f"recovery package is incomplete: {manifest_path}")
    _verify_artifacts(manifest_path.parent, result.to_dict())
    _verify_refs(result, git_bin=result.git_bin or DEFAULT_GIT_BIN)
    return result


def _failure_manifest(package_dir: Path, result: RecoveryResult) -> None:
    package_dir.mkdir(parents=True, exist_ok=True)
    _write_json(package_dir / "manifest.json", result.to_dict())


def _install_stage(stage: Path, package_dir: Path) -> None:
    package_dir.parent.mkdir(parents=True, exist_ok=True)
    quarantine: Path | None = None
    if package_dir.exists():
        quarantine = package_dir.with_name(
            f"{package_dir.name}.incomplete-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        )
        os.replace(package_dir, quarantine)
    try:
        os.replace(stage, package_dir)
        _fsync_directory(package_dir.parent)
    except Exception:
        if quarantine is not None and not package_dir.exists():
            os.replace(quarantine, package_dir)
        raise


def _resume_pending(
    package_dir: Path,
    *,
    run_id: int,
    attempt: int,
    sha: str,
    git_bin: str,
) -> RecoveryResult:
    """Finish a package installed before its local refs were published."""

    result = RecoveryResult.from_manifest(package_dir / "manifest.json")
    if (
        result.complete
        or result.preservation_status != "pending"
        or result.run_id != run_id
        or result.attempt != attempt
        or result.sha != sha
    ):
        raise RecoveryError("pending recovery package identity or state does not match this attempt")
    manifest = result.to_dict()
    _verify_artifacts(package_dir, manifest)
    if not result.repo_root or not result.head_sha or not result.head_ref:
        raise RecoveryError("pending recovery package has no planned HEAD ref")
    _pin_ref(git_bin, Path(result.repo_root), result.head_ref, result.head_sha)
    if result.stash_sha and result.stash_ref:
        _pin_ref(git_bin, Path(result.repo_root), result.stash_ref, result.stash_sha)
    for pair in [*result.extra.get("merge_refs", []), *result.extra.get("metadata_refs", [])]:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise RecoveryError("invalid planned recovery ref")
        _pin_ref(git_bin, Path(result.repo_root), str(pair[0]), str(pair[1]))
    result.preservation_status = "complete"
    result.complete = True
    result.error = ""
    manifest = result.to_dict()
    manifest.update(
        {
            "schema_version": SCHEMA_VERSION,
            "complete": True,
            "preservation_status": "complete",
            "cleanup_status": result.cleanup_status or "pending",
        }
    )
    _verify_refs(result, git_bin=git_bin)
    _write_json(package_dir / "manifest.json", manifest)
    return load(package_dir)


def _planned_metadata_refs(
    git_bin: str,
    root: Path,
    package: Path,
    metadata: Mapping[str, Any],
    *,
    run_id: int,
    attempt: int,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return refs that keep merge parents and AUTO_MERGE reachable."""

    merge_refs: list[tuple[str, str]] = []
    merge_record = next((item for item in metadata.get("files", []) if item.get("name") == "MERGE_HEAD"), None)
    if merge_record:
        merge_bytes = _artifact_path(package, merge_record["artifact"]).read_bytes()
        for position, line in enumerate(merge_bytes.splitlines()):
            merge_oid = line.decode("ascii", "strict").strip()
            if not merge_oid:
                continue
            _git_text(git_bin, ["rev-parse", "--verify", f"{merge_oid}^{{commit}}"], cwd=root)
            merge_refs.append(
                (f"{RECOVERY_REF_ROOT}/{run_id}/{attempt}/merge/{position}", merge_oid)
            )
    metadata_refs: list[tuple[str, str]] = []
    auto_record = next((item for item in metadata.get("files", []) if item.get("name") == "AUTO_MERGE"), None)
    if auto_record:
        raw = _artifact_path(package, auto_record["artifact"]).read_bytes().strip()
        try:
            auto_oid = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RecoveryError("AUTO_MERGE metadata is not an object id") from exc
        if _git_text(git_bin, ["cat-file", "-t", auto_oid], cwd=root) != "tree":
            raise RecoveryError("AUTO_MERGE metadata does not name a tree")
        metadata_refs.append(
            (f"{RECOVERY_REF_ROOT}/{run_id}/{attempt}/auto-merge", auto_oid)
        )
    return merge_refs, metadata_refs


def preserve(
    worktree: Path, package_dir: Path, *, run_id: int, attempt: int, sha: str,
    session_id: str | None, transcript: str, git_bin: str = DEFAULT_GIT_BIN,
) -> RecoveryResult:
    """Capture without editing the source, then publish durable recovery refs.

    The complete archive and intended refs are installed as a pending manifest
    before publishing any refs. Restarting finishes that exact package, even if
    the source worktree has subsequently changed.
    """
    worktree, package_dir = Path(worktree).resolve(), Path(package_dir).resolve()
    manifest_path = package_dir / "manifest.json"
    base = RecoveryResult(
        manifest_path=str(manifest_path), run_id=run_id, attempt=attempt, sha=sha,
        session_id=session_id, package_dir=str(package_dir),
        transcript_path=str(package_dir / "transcript.txt"), created_at=_now(),
        repo_root=str(worktree), host=socket.gethostname(), git_bin=git_bin,
    )
    stage = None
    protected = manifest_path.exists()
    try:
        if manifest_path.exists():
            previous = RecoveryResult.from_manifest(manifest_path)
            if (previous.run_id, previous.attempt, previous.sha, previous.repo_root) != (run_id, attempt, sha, str(worktree)):
                raise RecoveryError("recovery package identity does not match this attempt")
            if previous.complete:
                return load(package_dir)
            if previous.preservation_status == "pending":
                return _resume_pending(package_dir, run_id=run_id, attempt=attempt, sha=sha, git_bin=git_bin)
            # Failed captures never published refs or authorized source cleanup.
            # Retain them in quarantine when installing a replacement capture.
            if previous.extra.get("artifacts"):
                raise RecoveryError("incomplete package with artifacts requires inspection; refusing to replace it")
            protected = False
        elif package_dir.exists() and any(package_dir.iterdir()):
            raise RecoveryError(f"recovery directory has no manifest: {package_dir}")
        if not worktree.is_dir():
            raise RecoveryError(f"worktree does not exist: {worktree}")
        repo_root = Path(_git_text(git_bin, ["rev-parse", "--show-toplevel"], cwd=worktree)).resolve()
        if repo_root != worktree:
            raise RecoveryError(f"worktree path is not the repository root: {worktree}")
        package_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(prefix=f".{package_dir.name}.stage-", dir=package_dir.parent))
        archive = stage / "archive"
        archive.mkdir(mode=0o700)
        index, working, metadata, conflict, head, branch = _source_state(repo_root, git_bin=git_bin, archive=archive)
        base.head_sha, base.source_head_ref = head, branch
        base.conflict_snapshot = str(package_dir / "archive/conflict.json") if conflict else None
        _write_json(archive / "index.json", index)
        _write_json(archive / "worktree.json", working)
        _write_json(archive / "metadata.json", metadata)
        write_text_atomic(stage / "transcript.txt", transcript)
        if conflict:
            _write_json(archive / "conflict.json", {
                "index": "archive/index.json", "worktree": "archive/worktree.json",
                "metadata": "archive/metadata.json",
            })
        artifacts = {
            str(path.relative_to(stage)): _sha256(path.read_bytes())
            for path in stage.rglob("*") if path.is_file()
        }
        base.archive_id = _compute_archive_id(artifacts)
        base.extra.update(schema_version=SCHEMA_VERSION, artifacts=artifacts, conflict=conflict)
        _verify_artifacts(stage, base.to_dict())
        status = _run_git(git_bin, ["status", "--porcelain=v1", "--untracked-files=all"], cwd=repo_root)
        base.extra["no_changes"] = not status and not conflict
        if status and not conflict:
            base.stash_sha = _make_stash(repo_root, git_bin, stage, head_sha=head,
                                        index_records=index, worktree_records=working,
                                        conflict=False, run_id=run_id, attempt=attempt)
            base.stash_ref = f"{RECOVERY_REF_ROOT}/{run_id}/{attempt}/stash"
        base.head_ref = f"{RECOVERY_REF_ROOT}/{run_id}/{attempt}/head"
        merge_refs, metadata_refs = _planned_metadata_refs(
            git_bin, repo_root, stage, metadata, run_id=run_id, attempt=attempt,
        )
        base.extra.update(merge_refs=merge_refs, metadata_refs=metadata_refs)
        base.preservation_status = "pending"
        _write_json(stage / "manifest.json", base.to_dict())
        _verify_artifacts(stage, base.to_dict())
        _install_stage(stage, package_dir)
        stage = None
        protected = True
        return _resume_pending(package_dir, run_id=run_id, attempt=attempt, sha=sha, git_bin=git_bin)
    except (RecoveryError, OSError, ValueError, TypeError) as exc:
        base.preservation_status, base.complete, base.error = "failed", False, str(exc)
        if not protected:
            try:
                # Keep diagnosis accessible even when Git preservation failed.
                write_text_atomic(package_dir / "transcript.txt", transcript)
                base.extra = {}
                _failure_manifest(package_dir, base)
            except OSError:
                pass
        return base
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def mark_cleanup(
    result: RecoveryResult,
    status: str,
    error: str = "",
) -> RecoveryResult:
    """Atomically record the caller-owned cleanup result in the manifest."""

    if status not in {"pending", "complete", "failed"}:
        raise ValueError(f"invalid cleanup status {status!r}")
    result.cleanup_status = status
    result.error = error
    result.manifest_path = str(Path(result.manifest_path).resolve())
    _write_json(Path(result.manifest_path), result.to_dict())
    return result


def _restore_file(path: Path, kind: str, mode: int, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _safe_path_exists(path):
        _remove_path(path)
    if kind == "symlink":
        os.symlink(os.fsdecode(raw), path)
        return
    if kind != "file":
        raise RecoveryError(f"unsupported archived worktree kind {kind!r}")
    _atomic_write_bytes(path, raw, mode=mode)
    os.chmod(path, mode)


def _restore_metadata(git_bin: str, destination: Path, package: Path, metadata: Mapping[str, Any]) -> None:
    for name in MERGE_METADATA:
        target = _git_path(git_bin, destination, name)
        if _safe_path_exists(target):
            _remove_path(target)
    for item in metadata.get("files", []):
        if item["name"] not in MERGE_METADATA:
            raise RecoveryError(f"unsupported archived Git metadata: {item['name']}")
        target = _git_path(git_bin, destination, item["name"])
        raw = _artifact_path(package, item["artifact"]).read_bytes()
        _atomic_write_bytes(target, raw, mode=int(item.get("mode") or 0o600))


def _restore_worktree(package: Path, destination: Path, git_bin: str) -> None:
    """Restore exact raw bytes and modes, without traversing symlink parents."""
    records = _read_json(package / "archive/worktree.json")
    paths = {_unb64(item["path"]) for item in records}
    paths.update(path for path in _run_git(git_bin, ["ls-files", "-z"], cwd=destination).split(b"\0") if path)
    # This destination is newly created by restore(). Remove tracked obstructions
    # before recreating directory/file/symlink transitions from the archive.
    for raw in sorted(paths, key=lambda item: (item.count(b"/"), item)):
        _, path = _decode_path(destination, _b64(raw))
        if not _has_unsafe_parent(destination, path) and _safe_path_exists(path):
            _remove_path(path)
    for item in sorted(records, key=lambda item: (_unb64(item["path"]).count(b"/"), _unb64(item["path"]))):
        if item["state"] == "absent":
            continue
        _, path = _decode_path(destination, item["path"])
        if _has_unsafe_parent(destination, path):
            raise RecoveryError(f"refusing to restore through a non-directory parent: {path}")
        if item["state"] == "directory":
            path.mkdir(parents=True, exist_ok=True)
        elif item["state"] == "present":
            raw = _artifact_path(package, item["artifact"]).read_bytes()
            _restore_file(path, item["kind"], int(item["mode"]), raw)
        else:
            raise RecoveryError(f"invalid archived file state: {item['state']}")


def _restore_index(package: Path, destination: Path, git_bin: str) -> None:
    index = _read_json(package / "archive/index.json")
    by_oid = {item["oid"]: item for item in index}
    if by_oid:
        checks = _run_git(git_bin, ["cat-file", "--batch-check"], cwd=destination,
                          input_bytes=("\n".join(by_oid) + "\n").encode()).splitlines()
        for line in checks:
            fields = line.decode().split()
            oid = fields[0]
            if len(fields) == 2 and fields[1] == "missing":
                item = by_oid[oid]
                raw = _artifact_path(package, item["artifact"]).read_bytes()
                actual = _git_text(git_bin, ["hash-object", "-w", "--stdin"], cwd=destination, input_bytes=raw)
                if actual != oid:
                    raise RecoveryError(f"archived index blob hash changed: {oid}")
            elif len(fields) != 3 or fields[1] != "blob":
                raise RecoveryError(f"invalid index object: {line!r}")
    _run_git(git_bin, ["read-tree", "--empty"], cwd=destination)
    if index:
        _run_git(git_bin, ["update-index", "-z", "--index-info"], cwd=destination,
                 input_bytes=b"\0".join(_unb64(item["line"]) for item in index) + b"\0")
    expected = b"\0".join(_unb64(item["line"]) for item in index) + (b"\0" if index else b"")
    if _run_git(git_bin, ["ls-files", "--stage", "-z"], cwd=destination) != expected:
        raise RecoveryError("restored index does not match the saved index stages")


def restore(manifest_path: Path, destination: Path, git_bin: str = DEFAULT_GIT_BIN) -> Path:
    """Create a new detached worktree and restore the verified original state."""
    result = load(manifest_path)
    destination = Path(destination).absolute()
    if os.path.lexists(destination):
        raise RecoveryError(f"restore destination already exists: {destination}")
    destination = destination.resolve()
    root, package = Path(result.repo_root), Path(result.manifest_path).parent
    if destination == root or root in destination.parents or destination == package or package in destination.parents:
        raise RecoveryError("restore destination must be outside the source worktree and recovery package")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run_git(git_bin, ["worktree", "add", "--detach", str(destination), result.head_sha], cwd=root)
    try:
        if result.stash_sha and not result.conflict_snapshot:
            try:
                _run_git(git_bin, ["stash", "apply", "--index", result.stash_sha], cwd=destination)
            except GitError:
                # Git cannot apply every directory/file transition as a stash.
                # The verified full archive remains authoritative in these cases.
                _run_git(git_bin, ["reset", "--hard", result.head_sha], cwd=destination)
                _run_git(git_bin, ["clean", "-fd"], cwd=destination)
        _restore_worktree(package, destination, git_bin)
        _restore_index(package, destination, git_bin)
        metadata = _read_json(package / "archive/metadata.json")
        _restore_metadata(git_bin, destination, package, metadata)
        return destination
    except (RecoveryError, OSError, ValueError):
        try:
            _run_git(git_bin, ["worktree", "remove", "--force", str(destination)], cwd=root)
        except RecoveryError:
            pass
        raise


def render_markdown(result: RecoveryResult, helper_path: Path) -> str:
    """Render verified, host-local pointers and shell-safe recovery commands."""
    import shlex

    manifest = Path(result.manifest_path).resolve()
    destination = manifest.parent.with_name(manifest.parent.name + "-restored")
    verified = result.preservation_status == "complete" and result.complete
    lines = [
        "### Saved work and continuation pointers",
        "",
        f"Captured: {result.created_at or 'unknown'}; run {result.run_id}, attempt {result.attempt}.",
        f"Preservation: `{result.preservation_status}`; cleanup: `{result.cleanup_status}`.",
        "All refs, snapshots, and transcripts below are LOCAL to this host; they are not pushed to GitHub.",
        f"- Host: `{result.host or 'unknown'}`",
        f"- Repository: `{result.repo_root or 'unknown'}`",
        f"- Manifest: `{manifest}`",
        f"- Repair session ID: `{result.session_id or 'unavailable'}`",
        f"- Repair transcript: `{result.transcript_path or manifest.parent / 'transcript.txt'}`",
    ]
    if verified:
        lines.append(f"- Saved HEAD: `{result.head_sha}` via `{result.head_ref}`")
        if result.stash_sha:
            lines.append(f"- Saved edits: `{result.stash_sha}` via `{result.stash_ref}`")
        elif not result.conflict_snapshot:
            lines.append("- No uncommitted edits or nonignored untracked files were present.")
        if result.conflict_snapshot:
            lines.append(f"- Partial merge resolutions, index stages, and merge metadata: `{result.conflict_snapshot}`")
        for ref, oid in result.extra.get("merge_refs", []):
            lines.append(f"- Saved merge parent: `{oid}` via `{ref}`")
        if result.extra.get("unpushed_commits") is False:
            lines.append("- The saved HEAD is already contained in origin/master; there are no unpushed commits.")
        elif result.extra.get("unpushed_commits") is True:
            lines.append("- The saved HEAD includes commits not contained in origin/master.")
        repo = str(result.repo_root)
        lines.extend([
            "",
            "Inspect the saved work:",
            "```sh",
            shlex.join(["git", "-C", repo, "show", "--stat", str(result.head_ref)]),
        ])
        if result.stash_ref and not result.conflict_snapshot:
            lines.append(shlex.join(["git", "-C", repo, "stash", "show", "--include-untracked", "--stat", result.stash_ref]))
        lines.extend([
            "```",
            "",
            "Restore on the named host into a NEW detached worktree. The destination must not exist. "
            "This verifies the package and leaves the cron repair worktree alone:",
            "```sh",
            shlex.join(["python3", str(Path(helper_path).resolve()), str(manifest), str(destination)]),
            "```",
            "",
            "Staged/unstaged edits and nonignored untracked files are restored. "
            "An interrupted merge is reconstructed with its partial resolutions and index stages. "
            "Ignored build artifacts are excluded. Review the continuation note before resuming work.",
        ])
    else:
        lines.extend([
            "",
            "Preservation is incomplete or could not be verified. No destructive cleanup was authorized. "
            "Do not treat the listed paths as verified backups; inspect the manifest/error and original worktree. "
            "New automated repairs remain blocked until recovery succeeds.",
        ])
    if result.error:
        lines.extend(["", f"Recovery error: {result.error}"])
    return "\n".join(lines)


def _cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        restored = restore(args.manifest, args.destination)
    except (RecoveryError, OSError) as exc:
        print(f"recovery failed: {exc}", file=sys.stderr)
        return 1
    print(f"restored recovery package to {restored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
