"""Durable observations of local checks; these never authorize a merge."""
import datetime
import json
import math
import re


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS automerge_executions (
        batch_id TEXT NOT NULL, execution_id TEXT NOT NULL, session_id TEXT,
        payload_json TEXT NOT NULL, started_at TEXT NOT NULL,
        PRIMARY KEY (batch_id,execution_id)
    )""")


def validate(value):
    if not isinstance(value, dict) or len(json.dumps(value).encode()) > 48 * 1024:
        raise ValueError("execution receipt must be an object of at most 48 KiB")
    for name, pattern in (("id", r"[0-9a-f]{32}"), ("head", r"[0-9a-f]{40}"),
                          ("tree", r"[0-9a-f]{40}"), ("source_revision", r"[0-9a-f]{64}")):
        if not isinstance(value.get(name), str) or not re.fullmatch(pattern, value[name]):
            raise ValueError(f"invalid execution {name}")
    if type(value.get("attempt_generation")) is not int or value["attempt_generation"] < 0:
        raise ValueError("invalid execution attempt generation")
    for name in ("command", "cwd"):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise ValueError(f"execution {name} must be nonempty text")
    if (not isinstance(value.get("argv"), list) or not value["argv"]
            or any(not isinstance(arg, str) for arg in value["argv"])):
        raise ValueError("execution argv must be a nonempty string list")
    if (not isinstance(value.get("environment"), dict)
            or any(not isinstance(k, str) or not isinstance(v, str)
                   for k, v in value["environment"].items())):
        raise ValueError("execution environment must contain text metadata")
    if type(value.get("tracked_source_changed")) is not bool:
        raise ValueError("execution must record tracked source changes")
    status = value.get("status")
    if status not in {"running", "completed", "interrupted", "error"}:
        raise ValueError("invalid execution status")
    try:
        start = datetime.datetime.fromisoformat(value["started_at"])
        finish = datetime.datetime.fromisoformat(value["finished_at"]) if status != "running" else None
        if start.utcoffset() is None or (finish and (finish.utcoffset() is None or finish < start)):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("execution timestamps must be ordered and include a timezone") from None
    if status == "running":
        if any(value.get(k) is not None for k in ("finished_at", "duration_seconds", "exit_code")):
            raise ValueError("running execution cannot have a result")
    else:
        duration = value.get("duration_seconds")
        if (type(duration) not in {float, int} or not math.isfinite(duration) or duration < 0
                or type(value.get("exit_code")) is not int):
            raise ValueError("finished execution needs duration and exit code")
        for name in ("head_after", "tree_after"):
            if not isinstance(value.get(name), str) or not re.fullmatch(r"[0-9a-f]{40}", value[name]):
                raise ValueError(f"invalid execution {name}")
    for name in ("stdout", "stderr"):
        log = value.get(name)
        if not isinstance(log, dict) or not isinstance(log.get("path"), str) or not log["path"]:
            raise ValueError("execution output must reference retained log paths")
    return value


def record(conn, batch_id, session_id, value):
    value = validate(value)
    prior = conn.execute("SELECT payload_json FROM automerge_executions "
                         "WHERE batch_id=? AND execution_id=?", (batch_id, value["id"])).fetchone()
    if prior:
        old = json.loads(prior[0])
        if old == value:
            return old
        # A late retry of the start cannot overwrite a completed result.
        result_fields = {"status", "finished_at", "duration_seconds", "exit_code", "head_after",
                         "tree_after", "tracked_source_changed", "stdout", "stderr", "error"}
        identity = lambda v: {k: item for k, item in v.items() if k not in result_fields}
        if (identity(old) != identity(value)
                or any(old[name]["path"] != value[name]["path"] for name in ("stdout", "stderr"))):
            raise ValueError("execution ID already names a different check")
        if value["status"] == "running" and old["status"] != "running":
            return old
        if old["status"] != "running":
            raise ValueError("completed execution evidence is immutable")
        conn.execute("UPDATE automerge_executions SET payload_json=? WHERE batch_id=? AND execution_id=?",
                     (json.dumps(value), batch_id, value["id"]))
    else:
        conn.execute("INSERT INTO automerge_executions VALUES (?,?,?,?,?)",
                     (batch_id, value["id"], session_id, json.dumps(value), value["started_at"]))
    return value


def observations(conn, batch_id):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload_json FROM automerge_executions WHERE batch_id=? ORDER BY started_at,execution_id",
        (batch_id,))]


def assessment(conn, current, head, references=None):
    available = {r["id"]: r for r in observations(conn, current["batch_id"])}
    if references is None:
        references = [{"id": r["id"], "reuse_reason": ""} for r in available.values()
                      if r["status"] == "completed" and not r["tracked_source_changed"]
                      and r["head"] in {head, current["base_sha"]}
                      and r["source_revision"] == current["source_revision"]
                      and r["attempt_generation"] == current["attempt_generation"]]
    if not isinstance(references, list):
        raise ValueError("assessment executions must be a list of references")
    result, seen = [], set()
    for ref in references:
        if not isinstance(ref, dict) or not isinstance(ref.get("id"), str) or ref["id"] not in available:
            raise ValueError("assessment references an unknown execution in this batch")
        run = available[ref["id"]]
        reason = ref.get("reuse_reason", "")
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError("execution reuse reason must be text of at most 2000 characters")
        if run["status"] != "completed" or run["tracked_source_changed"]:
            raise ValueError("assessment execution must describe a completed check of a committed tree")
        if (run["head"] not in {head, current["base_sha"]}
                or run["source_revision"] != current["source_revision"]
                or run["attempt_generation"] != current["attempt_generation"]) and not reason.strip():
            raise ValueError("reused execution needs an explicit applicability reason")
        if run["id"] in seen:
            raise ValueError("duplicate assessment execution")
        seen.add(run["id"])
        # Snapshot provenance in the assessment and durable ready receipt.
        result.append(dict(run, reuse_reason=reason.strip()))
    return result


def render(assessment, *, limit=12000):
    runs = assessment.get("executions", [])
    if not runs:
        return ""
    lines = ["Execution evidence:"]
    used = len(lines[0])
    for index, run in enumerate(runs):
        entry = (f"- {run['id']}: {run['command']} at {run['head']}; "
                 f"exit {run['exit_code']}, {run['duration_seconds']:.3f}s; "
                 f"stdout {run['stdout']['path']}; stderr {run['stderr']['path']}")
        if run.get("reuse_reason"):
            entry += "\n  Reuse: " + run["reuse_reason"]
        if used + len(entry) + 1 > limit - 150:
            lines.append(f"{len(runs) - index} additional execution records retained in mm-db executions.")
            break
        lines.append(entry)
        used += len(entry) + 1
    return "\n".join(lines)
