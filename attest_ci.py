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
    conn.execute("""CREATE TABLE IF NOT EXISTS attest_ci_imports (
        run_id INTEGER NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL,
        updated_at TEXT NOT NULL, objects_json TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (run_id, attempt))""")


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
    runs = json.loads(monitor.run_gh(["api", f"repos/{monitor.REPO_NAME}/actions/workflows/ci.yml/runs"
                                    "?status=completed&per_page=20"], timeout=20))["workflow_runs"]
    for run in runs:
        identifier, attempt = run["id"], run["run_attempt"]
        if conn.execute("SELECT 1 FROM attest_ci_imports WHERE run_id=? AND attempt=? "
                        "AND status='done'", (identifier, attempt)).fetchone():
            continue
        if not trusted_run(conn, run, monitor.REPO_NAME):
            continue
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
        conn.execute("INSERT OR REPLACE INTO attest_ci_imports VALUES (?,?,?,?,?)",
                     (identifier, attempt, "done", datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      json.dumps(objects)))
        conn.commit()
        if objects:
            monitor.log(f"Attest imported {len(objects)} CI result bundles from run {identifier}")
        return
