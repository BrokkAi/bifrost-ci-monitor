#!/usr/bin/env python3
"""Batch-scoped state/publication service for MergeMarshall agent skills."""
from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import uuid
from urllib.parse import quote

import automerge


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS automerge_skill_events (
        batch_id TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY (batch_id,event_id)
    )""")
    conn.commit()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def batch_token(key, batch_id):
    return hmac.new(key, batch_id.encode(), hashlib.sha256).hexdigest()


def latest(conn, batch_id, kind):
    row = conn.execute("SELECT payload_json FROM automerge_skill_events WHERE batch_id=? "
                       "AND kind=? ORDER BY rowid DESC LIMIT 1", (batch_id, kind)).fetchone()
    return json.loads(row[0]) if row else None


def record(conn, batch_id, kind, payload):
    # An explicit new assessment may have the same summary as an older run.
    event_id = uuid.uuid4().hex if kind == "tests" else digest([kind, payload])
    conn.execute("INSERT OR IGNORE INTO automerge_skill_events VALUES (?,?,?,?,?)",
                 (batch_id, event_id, kind, json.dumps(payload), automerge.utc_now()))


def state(conn, batch_id):
    row = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if row is None or row["kind"] != "batch":
        raise ValueError("unknown integration batch")
    result = {"batch_id": batch_id, "branch": row["branch"], "base_sha": row["base_sha"],
              "ci_mode": row["ci_mode"], "status": row["status"], "phase": row["phase"],
              "sources": [p.as_json() for p in automerge.row_pulls(row)],
              "excluded": automerge._excluded_source_heads(row)}
    result["source_revision"] = digest([result["base_sha"], result["sources"], result["excluded"]])
    tests = latest(conn, batch_id, "tests")
    result["tests"] = tests if tests and tests["source_revision"] == result["source_revision"] else None
    result["publication"] = latest(conn, batch_id, "publication")
    result["revision"] = digest(result)
    return result


def checked(conn, batch_id, revision):
    current = state(conn, batch_id)
    if current["status"] not in {"running", "launching"} or current["phase"] not in {"building", "fixing"}:
        raise ValueError("batch is no longer accepting agent updates")
    if revision != current["revision"]:
        raise ValueError("stale revision; read mm-db state again")
    return current


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("head must be a full lowercase SHA")
    return value


def text(value, name, limit=30000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty and at most {limit} characters")
    return value.strip()


def gh_api(endpoint, *, method="GET", payload=None):
    args = ["api", f"repos/{automerge.REPO_NAME}/{endpoint}", "--method", method]
    if payload is None:
        return json.loads(automerge.run_gh(args))
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
        json.dump(payload, handle)
        handle.flush()
        return json.loads(automerge.run_gh(args + ["--input", handle.name]))


def rejection(number, head, reason, evidence):
    # Use REST and the supervisor's App token, so markers have its trusted identity.
    gh_api("labels/" + quote(automerge.REJECTED_LABEL, safe=""))
    gh_api(f"issues/{number}/labels", method="POST", payload={"labels": [automerge.REJECTED_LABEL]})
    marker = "automerge-rejected-head: " + head
    if any(re.search(r"(?m)^" + re.escape(marker) + r"\s*$", comment.get("body", "")) and
           (comment.get("user") or {}).get("login") == automerge.TRUSTED_REJECTION_LOGIN
           for comment in automerge.list_pull_comments(number)):
        return
    gh_api(f"issues/{number}/comments", method="POST",
           payload={"body": reason + "\n\n" + evidence + "\n\n" + marker})


def reconcile_publication(current, head, notes):
    branch = current["branch"]
    remote = gh_api("git/ref/heads/" + quote(branch, safe="/"))
    if remote.get("object", {}).get("sha") != head:
        raise ValueError("remote batch head does not match recorded tested head")
    pulls = gh_api("pulls?state=open&head=" + quote(automerge.GH_OWNER + ":" + branch, safe=""))
    if not isinstance(pulls, list) or len(pulls) > 1:
        raise ValueError("expected at most one open integration PR")
    title = "Merge batch: " + " ".join(f"#{p['number']}" for p in current["sources"])
    body = f"MergeMarshall batch `{current['batch_id']}`\n\nBase: `{current['base_sha']}`\n\nIncluded source heads:\n"
    body += "\n".join(f"- #{p['number']} `{p['head_sha']}`: {p['title']}" for p in current["sources"])
    body += "\n\nRemoved source heads:\n" + ("\n".join(
        f"- #{p['number']} `{p['head_sha']}` ({p['kind']})" for p in current["excluded"]) or "None.")
    evidence = current["tests"]
    body += f"\n\nTested head: `{head}`\nTests run: {evidence['tests']}\nBaseline failures: {evidence['baseline']}\n"
    if notes:
        body += "\nConflict resolutions and fixes:\n" + notes
    gh_api("labels/" + quote(automerge.INTEGRATION_LABEL, safe=""))
    if pulls:
        pr = pulls[0]
        if pr["base"]["ref"] != automerge.BASE_BRANCH or pr.get("draft"):
            raise ValueError("existing integration PR must be ready and based on master")
        pr = gh_api(f"pulls/{pr['number']}", method="PATCH", payload={"title": title, "body": body})
    else:
        try:
            pr = gh_api("pulls", method="POST", payload={"title": title, "body": body,
                                                        "base": automerge.BASE_BRANCH, "head": branch})
        except automerge.monitor.CommandError:
            # Recover creation accepted before an ambiguous response. Never create a second PR.
            pulls = gh_api("pulls?state=open&head=" + quote(automerge.GH_OWNER + ":" + branch, safe=""))
            if not isinstance(pulls, list) or len(pulls) != 1:
                raise
            pr = gh_api(f"pulls/{pulls[0]['number']}", method="PATCH", payload={"title": title, "body": body})
    gh_api(f"issues/{pr['number']}/labels", method="POST", payload={"labels": [automerge.INTEGRATION_LABEL]})
    pr = gh_api(f"pulls/{pr['number']}")
    if pr["head"]["sha"] != head or pr["head"]["ref"] != branch or pr["state"] != "open":
        raise ValueError("published integration PR head/state does not match")
    return {"number": pr["number"], "url": pr["html_url"], "head": head}


def dispatch(conn, batch_id, operation, payload):
    if operation == "state":
        return state(conn, batch_id)
    if operation not in {"exclude", "tests", "publish"}:
        raise ValueError("unknown operation")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = checked(conn, batch_id, payload.get("revision"))
        head = sha(payload.get("head"))
        if operation == "exclude":
            number = payload.get("number")
            row = conn.execute("SELECT * FROM automerge_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if type(number) is not int or not any(p.number == number and p.head_sha == head
                                                 for p in automerge._all_batch_pulls(row)):
                raise ValueError("exclusion must name an exact captured source head")
            kind = payload.get("kind")
            if kind not in {"removed", "rejected"}:
                raise ValueError("kind must be removed or rejected")
            reason = text(payload.get("reason"), "reason", 1000)
            evidence = text(payload.get("evidence"), "evidence") if kind == "rejected" else ""
            if re.search(r"(?m)^automerge-rejected-head:", evidence):
                raise ValueError("provide evidence without rejection-marker lines")
            automerge._persist_excluded_source_heads(conn, row, [{"number": number, "head_sha": head, "kind": kind}])
            record(conn, batch_id, "exclusion", {"number": number, "head": head, "kind": kind,
                                                "reason": reason, "evidence": evidence})
            conn.commit()
            if kind == "rejected":
                rejection(number, head, reason, evidence)
        elif operation == "tests":
            verdict = payload.get("verdict")
            if verdict not in {"pass", "fail"}:
                raise ValueError("verdict must be pass or fail")
            tests = text(payload.get("tests"), "tests")
            baseline = text(payload.get("baseline"), "baseline")
            if "\n" in tests or "\n" in baseline or tests.casefold() in {"none", "n/a", "not run"}:
                raise ValueError("tests and baseline must be one-line evidence summaries")
            record(conn, batch_id, "tests", {"head": head, "verdict": verdict, "tests": tests,
                                            "baseline": baseline, "source_revision": current["source_revision"]})
            conn.commit()
        else:
            if not current["sources"] or not current["tests"] or current["tests"]["head"] != head or current["tests"]["verdict"] != "pass":
                raise ValueError("publication requires a passing assessment for this exact head and source set")
            notes = payload.get("notes", "")
            if not isinstance(notes, str) or len(notes) > 30000:
                raise ValueError("notes must be text of at most 30000 characters")
            conn.commit()  # GitHub calls do not hold the SQLite writer lock.
            publication = reconcile_publication(current, head, notes)
            conn.execute("BEGIN IMMEDIATE")
            checked(conn, batch_id, current["revision"])
            record(conn, batch_id, "publication", publication)
            conn.execute("UPDATE automerge_batches SET integration_pr_number=?,integration_pr_url=? WHERE batch_id=?",
                         (publication["number"], publication["url"], batch_id))
            conn.commit()
        return state(conn, batch_id)
    except Exception:
        conn.rollback()
        raise


class Server(ThreadingHTTPServer):
    def __init__(self, address, key, connect):
        super().__init__(address, Handler)
        self.key = key
        self.connect = connect


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, result):
        body = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.respond(200 if self.path == "/health" else 404, {"service": "mm-skills", "version": 1})

    def do_POST(self):
        match = re.fullmatch(r"/batch/([a-z0-9-]{1,64})/([a-z]+)", self.path)
        if not match:
            self.respond(404, {"error": "unknown route"})
            return
        batch_id, operation = match.groups()
        expected = "Bearer " + batch_token(self.server.key, batch_id)
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
            self.respond(403, {"error": "invalid batch token"})
            return
        conn = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("request must be at most 64 KiB")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request must be an object")
            conn = self.server.connect()
            self.respond(200, dispatch(conn, batch_id, operation, payload))
        except (ValueError, KeyError) as exc:
            self.respond(409, {"error": str(exc)})
        except (OSError, sqlite3.Error, automerge.monitor.CommandError, automerge.AutomergeError) as exc:
            self.respond(503, {"error": str(exc)})
        finally:
            if conn is not None:
                conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    key_path = automerge.STATE_DIR / "skill-service.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(32))
    key = key_path.read_bytes()
    if len(key) != 32:
        raise ValueError("invalid skill service key")
    conn = automerge.connect_db()
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    Server((args.listen, args.port), key, automerge.connect_db).serve_forever()


if __name__ == "__main__":
    main()
