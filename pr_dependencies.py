"""Supervisor-owned PR dependencies, captured by branch identity and Git ancestry."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable


def has_table(conn, name):
    return conn is not None and bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS automerge_pr_inventory (
        number INTEGER PRIMARY KEY, head_sha TEXT NOT NULL, data_json TEXT NOT NULL,
        head_history_json TEXT NOT NULL DEFAULT '[]', blocked_reason TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS automerge_pr_dependencies (
        number INTEGER NOT NULL, head_sha TEXT NOT NULL,
        prerequisite_number INTEGER NOT NULL, prerequisite_head TEXT NOT NULL,
        PRIMARY KEY(number,head_sha,prerequisite_number,prerequisite_head)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS automerge_commit_ancestry (
        ancestor TEXT NOT NULL, descendant TEXT NOT NULL, present INTEGER NOT NULL,
        PRIMARY KEY(ancestor,descendant)
    )""")


def inventory(conn):
    if not has_table(conn, 'automerge_pr_inventory'):
        return {}
    return {row['number']: (json.loads(row['data_json']), json.loads(row['head_history_json']))
            for row in conn.execute('SELECT * FROM automerge_pr_inventory')}


@dataclass(frozen=True)
class Dependency:
    number: int
    head_sha: str

    def as_json(self):
        return {'number': self.number, 'head_sha': self.head_sha}


@dataclass
class Node:
    number: int
    head: str
    base: str
    branch: str
    repo: str
    base_repo: str
    data: dict
    ready: bool
    priority: bool = False
    immediate: bool = False


