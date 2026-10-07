#!/usr/bin/env python3
"""Run an explicit Bash script at two commits and retain raw result diffs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def compare(repo, a, b, script, output, *, sequential=False):
    repo = Path(repo).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    script_bytes = Path(script).read_bytes()
    snapshot = output / "check.sh"
    snapshot.write_bytes(script_bytes)
    commits = {name: git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
               for name, ref in [("a", a), ("b", b)]}
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
            with stdout.open("wb") as out, stderr.open("wb") as err:
                result = subprocess.run(["bash", str(snapshot)], cwd=work, stdout=out, stderr=err)
            return {"commit": commits[name], "exit_code": result.returncode,
                    "tracked_source_changed": bool(git(repo, "-C", str(work), "status", "--porcelain", "--untracked-files=no"))
                    or git(repo, "-C", str(work), "rev-parse", "HEAD") != commits[name],
                    "stdout": str(stdout), "stderr": str(stderr)}

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
        result = {"script_sha256": hashlib.sha256(script_bytes).hexdigest(), "results": results,
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
    print(json.dumps(compare(args.repo, args.a, args.b, args.script, output,
                             sequential=args.sequential), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
