#!/usr/bin/env python3
"""Fetch verified batch heads; attempt octopus, then offer manual merges."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mm-db" / "scripts"))
from mm_db import Client, git


def ancestor(head):
    return git("merge-base", "--is-ancestor", head, "HEAD", check=False).returncode == 0


def assemble(state, *, manual=False, rebuild=False):
    if git("branch", "--show-current").stdout.strip() != state["branch"]:
        raise ValueError("not on the recorded integration branch")
    if git("status", "--porcelain").stdout.strip():
        raise ValueError("working tree must be clean; finish any current merge first")
    sources = state["sources"]
    if not sources:
        raise ValueError("cannot merge an empty batch")
    refs = [f"refs/mm/{state['batch_id']}/{p['number']}" for p in sources]
    git("fetch", "origin", *[f"+pull/{p['number']}/head:{ref}" for p, ref in zip(sources, refs)])
    selected = {p['number']: p['head_sha'] for p in sources}
    def in_base(head):
        return git('merge-base', '--is-ancestor', head, state['base_sha'], check=False).returncode == 0
    for p, ref in zip(sources, refs):
        fetched = git("rev-parse", ref).stdout.strip()
        if fetched != p["head_sha"]:
            raise ValueError(f"PR #{p['number']} head changed: expected {p['head_sha']}, fetched {fetched}; remove without rejection")
        for dep in p.get('dependencies', []):
            if git('merge-base', '--is-ancestor', dep['head_sha'], fetched, check=False).returncode:
                raise ValueError(f"PR #{p['number']} does not contain captured prerequisite #{dep['number']}")
            if not in_base(dep['head_sha']) and selected.get(dep['number']) != dep['head_sha']:
                raise ValueError(f"PR #{p['number']} needs prerequisite #{dep['number']} in this batch")
        for excluded in state["excluded"]:
            if in_base(excluded['head_sha']):
                continue
            if not git("merge-base", "--is-ancestor", excluded["head_sha"], fetched, check=False).returncode:
                raise ValueError(f"PR #{p['number']} contains excluded PR #{excluded['number']}; reconsider membership before merging")
    if rebuild:
        git("reset", "--hard", state["base_sha"])
    if not ancestor(state["base_sha"]):
        raise ValueError("captured base is not an ancestor of HEAD; rebuild explicitly")
    for p in state["excluded"]:
        if ancestor(p["head_sha"]) and not in_base(p['head_sha']):
            raise ValueError(f"excluded PR #{p['number']} is still present; rebuild explicitly")
    missing = [p for p in sources if not ancestor(p["head_sha"])]
    trailer = "Automerge-Batch: " + state["batch_id"]
    if missing and not manual:
        before = git("rev-parse", "HEAD").stdout.strip()
        title = "Merge batch: " + " ".join(f"#{p['number']}" for p in missing)
        result = git("merge", "--no-ff", "--no-edit", "-m", title, "-m", trailer,
                     *[p["head_sha"] for p in missing], check=False)
        if result.returncode:
            git("merge", "--abort", check=False)
            git("reset", "--hard", before)
            return {"manual_required": True, "merge_output": result.stdout + result.stderr,
                    "next": "Run this script with --manual; resolve each conflict and git commit --no-edit."}
    elif manual:
        for p in missing:
            result = git("merge", "--no-ff", "--no-edit", "-m", f"Merge PR #{p['number']}: {p['title']}",
                         "-m", trailer, p["head_sha"], check=False)
            if result.returncode:
                return {"manual_required": True, "pr": p["number"],
                        "merge_output": result.stdout + result.stderr,
                        "next": "Resolve and stage conflicts, git commit --no-edit, then run --manual again."}
    if not all(ancestor(p["head_sha"]) for p in sources):
        raise ValueError("merge did not retain every included source head")
    return {"manual_required": False, "head": git("rev-parse", "HEAD").stdout.strip(),
            "tree": git("rev-parse", "HEAD^{tree}").stdout.strip(),
            "sources": [p["number"] for p in sources]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--state-file", type=Path, help="use an explicit saved state instead of mm-db")
    args = parser.parse_args()
    state = json.loads(args.state_file.read_text()) if args.state_file else Client().call("state")
    result = assemble(state, manual=args.manual, rebuild=args.rebuild)
    print(json.dumps(result, indent=2))
    return 2 if result["manual_required"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