class Graph:
    def __init__(self, nodes: dict[int, Node], histories: dict[int, list[str]],
                 master: str, ancestor: Callable[[str, str], bool], *, conn=None,
                 base_branch='master'):
        self.nodes = nodes
        self.histories = histories
        self.master = master
        self._ancestor = ancestor
        self.conn = conn
        self.base_branch = base_branch
        self._comparisons = {}
        self.dependencies: dict[int, tuple[Dependency, ...]] = {}
        self.problems: dict[int, str] = {}
        self.blocked: dict[int, str] = {}
        self.captured = {}
        self._eligibility = {}
        if has_table(conn, 'automerge_pr_dependencies'):
            for row in conn.execute('SELECT * FROM automerge_pr_dependencies'):
                self.captured.setdefault((row['number'], row['head_sha']), []).append(
                    Dependency(row['prerequisite_number'], row['prerequisite_head']))

    def ancestor(self, a, b):
        if a == b:
            return True
        pair = (a, b)
        if pair not in self._comparisons:
            cached = self.conn.execute(
                'SELECT present FROM automerge_commit_ancestry WHERE ancestor=? AND descendant=?',
                pair).fetchone() if has_table(self.conn, 'automerge_commit_ancestry') else None
            value = bool(cached[0]) if cached is not None else self._ancestor(a, b)
            self._comparisons[pair] = value
        return self._comparisons[pair]

    def landed(self, head):
        return self.ancestor(head, self.master)

    def discover(self):
        branches = {}
        for node in self.nodes.values():
            branches.setdefault((node.repo.casefold(), node.branch), []).append(node.number)
        for number, node in self.nodes.items():
            if str(node.data.get('state')).lower() != 'open':
                self.dependencies[number] = tuple(self.captured.get((number, node.head), ()))
                continue
            related: dict[int, str] = {}
            # Retargeting must not erase an accepted relationship. Carry a prior
            # relationship to a new descendant head, but not to unrelated work.
            for (owner, head), deps in self.captured.items():
                if owner == number and self.ancestor(head, node.head):
                    for dep in deps:
                        if dep.number not in related or self.landed(dep.head_sha):
                            related[dep.number] = dep.head_sha
            if node.base != self.base_branch:
                matches = branches.get((node.base_repo.casefold(), node.base), [])
                open_matches = [n for n in matches if str(self.nodes[n].data.get('state')).lower() == 'open']
                matches = open_matches or matches
                if len(matches) != 1 or matches[0] == number:
                    self.problems[number] = f'ambiguous or unresolved prerequisite branch {node.base}'
                else:
                    parent = matches[0]
                    related.setdefault(parent, self.nodes[parent].head)
            # This closes the master-targeting bypass, including old rejected
            # heads retained by a repaired prerequisite through merge commits.
            for parent, prior in self.nodes.items():
                if parent == number:
                    continue
                for head in self.histories.get(parent, [prior.head]):
                    if self.landed(head) or not self.ancestor(head, node.head):
                        continue
                    if head == node.head:
                        self.problems[number] = f'ambiguous shared head with PR #{parent}'
                    related.setdefault(parent, head)
                    break
            deps = []
            for parent, captured_head in sorted(related.items()):
                prior = self.nodes.get(parent)
                if prior is None:
                    self.problems[number] = f'unresolved prerequisite PR #{parent}'
                    deps.append(Dependency(parent, captured_head))
                    continue
                required = captured_head if self.landed(captured_head) else prior.head
                if not self.ancestor(required, node.head):
                    self.problems[number] = f'PR #{parent} changed; its current head is absent from this PR'
                deps.append(Dependency(parent, required))
            self.dependencies[number] = tuple(deps)
        # Explicit relationships can be cyclic even before ancestry is valid.
        visited = set()
        def visit(number, path):
            if number in path:
                cycle = path[path.index(number):] + [number]
                message = 'cyclic prerequisites: ' + ' -> '.join(f'#{n}' for n in cycle)
                for n in cycle:
                    self.problems[n] = message
                return
            if number in visited:
                return
            for dep in self.dependencies.get(number, ()):
                if not self.landed(dep.head_sha):
                    visit(dep.number, path + [number])
            visited.add(number)
        for number in self.nodes:
            visit(number, [])
        return self

    def save(self):
        for number, deps in self.dependencies.items():
            node = self.nodes[number]
            data = {key: value for key, value in node.data.items() if not key.startswith('_mm_')}
            self.conn.execute('INSERT OR REPLACE INTO automerge_pr_inventory '
                              '(number,head_sha,data_json,head_history_json,blocked_reason) VALUES (?,?,?,?,?)',
                              (number, node.head, json.dumps(data),
                               json.dumps(self.histories[number]),
                               (self.blocked.get(number) or self.problems.get(number)) if node.ready else None))
            for dep in deps:
                self.conn.execute('INSERT OR IGNORE INTO automerge_pr_dependencies '
                                  '(number,head_sha,prerequisite_number,prerequisite_head) VALUES (?,?,?,?)',
                                  (number, node.head, dep.number, dep.head_sha))
        for pair, value in self._comparisons.items():
            self.conn.execute('INSERT OR IGNORE INTO automerge_commit_ancestry '
                              '(ancestor,descendant,present) VALUES (?,?,?)',
                              (*pair, int(value)))

    def eligible(self, number, path=()):
        if number in self._eligibility:
            return self._eligibility[number]
        node = self.nodes[number]
        reason = self.problems.get(number)
        if not reason and not node.ready:
            reason = 'prerequisite is draft, rejected, withdrawn, or closed without its head in master'
        if not reason:
            for dep in self.dependencies[number]:
                if self.landed(dep.head_sha):
                    continue
                prior = self.nodes.get(dep.number)
                if prior is None or dep.number in path or not self.eligible(dep.number, path + (number,)):
                    reason = f'blocked by prerequisite PR #{dep.number}'
                    break
        if reason:
            self.blocked[number] = reason
            self._eligibility[number] = False
            return False
        self._eligibility[number] = True
        return True

    def select(self, *, priority=True):
        ready = [n for n in sorted(self.nodes) if self.nodes[n].ready
                 and not self.landed(self.nodes[n].head) and self.eligible(n)]
        seeds = [n for n in ready if self.nodes[n].priority] if priority else []
        seeds = seeds or ready
        ordered = []
        def include(number):
            if number in ordered:
                return
            for dep in self.dependencies[number]:
                if not self.landed(dep.head_sha):
                    include(dep.number)
            ordered.append(number)
        for number in seeds:
            include(number)
        return ordered

    def validate(self, sources):
        """Exact captured heads must close over all unlanded prerequisites."""
        selected = {p.number: p for p in sources}
        blocked = {}
        for pull in sources:
            node = self.nodes.get(pull.number)
            if node is None or node.head != pull.head_sha or not self.eligible(pull.number):
                blocked[pull.number] = self.blocked.get(pull.number, 'source changed or dependency unresolved')
                continue
            deps = {d.number: d for d in self.dependencies[pull.number]}
            for dep in pull.dependencies:
                if not self.landed(dep.head_sha) and (dep.number not in deps or deps[dep.number].head_sha != dep.head_sha):
                    blocked[pull.number] = f'captured prerequisite PR #{dep.number} changed'
                deps.setdefault(dep.number, dep)
            for dep in deps.values():
                if self.landed(dep.head_sha):
                    continue
                included = selected.get(dep.number)
                if included is None or included.head_sha != dep.head_sha:
                    blocked[pull.number] = f'prerequisite PR #{dep.number} is absent from this batch'
        changed = True
        while changed:
            changed = False
            for pull in sources:
                for dep in self.dependencies.get(pull.number, ()):
                    if pull.number not in blocked and dep.number in blocked and not self.landed(dep.head_sha):
                        blocked[pull.number] = f'blocked by prerequisite PR #{dep.number}'
                        changed = True
        return blocked
