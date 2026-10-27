"""Workstation trigger for the remote CI agent health probe."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import health_check_delivery  # noqa: E402
import monitor  # noqa: E402

AWS_ARGS = ["aws", "--profile", "bifrost-ci", "--region", "us-east-1", "ec2",
            "describe-instances", "--filters",
            "Name=tag:Name,Values=bifrost-ci-agents",
            "Name=instance-state-name,Values=running", "--query",
            "Reservations[].Instances[].{ID:InstanceId,IP:PublicIpAddress}", "--output", "json"]
REMOTE = ("cd /home/ubuntu/Projects/bifrost-ci-monitor && "
          "BIFROST_GH_BIN=/home/ubuntu/.local/bin/gh timeout 90 python3 health_probe.py")
LOG_LIMIT = 5 * 1024 * 1024
TEXT_LIMIT = 60 * 1024


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fingerprint(kind: str, entity: str, cause: str) -> str:
    return f"{kind}:{entity}:{hashlib.sha256(cause.encode()).hexdigest()[:12]}"


def synthetic(kind: str, entity: str, cause: str, summary: str | None = None) -> dict:
    return {"kind": kind, "entity": entity, "cause": cause,
            "fingerprint": fingerprint(kind, entity, cause), "persist_seconds": 0,
            "summary": (summary or cause)[:200]}


def short_error(value: object) -> str:
    # Diagnostics from subprocesses are untrusted; retain only a bounded, single line.
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    text = re.sub(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[redacted]", text)
    text = re.sub(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "[redacted]", text)
    text = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[redacted]", text)
    text = re.sub(r"(?i)\b(token|secret|password|authorization)\s*[:=]\s*[^\s,;]+",
                  r"\1=[redacted]", text)
    text = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", text)
    return text[:200]


def discover_target(run=subprocess.run) -> tuple[str | None, dict | None]:
    try:
        result = run(AWS_ARGS, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, synthetic("probe_ec2_discovery", "aws", "aws cli failed")
    if result.returncode:
        return None, synthetic("probe_ec2_discovery", "aws", "aws cli failed")
    try:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list):
            raise ValueError
    except (ValueError, TypeError):
        return None, synthetic("probe_ec2_discovery", "aws", "aws cli failed")
    if len(rows) != 1:
        return None, synthetic("probe_ec2_discovery", "aws", f"{len(rows)} running instances")
    ip = rows[0].get("IP") if isinstance(rows[0], dict) else None
    if not isinstance(ip, str) or not ip.strip():
        return None, synthetic("probe_ec2_discovery", "aws", "1 running instance has no public IP")
    return "ubuntu@" + ip, None


def run_probe(target: str, run=subprocess.run) -> tuple[dict | None, list[dict]]:
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, REMOTE]
    try:
        result = run(command, capture_output=True, text=True, timeout=110, check=False)
    except subprocess.TimeoutExpired:
        return None, [synthetic("probe_ssh", "ssh", "timeout")]
    except OSError:
        return None, [synthetic("probe_ssh", "ssh", "connect failed")]
    if result.returncode == 255:
        return None, [synthetic("probe_ssh", "ssh", "exit 255")]
    if result.returncode:
        return None, [synthetic("probe_failed", "probe", f"exit {result.returncode}")]
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, TypeError):
        return None, [synthetic("probe_failed", "probe", "invalid json")]
    if payload.get("format") != 1:
        return None, [synthetic("probe_failed", "probe", "unsupported format")]
    findings = payload.get("findings")
    errors = payload.get("check_errors")
    if not isinstance(findings, list) or not isinstance(errors, list):
        return None, [synthetic("probe_failed", "probe", "invalid json")]
    valid = []
    for item in findings:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in
                                                  ("kind", "entity", "cause", "summary", "fingerprint")):
            continue
        # The probe normalizes causes (retry counters, timestamps) before hashing,
        # so its fingerprint is the episode identity; never rehash the raw cause.
        finding = dict(item)
        try:
            finding["persist_seconds"] = max(0, int(item.get("persist_seconds", 0)))
        except (ValueError, TypeError):
            finding["persist_seconds"] = 0
        valid.append(finding)
    for item in errors:
        if isinstance(item, dict) and isinstance(item.get("check"), str):
            valid.append(synthetic("probe_check_failed", item["check"],
                                   short_error(item.get("error", "check failed"))))
    return payload, valid


def format_message(mode: str, findings: list[dict], state_dir: Path, prompt: str = "") -> str:
    if mode == "shadow":
        intro = ("Health probe (SHADOW MODE — no checkup was started).\n\n"
                 "It would have triggered a CI-agent health checkup for these new persistent findings:")
        ending = ("Assess, read-only, whether each finding reflects a real problem in the CI agents "
                  "(merger, triage, fixers, campaign, host). The scheduled checkup still owns remediation. "
                  f"Append one JSON line per finding to {state_dir / 'verdicts.jsonl'} with fingerprint, "
                  "verdict (true_positive|false_positive|unclear) and a one-sentence reason, then report "
                  "briefly in this session, telling Jonathan when a real problem is not already being handled. "
                  "A reasonable rate of false positives is acceptable when it catches more real problems; "
                  "suggest threshold changes only with evidence.")
    else:
        intro = ("Triggered CI-agent health check. The health probe found these new persistent findings; "
                 "start with them, then complete the full check below.")
        ending = prompt
    shown = findings[:40]
    lines = [f"- [{f['kind']}] {f['entity']} — {f['summary']} (first seen {f['first_seen']}, fingerprint {f['fingerprint']})"
             for f in shown]
    if len(findings) > 40:
        lines.append(f"... and {len(findings) - 40} more")
    body = intro + "\n" + "\n".join(lines) + "\n\n" + ending
    if len(body.encode()) > TEXT_LIMIT:
        # Summaries are untrusted. Trim them deterministically while preserving all identities.
        for f, line_index in reversed(list(zip(shown, range(len(shown))))):
            over = len(body.encode()) - TEXT_LIMIT
            summary = f["summary"]
            f["summary"] = summary[:max(0, len(summary) - over - 16)] + "…"
            lines[line_index] = f"- [{f['kind']}] {f['entity']} — {f['summary']} (first seen {f['first_seen']}, fingerprint {f['fingerprint']})"
            body = intro + "\n" + "\n".join(lines) + "\n\n" + ending
            if len(body.encode()) <= TEXT_LIMIT:
                break
    if len(body.encode()) > TEXT_LIMIT:
        body = body.encode()[:TEXT_LIMIT].decode("utf-8", "ignore")
    return body


def atomic_json(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def append_log(path: Path, row: dict) -> None:
    if path.exists() and path.stat().st_size > LOG_LIMIT:
        backup = path.with_name(path.name + ".1")
        os.replace(path, backup)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _request_id(entries: list[dict]) -> str:
    identity = "\n".join(sorted(f"{x['fingerprint']}@{x['first_seen']}@{x['generation']}" for x in entries))
    return "ci-health-probe-" + hashlib.sha256(identity.encode()).hexdigest()[:40]


def execute(args, *, sender=None, run=subprocess.run, now=None) -> int:
    state_dir = Path(args.state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    lock_path = state_dir / "lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("health-trigger: locked")
            return 0
        state_path = state_dir / "state.json"
        state = {"findings": {}, "pending_trigger": None}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or not isinstance(state.get("findings"), dict):
                    raise ValueError("invalid state")
            except (OSError, ValueError, TypeError):
                damaged = state_path.with_name(f"state.json.corrupt-{int((now or datetime.now(timezone.utc)).timestamp())}")
                os.replace(state_path, damaged)
                print("health-trigger: corrupt state quarantined")
                state = {"findings": {}, "pending_trigger": None}
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        target, discovery_error = (args.ssh_target, None) if args.ssh_target else discover_target(run)
        payload, findings = (None, [discovery_error]) if discovery_error else run_probe(target, run)
        present = {f["fingerprint"]: f for f in findings}
        previous = state.get("findings", {})
        tracked = {}
        due = []
        for fp, finding in present.items():
            old = previous.get(fp)
            entry = {"first_seen": old["first_seen"] if old else iso(now),
                     "last_seen": iso(now), "consecutive": old.get("consecutive", 0) + 1 if old else 1,
                     "triggered_at": old.get("triggered_at") if old else None,
                     "generation": int(old.get("generation", 0)) if old else 0,
                     "finding": finding}
            triggered = parse_time(entry["triggered_at"]) if entry["triggered_at"] else None
            if (entry["consecutive"] >= 2 and
                    (now - parse_time(entry["first_seen"])).total_seconds() >= finding["persist_seconds"] and
                    (triggered is None or (now - triggered).total_seconds() >= 12 * 3600)):
                if triggered is not None:
                    entry["generation"] += 1
                due.append({**finding, **entry})
            tracked[fp] = entry
        pending = state.get("pending_trigger")
        sent = []
        deliver = sender or health_check_delivery.send_check
        prompt = getattr(args, "_prompt", "")

        def send(item, *, keep_existing_on_failure=False):
            try:
                deliver(args.session, item["text"], request_id=item["request_id"])
                for fp in item["fingerprints"]:
                    if fp in tracked:
                        tracked[fp]["triggered_at"] = iso(now)
                        if fp in {d["fingerprint"] for d in due}:
                            tracked[fp]["generation"] = next(d["generation"] for d in due if d["fingerprint"] == fp)
                if (state.get("pending_trigger") or {}).get("request_id") == item["request_id"]:
                    state["pending_trigger"] = None
                sent.append({"request_id": item["request_id"], "accepted": True, "error": ""})
                return True
            except Exception as exc:
                err = type(exc).__name__[:200]
                if not keep_existing_on_failure:
                    state["pending_trigger"] = item
                sent.append({"request_id": item["request_id"], "accepted": False, "error": err})
                return False

        if pending:
            accepted_pending = send(pending)
            fresh = [d for d in due if d["fingerprint"] not in pending["fingerprints"]]
            if fresh:
                msg = format_message(args.mode, fresh, state_dir, prompt)
                item = {"request_id": _request_id(fresh), "text": msg,
                        "fingerprints": [d["fingerprint"] for d in fresh], "created_at": iso(now)}
                send(item, keep_existing_on_failure=not accepted_pending)
        elif due:
            msg = format_message(args.mode, due, state_dir, prompt)
            send({"request_id": _request_id(due), "text": msg,
                  "fingerprints": [d["fingerprint"] for d in due], "created_at": iso(now)})
        state["findings"] = tracked
        if state.get("pending_trigger") is None:
            state["pending_trigger"] = None
        atomic_json(state_path, state)
        append_log(state_dir / "probe-log.jsonl", {"at": iso(now), "mode": args.mode,
                    "probe_ok": payload is not None, "fingerprints": sorted(present),
                    "due": sorted(d["fingerprint"] for d in due), "sent": sent})
        print(f"health-trigger: findings={len(present)} due={len(due)} sent={len(sent)}")
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("shadow", "live"), default="shadow")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--prompt-file")
    parser.add_argument("--ssh-target")
    args = parser.parse_args(argv)
    if args.mode == "live" and not args.prompt_file:
        parser.error("--prompt-file is required in live mode")
    prompt = ""
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot read prompt file: {exc}")
    if len(prompt.encode()) > TEXT_LIMIT:
        parser.error("prompt file exceeds the message limit")
    args._prompt = prompt
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
