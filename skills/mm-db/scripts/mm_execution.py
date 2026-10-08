"""Run explicit local checks and retain receipts independently of HTTP delivery."""
import datetime
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import time
import uuid


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def upload(client, path):
    receipt = json.loads(Path(path).read_text())
    if receipt.get("batch_id") != client.connection["batch_id"]:
        raise ValueError("execution receipt belongs to a different batch")
    return client.call("execution", receipt=receipt, request_timeout=10)


def try_upload(client, path):
    if client is None:
        return
    try:
        upload(client, path)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Execution evidence upload pending: {exc}; retry with mm-db execution --receipt {path}",
              file=sys.stderr, flush=True)


def execute(argv, cwd, output, *, client=None, state=None, script=None, stdout=None, stderr=None):
    cwd, output = Path(cwd).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipt_path = output / "receipt.json"
    stdout = Path(stdout).resolve() if stdout else output / "stdout.log"
    stderr = Path(stderr).resolve() if stderr else output / "stderr.log"
    if script is not None:
        contents = Path(script).read_bytes()
        snapshot = output / "check.sh"
        snapshot.write_bytes(contents)
        argv = ["bash", str(snapshot)]
    if not argv or any(not isinstance(arg, str) for arg in argv):
        raise ValueError("supply a command after -- or a Bash --script")
    head = git(cwd, "rev-parse", "HEAD")
    tree = git(cwd, "rev-parse", "HEAD^{tree}")
    dirty = bool(git(cwd, "status", "--porcelain", "--untracked-files=no"))
    environment = {"system": platform.system(), "release": platform.release(),
                   "machine": platform.machine(), "hostname": platform.node(), "uid": str(os.getuid())}
    # Deliberately avoid copying the process environment or authentication data.
    for name in ("RUSTUP_TOOLCHAIN", "RUSTFLAGS", "CARGO_BUILD_JOBS", "CARGO_INCREMENTAL",
                 "CARGO_TARGET_DIR", "NEXTEST_PROFILE"):
        if name in os.environ:
            environment[name] = os.environ[name]
    receipt = {"id": uuid.uuid4().hex, "batch_id": state["batch_id"] if state else None,
               "source_revision": state["source_revision"] if state else None,
               "attempt_generation": state["attempt_generation"] if state else None,
               "head": head, "tree": tree, "argv": argv, "command": shlex.join(argv),
               "cwd": str(cwd), "environment": environment,
               "started_at": utc_now(), "finished_at": None, "duration_seconds": None,
               "exit_code": None, "status": "running", "tracked_source_changed": dirty,
               "stdout": {"path": str(stdout)}, "stderr": {"path": str(stderr)}}
    save(receipt_path, receipt)
    print(f"Execution {receipt['id']}: stdout {stdout}; stderr {stderr}", file=sys.stderr, flush=True)
    try_upload(client, receipt_path)
    started = time.monotonic()
    try:
        with stdout.open("wb") as out, stderr.open("wb") as err:
            receipt["exit_code"] = subprocess.run(argv, cwd=cwd, stdout=out, stderr=err).returncode
        receipt["status"] = "interrupted" if receipt["exit_code"] < 0 else "completed"
    except KeyboardInterrupt:
        receipt.update(status="interrupted", exit_code=-2)
    except OSError as exc:
        receipt.update(status="error", exit_code=127, error=str(exc))
    finally:
        receipt.update(finished_at=utc_now(), duration_seconds=time.monotonic() - started)
        try:
            receipt["head_after"] = git(cwd, "rev-parse", "HEAD")
            receipt["tree_after"] = git(cwd, "rev-parse", "HEAD^{tree}")
            receipt["tracked_source_changed"] |= (
                bool(git(cwd, "status", "--porcelain", "--untracked-files=no"))
                or receipt["head_after"] != head or receipt["tree_after"] != tree)
        except subprocess.CalledProcessError:
            receipt.update(head_after=head, tree_after=tree, tracked_source_changed=True)
        for name, path in (("stdout", stdout), ("stderr", stderr)):
            path.touch(exist_ok=True)
            receipt[name] = {"path": str(path)}
        save(receipt_path, receipt)
        try_upload(client, receipt_path)
    return receipt


def run(client, *, command=None, script=None, output=None):
    current = client.call("state")
    if (current["status"] not in {"running", "launching"}
            or current["phase"] not in {"building", "fixing"} or current.get("ready")
            or (current.get("predecessor") and not current["predecessor"]["current"])):
        raise ValueError("batch is not accepting new checks; wait for the supervisor")
    cwd = Path.cwd()
    common = Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    output = output or common / "mm-executions" / uuid.uuid4().hex
    return execute(command, cwd, output, client=client, state=current, script=script)
