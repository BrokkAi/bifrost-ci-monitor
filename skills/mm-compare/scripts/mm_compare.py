#!/usr/bin/env python3
"""Run an explicit Bash script at two commits and retain raw result diffs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mm-db" / "scripts"))
from mm_db import Client
from mm_execution import execute


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def compare(repo, a, b, script, output, *, sequential=False, client=None):
    repo = Path(repo).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    script_bytes = Path(script).read_bytes()
    snapshot = output / "check.sh"
    snapshot.write_bytes(script_bytes)
    commits = {name: git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
               for name, ref in [("a", a), ("b", b)]}
    state = client.call("state") if client else None
    if state and (state["phase"] not in {"building", "fixing"} or state.get("ready")
                  or (state.get("predecessor") and not state["predecessor"]["current"])):
        raise ValueError("batch is not accepting new checks; wait for the supervisor")
    worktrees = []
    try:
        for name, sha in commits.items():
            work = output / (name + "-worktree")
            git(repo, "worktree", "add", "--detach", str(work), sha)
            worktrees.append(work)

        def run(name):
            work = output / (name + "-worktree")
            stdout = output / (name + ".stdout")
            stderr = output / (name + ".stderr")
            receipt = execute([], work, output / (name + "-execution"), client=client, state=state,
                              script=snapshot, stdout=stdout, stderr=stderr)
            return {"commit": commits[name], "exit_code": receipt["exit_code"],
                    "tracked_source_changed": receipt["tracked_source_changed"],
                    "stdout": str(stdout), "stderr": str(stderr), "execution_id": receipt["id"],
                    "receipt": str(output / (name + "-execution") / "receipt.json")}

        if sequential:
            results = {name: run(name) for name in commits}
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {name: pool.submit(run, name) for name in commits}
                results = {name: future.result() for name, future in futures.items()}
        for stream in ["stdout", "stderr"]:
            with (output / (stream + ".diff")).open("wb") as handle:
                diff = subprocess.run(["diff", "-u", str(output / ("a." + stream)), str(output / ("b." + stream))], stdout=handle)
                if diff.returncode not in {0, 1}:
                    raise RuntimeError("diff failed")
        result = {"results": results,
                  "stdout_diff": str(output / "stdout.diff"), "stderr_diff": str(output / "stderr.diff")}
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        for work in worktrees:
            git(repo, "worktree", "remove", "--force", str(work))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--a", required=True)
    parser.add_argument("--b", required=True)
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sequential", action="store_true")
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix="mm-compare-")) / "results"
    connection = Path(git(args.repo, "rev-parse", "--absolute-git-dir")) / "mm-connection.json"
    client = Client(json.loads(connection.read_text())) if connection.exists() else None
    print(json.dumps(compare(args.repo, args.a, args.b, args.script, output,
                             sequential=args.sequential, client=client), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
