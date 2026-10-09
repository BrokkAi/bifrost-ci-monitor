#!/usr/bin/env python3
"""Client for the batch-scoped MergeMarshall state service."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mm_execution


def git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def context_path():
    return Path(git("rev-parse", "--absolute-git-dir").stdout.strip()) / "mm-connection.json"


def configure(connection):
    if set(connection) != {"url", "batch_id", "token"}:
        raise ValueError("connection needs url, batch_id, and token")
    if not all(isinstance(v, str) and v for v in connection.values()):
        raise ValueError("connection values must be nonempty strings")
    path = context_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(connection, handle)


class Client:
    def __init__(self, connection=None):
        self.connection = connection or json.loads(context_path().read_text())

    def call(self, operation, *, request_timeout=120, **payload):
        c = self.connection
        request = urllib.request.Request(
            c["url"].rstrip("/") + "/batch/" + c["batch_id"] + "/" + operation,
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + c["token"], "Content-Type": "application/json"},
            method="POST",
        )
        deadline = time.monotonic() + request_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('state service operation is still pending; retry the same request')
            try:
                with urllib.request.urlopen(request, timeout=remaining) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    error = json.loads(exc.read())
                    message = error.get("error", str(exc))
                except (ValueError, AttributeError):
                    error = {}
                    message = "state service returned HTTP " + str(exc.code)
                # Only an explicit unfinished cache read is automatically
                # retried. Keep the identical revision/payload and timeout.
                remaining = deadline - time.monotonic()
                if exc.code == 503 and error.get('retryable') is True and remaining > 0:
                    time.sleep(min(1, remaining))
                    continue
                raise RuntimeError(message) from None


def checkpoint(client, revision, *, rebuild=False, withdraw=False):
    state = client.call("state")
    if state["revision"] != revision:
        raise ValueError("stale revision; read mm-db state again")
    if withdraw:
        return client.call("candidate", revision=revision, withdraw=True)
    if git("branch", "--show-current").stdout.strip() != state["branch"]:
        raise ValueError("not on the recorded integration branch")
    if git("status", "--porcelain").stdout.strip():
        raise ValueError("candidate working tree must be clean and committed")
    head = git("rev-parse", "HEAD").stdout.strip()
    for source in state["sources"]:
        if git("merge-base", "--is-ancestor", source["head_sha"], head, check=False).returncode:
            raise ValueError(f"included PR #{source['number']} is absent")
    if git("merge-base", "--is-ancestor", state["base_sha"], head, check=False).returncode:
        raise ValueError("candidate does not contain its recorded base")
    ref = "refs/heads/" + state["branch"]
    args = ["push"]
    if rebuild:
        observed = git("ls-remote", "--heads", "origin", ref).stdout.split()
        args.append("--force-with-lease=" + ref + ":" + (observed[0] if observed else ""))
    git(*args, "origin", "HEAD:" + ref)
    return client.call("candidate", revision=revision, head=head)


def render_report(state):
    evidence = state.get("tests")
    if not evidence:
        raise ValueError("no local test assessment has been recorded")
    lines = ["mergemarshall:local: " + evidence["verdict"],
             "Tests run: " + evidence["tests"],
             "Baseline failures: " + evidence["baseline"],
             "Tested head: " + evidence["head"]]
    for run in evidence.get("executions", []):
        lines.append(f"Execution {run['id']}: {run['command']} at {run['head']}; "
                     f"exit {run['exit_code']}, {run['duration_seconds']:.3f}s; "
                     f"stdout {run['stdout']['path']}; stderr {run['stderr']['path']}")
        if run.get("reuse_reason"):
            lines.append("Reuse: " + run["reuse_reason"])
    publication = state.get("publication")
    if publication:
        if publication["head"] != evidence["head"]:
            raise ValueError("publication and recorded test heads differ")
        lines.append("Integration PR: " + publication["url"])
    for entry in state["excluded"]:
        if entry["kind"] in {"rejected", "ejected"}:
            lines.append(f"mergemarshall:ejected-pr: {entry['number']} {entry['head_sha']}")
    return "\n".join(lines)


def ready(client, revision, *, notes_file=None):
    state = client.call("state")
    notes = notes_file.read_text() if notes_file else ""
    if git("branch", "--show-current").stdout.strip() != state["branch"]:
        raise ValueError("not on the recorded integration branch")
    if git("status", "--porcelain").stdout.strip():
        raise ValueError("working tree must be clean and committed")
    head = git("rev-parse", "HEAD").stdout.strip()
    receipt = state.get("ready")
    if (receipt and receipt["kind"] == "ready" and receipt["head"] == head
            and revision in {receipt["registered_revision"], state["revision"]}):
        if receipt["registered_revision"] is not None and receipt["notes"] != notes:
            raise ValueError("candidate handed to supervisor; readiness notes cannot be changed")
        return state
    if revision != state["revision"]:
        raise ValueError("stale revision; read mm-db state again")
    if not state.get("predecessor"):
        raise ValueError("foreground candidates hand off through mm-autopr")
    evidence = state.get("tests")
    checkpoint = state.get("candidate")
    if (not evidence or evidence["verdict"] != "pass" or evidence["head"] != head
            or not checkpoint or checkpoint["head"] != head):
        raise ValueError("record a passing assessment for this exact checkpointed HEAD")
    return client.call("ready", revision=revision, head=head, notes=notes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    config = sub.add_parser("configure")
    config.add_argument("--connection-file", required=True, type=Path)
    sub.add_parser("state")
    sub.add_parser("report")
    sub.add_parser("findings")
    sub.add_parser("executions")
    execution = sub.add_parser("execution", help="upload a saved execution receipt without rerunning it")
    execution.add_argument("--receipt", required=True, type=Path)
    run = sub.add_parser("run", help="record an explicitly chosen local check")
    run.add_argument("--output", type=Path)
    run.add_argument("--script", type=Path)
    run.add_argument("command", nargs=argparse.REMAINDER)
    finding = sub.add_parser("finding")
    finding.add_argument("--revision", required=True)
    finding.add_argument("--kind", required=True, choices=["baseline", "flaky"])
    finding.add_argument("--head", required=True)
    finding.add_argument("--identity", required=True)
    finding.add_argument("--command", required=True)
    finding.add_argument("--evidence-file", required=True, type=Path)
    candidate = sub.add_parser("candidate")
    candidate.add_argument("--revision", required=True)
    candidate.add_argument("--rebuild", action="store_true")
    candidate.add_argument("--withdraw", action="store_true")
    readiness = sub.add_parser("ready")
    readiness.add_argument("--revision", required=True)
    readiness.add_argument("--notes-file", type=Path)
    inspect_pr = sub.add_parser("pr")
    inspect_pr.add_argument("--pr", required=True, type=int)
    inspect_issue = sub.add_parser("issue")
    inspect_issue.add_argument("--issue", required=True, type=int)
    exclude = sub.add_parser("exclude")
    exclude.add_argument("--revision", required=True)
    exclude.add_argument("--pr", required=True, type=int)
    exclude.add_argument("--head", required=True)
    exclude.add_argument("--kind", required=True, choices=["removed", "rejected"])
    exclude.add_argument("--reason", required=True)
    exclude.add_argument("--evidence-file", type=Path)
    comment = sub.add_parser("comment")
    comment.add_argument("--revision", required=True)
    comment.add_argument("--issue", required=True, type=int)
    comment.add_argument("--body-file", required=True, type=Path)
    tests = sub.add_parser("tests")
    tests.add_argument("--revision", required=True)
    tests.add_argument("--head", required=True)
    tests.add_argument("--verdict", required=True, choices=["pass", "fail"])
    tests.add_argument("--tests", required=True)
    tests.add_argument("--baseline", required=True)
    tests.add_argument("--execution", action="append", default=[])
    tests.add_argument("--reuse-execution", action="append", nargs=2, metavar=("ID", "REASON"), default=[])
    args = vars(parser.parse_args())
    operation = args.pop("operation")
    if operation == "configure":
        configure(json.loads(args["connection_file"].read_text()))
        print("Configured MergeMarshall connection.")
        return
    client = Client()
    if operation == "run":
        command = args["command"]
        if command and command[0] == "--":
            command = command[1:]
        if args["script"] and command:
            raise ValueError("supply either --script or a command after --")
        receipt = mm_execution.run(client, command=command, script=args["script"], output=args["output"])
        print(json.dumps(receipt, indent=2))
        code = receipt["exit_code"]
        return code if code >= 0 else 128 - code
    if operation == "execution":
        print(json.dumps(mm_execution.upload(client, args["receipt"]), indent=2))
        return
    if operation == "candidate":
        print(json.dumps(checkpoint(client, **args), indent=2))
        return
    if operation == "ready":
        print(json.dumps(ready(client, **args), indent=2))
        return
    if operation == "report":
        print(render_report(client.call("state")))
        return
    if operation == "exclude":
        path = args.pop("evidence_file")
        args["evidence"] = path.read_text() if path else ""
        args["number"] = args.pop("pr")
    if operation == "finding":
        args["evidence"] = args.pop("evidence_file").read_text()
    if operation == "comment":
        args["number"] = args.pop("issue")
        args["body"] = args.pop("body_file").read_text()
    if operation == "tests":
        runs, reused = args.pop("execution"), args.pop("reuse_execution")
        if runs or reused:
            args["executions"] = ([{"id": identifier, "reuse_reason": ""} for identifier in runs]
                                  + [{"id": identifier, "reuse_reason": reason} for identifier, reason in reused])
    if operation in {"pr", "issue"}:
        args["number"] = args.pop(operation)
        args["kind"] = "pull" if operation == "pr" else "issue"
        operation = "inspect"
    print(json.dumps(client.call(operation, **args), indent=2))


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
