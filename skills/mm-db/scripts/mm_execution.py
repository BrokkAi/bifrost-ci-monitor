"""Run explicit local checks and retain receipts independently of HTTP delivery."""
import datetime
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import urllib.request

import mm_attest_capture

ATTEST_VERSION = "0.1.5"


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


def execute(argv, cwd, output, *, client=None, state=None, script=None, stdout=None, stderr=None,
            process_env=None, attest_output=None):
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
    capture_root = None
    executed_argv = argv
    if client is not None and attest_output is None:
        try:
            root = Path(git(cwd, "rev-parse", "--show-toplevel"))
            if (root / "attest.toml").is_file():
                common = Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
                binary = ensure_attest(client, common)
                capture_root = output / "attest-runs"
                executed_argv = [binary, "exec", "--config", str(root / "attest.toml"),
                                 "--output", str(capture_root), "--"] + argv
                process_env = dict(os.environ if process_env is None else process_env,
                    ATTEST_HTTP_URL=client.connection["url"].rstrip("/")
                        + "/batch/" + client.connection["batch_id"] + "/attest",
                    ATTEST_HTTP_TOKEN=client.connection["token"])
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            print("Automatic Attest capture unavailable; native check and command evidence retained.", file=sys.stderr)
            receipt["attest_capture"] = {"recorded": 0, "unavailable": 1, "native_fallbacks": 0, "omitted": 0}
    started = time.monotonic()
    try:
        with stdout.open("wb") as out, stderr.open("wb") as err:
            try:
                receipt["exit_code"] = subprocess.run(executed_argv, cwd=cwd, stdout=out, stderr=err,
                                                     env=process_env).returncode
            except OSError:
                if executed_argv == argv:
                    raise
                # Failure to start the observer has not executed any test work.
                receipt["attest_capture"] = {"recorded": 0, "unavailable": 1,
                                            "native_fallbacks": 0, "omitted": 0}
                capture_root = None
                print("Attest could not start; executing the original command.", file=sys.stderr)
                receipt["exit_code"] = subprocess.run(argv, cwd=cwd, stdout=out, stderr=err,
                                                     env=process_env).returncode
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
        if attest_output is not None:
            try:
                summary = json.loads((Path(attest_output) / "summary.json").read_text())
                if (summary["format_version"] not in (1, 2, 3) or summary["head"] != head
                        or summary["tree"] != tree or summary["exit_code"] != receipt["exit_code"]):
                    raise ValueError("Attest summary does not match this execution")
                receipt["attest"] = {k: summary[k] for k in (
                    "format_version", "run_id", "result", "object", "published", "suite", "mode", "metrics")}
            except (OSError, ValueError, KeyError):
                print("Attest summary unavailable; command evidence retained.", file=sys.stderr)
        if capture_root is not None:
            try:
                mm_attest_capture.collect(receipt, capture_root)
            except (OSError, ValueError):
                print("Automatic Attest summaries unavailable; native command evidence retained.", file=sys.stderr)
        save(receipt_path, receipt)
        try_upload(client, receipt_path)
    return receipt


def run_attest(client, suite, *, filterset=None, config="attest.toml"):
    current = client.call("state")
    if (current["status"] not in {"running", "launching"}
            or current["phase"] not in {"building", "fixing"} or current.get("ready")
            or (current.get("predecessor") and not current["predecessor"]["current"])):
        raise ValueError("batch is not accepting new checks; wait for the supervisor")
    cwd = Path.cwd()
    common = Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    output = common / "mm-executions" / uuid.uuid4().hex
    attest_output = output / "attest"
    binary = ensure_attest(client, common)
    command = [binary, "run", suite, "--config", config, "--shadow", "--output", str(attest_output)]
    if filterset:
        command += ["--filter", filterset]
    environment = dict(os.environ, ATTEST_HTTP_URL=client.connection["url"].rstrip("/")
                       + "/batch/" + current["batch_id"] + "/attest",
                       ATTEST_HTTP_TOKEN=client.connection["token"])
    return execute(command, cwd, output, client=client, state=current,
                   process_env=environment, attest_output=attest_output)


def ensure_attest(client, common):
    configured = os.environ.get("ATTEST_BIN")
    if configured:
        checked = subprocess.run([configured, "version"], capture_output=True, text=True, timeout=10)
        if checked.returncode or checked.stdout.strip() != ATTEST_VERSION:
            raise ValueError("configured Attest binary has an unsupported version")
        return configured
    installed = shutil.which("attest")
    if installed:
        try:
            checked = subprocess.run([installed, "version"], capture_output=True, text=True, timeout=10)
            if checked.returncode == 0 and checked.stdout.strip() == ATTEST_VERSION:
                return installed
        except (OSError, subprocess.TimeoutExpired):
            pass
    target = common / "mm-tools" / ("attest-" + ATTEST_VERSION)
    if target.is_file():
        try:
            checked = subprocess.run([str(target), "version"], capture_output=True, text=True, timeout=10)
            if checked.returncode == 0 and checked.stdout.strip() == ATTEST_VERSION:
                return str(target)
        except (OSError, subprocess.TimeoutExpired):
            pass
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(".attest-" + uuid.uuid4().hex)
    request = urllib.request.Request(client.connection["url"].rstrip("/") + "/batch/"
        + client.connection["batch_id"] + "/attestbinary",
        headers={"Authorization": "Bearer " + client.connection["token"]})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as handle:
            expected_size = int(response.headers.get("Content-Length", "0"))
            total = 0
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > 64 * 1024 * 1024:
                    raise ValueError("Attest binary exceeds download limit")
                handle.write(chunk)
            if total == 0 or (expected_size and total != expected_size):
                raise ValueError("incomplete Attest binary download")
        temporary.chmod(0o700)
        checked = subprocess.run([str(temporary), "version"], capture_output=True, text=True, timeout=10)
        if checked.returncode or checked.stdout.strip() != ATTEST_VERSION:
            raise ValueError("downloaded Attest binary has an unsupported version")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


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
