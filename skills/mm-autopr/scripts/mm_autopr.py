#!/usr/bin/env python3
"""Push a tested batch branch and reconcile its integration PR."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mm-db" / "scripts"))
from mm_db import Client, git


def publish(client, *, rebuild=False, notes=""):
    state = client.call("state")
    if state.get("predecessor"):
        raise ValueError("publication is blocked until the predecessor lands and the supervisor promotes this batch")
    branch = state["branch"]
    if git("branch", "--show-current").stdout.strip() != branch:
        raise ValueError("not on the recorded integration branch")
    if git("status", "--porcelain").stdout.strip():
        raise ValueError("working tree must be clean")
    head = git("rev-parse", "HEAD").stdout.strip()
    evidence = state.get("tests")
    if not evidence or evidence["verdict"] != "pass" or evidence["head"] != head:
        raise ValueError("record a passing local assessment for this exact HEAD with mm-db")
    if not state["sources"]:
        raise ValueError("cannot publish an empty batch")
    for p in state["sources"]:
        if git("merge-base", "--is-ancestor", p["head_sha"], head, check=False).returncode:
            raise ValueError(f"included PR #{p['number']} is absent")
    for p in state["excluded"]:
        if not git("merge-base", "--is-ancestor", p["head_sha"], head, check=False).returncode:
            raise ValueError(f"excluded PR #{p['number']} is still present")
    ref = "refs/heads/" + branch
    args = ["push"]
    if rebuild:
        observed = git("ls-remote", "--heads", "origin", ref).stdout.split()
        args.append("--force-with-lease=" + ref + ":" + (observed[0] if observed else ""))
    git(*args, "origin", "HEAD:" + ref)
    return client.call("publish", revision=state["revision"], head=head, notes=notes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--notes-file", type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(Client(), rebuild=args.rebuild,
                             notes=args.notes_file.read_text() if args.notes_file else ""), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
