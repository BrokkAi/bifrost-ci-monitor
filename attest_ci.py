"""Import completed CI's Attest bundles as independent, retryable maintenance."""
import base64
import datetime
import gzip
import json
import os
from pathlib import Path
import tempfile

import attest_store


def ensure_schema(conn):
    import monitor
    conn.execute("""CREATE TABLE IF NOT EXISTS attest_ci_imports (
        run_id INTEGER NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL,
        updated_at TEXT NOT NULL, objects_json TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (run_id, attempt))""")
    monitor.ensure_column(conn, 'attest_ci_imports', 'run_json', "TEXT NOT NULL DEFAULT '{}'")
    conn.execute("""CREATE TABLE IF NOT EXISTS attest_ci_scan (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        day TEXT NOT NULL, page INTEGER NOT NULL)""")


def discover(conn):
    """Refresh today's results and checkpoint one older page within evidence retention."""
    import monitor
    today = datetime.datetime.now(datetime.timezone.utc).date()
    oldest = today - datetime.timedelta(days=7)
    cursor = conn.execute('SELECT day,page FROM attest_ci_scan WHERE singleton=1').fetchone()
    day, page = (datetime.date.fromisoformat(cursor[0]), cursor[1]) if cursor else (today, 1)
    if not oldest <= day <= today:
        day, page = today, 1
    queries = [(today, 1)]
    if (day, page) != (today, 1):
        queries.append((day, page))
    for query_day, query_page in queries:
        runs = json.loads(monitor.run_gh([
            'api', f'repos/{monitor.REPO_NAME}/actions/workflows/ci.yml/runs'
            f'?status=completed&created={query_day}&per_page=100&page={query_page}'
        ], timeout=20))['workflow_runs']
        with conn:
            for run in runs:
                if not trusted_run(conn, run, monitor.REPO_NAME):
                    continue
                metadata = {key: run[key] for key in
                            ('id', 'run_attempt', 'event', 'head_branch', 'head_sha')}
                metadata['head_repository'] = {'full_name': monitor.REPO_NAME}
                conn.execute('INSERT OR IGNORE INTO attest_ci_imports '
                             '(run_id,attempt,status,updated_at,objects_json,run_json) '
                             "VALUES (?,?,'pending',?,'[]',?)",
                             (run['id'], run['run_attempt'], monitor.utc_now(), json.dumps(metadata)))
            if (query_day, query_page) == (day, page):
                next_day, next_page = ((day, page + 1) if len(runs) == 100 else
                                       (day - datetime.timedelta(days=1), 1))
                conn.execute('INSERT OR REPLACE INTO attest_ci_scan VALUES (1,?,?)',
                             (next_day.isoformat(), next_page))


def trusted_run(conn, run, repository):
    if (run.get("head_repository") or {}).get("full_name") != repository:
        return False
    if run.get("event") == "push" and run.get("head_branch") == "master":
        return True
    if run.get("event") != "pull_request":
        return False
    sha = run.get("head_sha")
    return bool(conn.execute("SELECT 1 FROM automerge_batches WHERE ci_head_sha=? "
                             "OR CASE WHEN json_valid(ready_json) THEN "
                             "json_extract(ready_json, '$.head') END=? LIMIT 1", (sha, sha)).fetchone())


def tick(conn):
    import monitor
    config = Path(os.environ.get("ATTEST_STORAGE_CONFIG", str(Path.home() / ".config/attest/storage.toml")))
    if not config.is_file():
        return
    ensure_schema(conn)
    conn.commit()
    discover(conn)
    pending = conn.execute("SELECT run_json FROM attest_ci_imports WHERE status='pending' "
                           'ORDER BY updated_at,run_id,attempt LIMIT 1').fetchone()
    if not pending:
        return
    run = json.loads(pending[0])
    identifier, attempt = run['id'], run['run_attempt']
    try:
        artifacts = json.loads(monitor.run_gh(["api", f"repos/{monitor.REPO_NAME}/actions/runs/"
                                               f"{identifier}/artifacts?per_page=100"], timeout=20))["artifacts"]
        selected = [a for a in artifacts if a["name"].startswith("attest-results-")
                    and a["name"].endswith(f"-{attempt}") and not a["expired"]]
        objects = []
        if selected:
            with tempfile.TemporaryDirectory(prefix="attest-ci-") as directory:
                monitor.run_gh(["run", "download", str(identifier), "--repo", monitor.REPO_NAME,
                                "--pattern", f"attest-results-*-{attempt}", "--dir", directory], timeout=45)
                root = Path(directory)
                for path in sorted(root.rglob("result.json")):
                    if path.is_symlink() or root.resolve() not in path.resolve().parents or path.stat().st_size > 32 * 1024 * 1024:
                        raise ValueError("invalid Attest artifact path or size")
                    raw = path.read_bytes()
                    bundle = json.loads(raw)
                    if bundle.get("repository") != "bifrost-dev" or not bundle.get("source", {}).get("clean"):
                        continue
                    if run["event"] == "push" and bundle["source"].get("head") != run["head_sha"]:
                        raise ValueError("Attest artifact does not match the completed push")
                    key = "/".join((bundle["repository"], bundle["source"]["tree"], bundle["suite"],
                                    bundle["run_id"] + ".json.gz"))
                    response = attest_store.request({"operation": "put", "key": key,
                        "data": base64.b64encode(gzip.compress(raw, mtime=0)).decode()})
                    objects.append(response["reference"])
        conn.execute("UPDATE attest_ci_imports SET status='done',updated_at=?,objects_json=? "
                     'WHERE run_id=? AND attempt=?',
                     (monitor.utc_now(), json.dumps(objects), identifier, attempt))
        conn.commit()
        if objects:
            monitor.log(f"Attest imported {len(objects)} CI result bundles from run {identifier}")
    except Exception:
        # Retain the queued attempt, while letting independent imports run next tick.
        with conn:
            conn.execute('UPDATE attest_ci_imports SET updated_at=? WHERE run_id=? AND attempt=?',
                         (monitor.utc_now(), identifier, attempt))
        raise
