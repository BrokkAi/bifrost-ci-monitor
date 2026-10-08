"""Durable local test observations, separate from master CI run evidence."""
import hashlib
import json


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS local_findings (
        id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, session_id TEXT,
        kind TEXT NOT NULL, head_sha TEXT NOT NULL, identity TEXT NOT NULL,
        command TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', diagnosis TEXT, triage_outcome TEXT,
        triage_issue_url TEXT, linked_pr_url TEXT,
        linked_pr_state TEXT NOT NULL DEFAULT 'OPEN'
    )""")


def record(conn, batch_id, session_id, kind, head, identity, command, evidence, now):
    # The same immutable tree/check is one observation, including retries and
    # another batch rediscovering it. Preserve the original evidence/provenance.
    key = [kind, head, identity, command]
    identifier = hashlib.sha256(json.dumps(key).encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO local_findings "
                 "(id,batch_id,session_id,kind,head_sha,identity,command,evidence,created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (identifier, batch_id, session_id, kind, head, identity, command, evidence, now))
    return dict(conn.execute("SELECT * FROM local_findings WHERE id=?", (identifier,)).fetchone())


def observations(conn, issue_url=None):
    query = "SELECT * FROM local_findings WHERE status='open'"
    args = ()
    if issue_url is not None:
        query += " AND triage_issue_url=?"
        args = (issue_url,)
    result = []
    for row in conn.execute(query + " ORDER BY created_at DESC,id", args):
        result.append(dict(row, local_finding_id=row['id'],
                           workflow='Local merge validation', job_name=row['kind'],
                           identity_kind='test', last_seen_sha=row['head_sha'],
                           last_seen_run_id=None, last_seen_run_url=None,
                           last_seen_at=row['created_at'], last_seen_failed_steps_json='[]',
                           diagnosis_source=f"merge session {row['session_id']}",
                           linked_issue_url=None, linked_issue_state='OPEN'))
    return result


def classify(conn, identifier, outcome, diagnosis, issue_url=None):
    conn.execute("UPDATE local_findings SET diagnosis=?,triage_outcome=?,"
                 "triage_issue_url=COALESCE(?,triage_issue_url),"
                 "status=CASE WHEN ?='resolved' THEN 'fixed' ELSE status END "
                 "WHERE id=? AND status='open'",
                 (diagnosis, outcome, issue_url, outcome, identifier))
