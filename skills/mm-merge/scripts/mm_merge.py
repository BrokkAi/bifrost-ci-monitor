#!/usr/bin/env python3
"""Fetch verified batch heads; attempt octopus, then offer manual merges."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mm-db" / "scripts"))
from mm_db import Client, git


def ancestor(head):
    return git("merge-base", "--is-ancestor", head, "HEAD", check=False).returncode == 0


def fetch_base(state):
    if git("cat-file", "-e", state["base_sha"] + "^{commit}", check=False).returncode:
        git("fetch", "origin", state["base_sha"])
    if git("rev-parse", state["base_sha"] + "^{commit}").stdout.strip() != state["base_sha"]:
        raise ValueError("fetched base does not match its recorded SHA")


def restart(state):
    """Preserve pending source work once, then start this generation's new base."""
    if git("branch", "--show-current").stdout.strip() != state["branch"]:
        raise ValueError("not on the recorded integration branch")
    generation = state.get("attempt_generation", 0)
    if generation <= 0:
        raise ValueError("--restart requires a supervisor-created fresh attempt")
    fetch_base(state)
    git_dir = Path(git("rev-parse", "--absolute-git-dir").stdout.strip())
    root = Path(git("rev-parse", "--show-toplevel").stdout.strip())
    archive = git_dir / "mm-recovery" / f"attempt-{generation - 1}"
    archive.mkdir(parents=True, exist_ok=True)
    saved = archive / "snapshot.json"
    done = archive / "reset-done"
    if done.exists():
        if done.read_text() != state["base_sha"]:
            raise ValueError("restart base changed within one attempt; await supervisor recovery")
        return {"archive": str(archive), "already_reset": True}
    if not saved.exists():
        old_head = git("rev-parse", "HEAD").stdout.strip()
        git("update-ref", f"refs/mm-recovery/{state['batch_id']}/attempt-{generation - 1}", old_head)
        paths = set(git("diff", "--name-only", "-z").stdout.split("\0"))
        paths.update(git("diff", "--cached", "--name-only", "-z").stdout.split("\0"))
        untracked = set(git("ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")) - {""}
        for name in paths | untracked:
            if not name:
                continue
            source = root / name
            if source.is_file() or source.is_symlink():
                target = archive / "files" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)
        for args, name in ((["diff", "--binary"], "working.patch"),
                           (["diff", "--cached", "--binary"], "index.patch"),
                           (["status", "--porcelain"], "status.txt")):
            (archive / name).write_text(git(*args).stdout)
        index = git_dir / "index"
        if index.exists():
            shutil.copy2(index, archive / "index")
        note = git_dir / "mergemarshall-progress.md"
        if note.exists():
            shutil.copy2(note, archive / note.name)
        temporary = archive / "snapshot.tmp"
        temporary.write_text(json.dumps({"head": old_head, "untracked": sorted(untracked)}))
        temporary.replace(saved)
    snapshot = json.loads(saved.read_text())
    # Remove only source files already preserved above. Ignored build storage
    # and the Git directory (including connection credentials) are untouched.
    for name in snapshot["untracked"]:
        path = root / name
        if path.is_file() or path.is_symlink():
            path.unlink()
    git("reset", "--hard", state["base_sha"])
    (git_dir / "mergemarshall-progress.md").unlink(missing_ok=True)
    done.write_text(state["base_sha"])
    return {"archive": str(archive), "old_head": snapshot["head"], "already_reset": False}


def promote(state):
    if state.get("predecessor") or not state.get("promotion"):
        raise ValueError("no supervisor-approved promotion is pending")
    if git("branch", "--show-current").stdout.strip() != state["branch"]:
        raise ValueError("not on the recorded integration branch")
    if git("status", "--porcelain").stdout.strip():
        raise ValueError("commit pending changes before promoting")
    fetch_base(state)
    before = git("rev-parse", "HEAD").stdout.strip()
    tree = git("rev-parse", "HEAD^{tree}").stdout.strip()
    evidence = state["promotion"].get("previous_tests")
    tested_tree = git('rev-parse', evidence['head'] + '^{tree}', check=False) if evidence else None
    reusable = bool(evidence and evidence['verdict'] == 'pass' and tested_tree.returncode == 0
                    and tested_tree.stdout.strip() == tree)
    if not ancestor(state["base_sha"]):
        result = git("merge", "--no-ff", "--no-edit", "-m", "Incorporate landed predecessor",
                     "-m", "Automerge-Batch: " + state["batch_id"], state["base_sha"], check=False)
        if result.returncode:
            return {"manual_required": True, "merge_output": result.stdout + result.stderr,
                    "evidence_reusable": False,
                    "next": "Resolve, commit, reassess affected checks and record a fresh assessment."}
    unchanged = git("rev-parse", "HEAD^{tree}").stdout.strip() == tree
    return {"manual_required": False, "head": git("rev-parse", "HEAD").stdout.strip(),
            "previous_head": before, "tree_unchanged": unchanged,
            "evidence_reusable": reusable and unchanged, "previous_tests": evidence,
            "next": "Record a fresh assessment; cite old SHA/settings for reusable evidence, otherwise validate affected checks."}


def assemble(state, *, manual=False, rebuild=False):
    git("config", "rerere.enabled", "true")
    git("config", "rerere.autoupdate", "false")
    fetch_base(state)
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
    parser.add_argument("--restart", action="store_true", help="save old attempt's pending work and reset once")
    parser.add_argument("--promote", action="store_true", help="incorporate the supervisor's landed predecessor")
    parser.add_argument("--state-file", type=Path, help="use an explicit saved state instead of mm-db")
    args = parser.parse_args()
    state = json.loads(args.state_file.read_text()) if args.state_file else Client().call("state")
    if args.restart and (args.rebuild or args.promote):
        raise ValueError("--restart cannot be combined with --rebuild or --promote")
    if args.promote:
        result = promote(state)
    else:
        recovery = restart(state) if args.restart else None
        result = assemble(state, manual=args.manual, rebuild=args.rebuild)
        if recovery:
            result["recovery"] = recovery
    print(json.dumps(result, indent=2))
    return 2 if result["manual_required"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
