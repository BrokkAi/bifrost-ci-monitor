"""Supervisor-owned PR dependencies, captured by branch identity and Git ancestry."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
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


def cached_ancestor(conn, ancestor, a, b, *, persist=False):
    """Immutable pairs survive interrupted discovery; exceptions never become facts."""
    if a == b:
        return True
    pair = (a, b)
    cached = conn.execute(
        'SELECT present FROM automerge_commit_ancestry WHERE ancestor=? AND descendant=?',
        pair).fetchone() if has_table(conn, 'automerge_commit_ancestry') else None
    if cached is not None:
        return bool(cached[0])
    value = ancestor(a, b)
    if persist and conn is not None:
        # Graph discovery runs outside membership/publication transactions.
        # Do not commit a caller's unrelated changes to save a comparison.
        if conn.in_transaction:
            raise RuntimeError('ancestry discovery must run outside a state transaction')
        with conn:
            conn.execute('INSERT OR IGNORE INTO automerge_commit_ancestry '
                         '(ancestor,descendant,present) VALUES (?,?,?)', (*pair, int(value)))
    return value


def inventory(conn):
    if not has_table(conn, 'automerge_pr_inventory'):
        return {}
    return {row['number']: (json.loads(row['data_json']), json.loads(row['head_history_json']))
            for row in conn.execute('SELECT * FROM automerge_pr_inventory')}


def save_observation(conn, number, head, data, history, ready):
    data = {key: value for key, value in data.items() if not key.startswith('_mm_')}
    with conn:
        conn.execute('INSERT INTO automerge_pr_inventory '
                     '(number,head_sha,data_json,head_history_json) VALUES (?,?,?,?) '
                     'ON CONFLICT(number) DO UPDATE SET head_sha=excluded.head_sha,'
                     'data_json=excluded.data_json,head_history_json=excluded.head_history_json,'
                     'blocked_reason=CASE WHEN automerge_pr_inventory.head_sha=excluded.head_sha '
                     'AND ? THEN automerge_pr_inventory.blocked_reason ELSE NULL END '
                     'WHERE automerge_pr_inventory.head_sha<>excluded.head_sha '
                     'OR automerge_pr_inventory.data_json<>excluded.data_json '
                     'OR automerge_pr_inventory.head_history_json<>excluded.head_history_json '
                     'OR (NOT ? AND automerge_pr_inventory.blocked_reason IS NOT NULL)',
                     (number, head, json.dumps(data), json.dumps(history), ready, ready))


def save_observations(conn, nodes, histories):
    """Checkpoint the listing even when later ancestry discovery has to yield."""
    for number, node in nodes.items():
        save_observation(conn, number, node.head, node.data, histories[number], node.ready)


@dataclass(frozen=True)
class Dependency:
    number: int
    head_sha: str

    def as_json(self):
        return {'number': self.number, 'head_sha': self.head_sha}


@dataclass(frozen=True)
class ReviewedSupersession:
    """Source-owned replacement for one retired PR exact head."""
    repository: str
    retired_number: int
    retired_head: str
    replacement_number: int
    replacement_head: str


# Records are scheduler source inputs, never ordinary PR metadata.
CANONICAL_REPOSITORY = 'BrokkAi/bifrost-dev'

# Records supply both full exact heads. PR descriptions, manifests and
# selection state are not supersession authority.
# The four retired preparations are contained by the consolidated exact head.
# Fresh state, ancestry and authenticated rejection checks still apply on every
# use; these records grant neither merge approval nor runtime qualification.
REVIEWED_SUPERSESSIONS: tuple[ReviewedSupersession, ...] = (
    ReviewedSupersession(CANONICAL_REPOSITORY, 4776,
        '0b9a463fe6e3b69ebb9a4346d7b8c76753f2406b', 4785,
        'cfd7bc3aff89a7aa3539045d9c964ecb384bf298'),
    ReviewedSupersession(CANONICAL_REPOSITORY, 4781,
        'c2a2e40205852981ebe104b76b857f54c1a12643', 4785,
        'cfd7bc3aff89a7aa3539045d9c964ecb384bf298'),
    ReviewedSupersession(CANONICAL_REPOSITORY, 4788,
        'f776e72b9fe9cc4f6aff72768c9d66a6c6d2d59e', 4785,
        'cfd7bc3aff89a7aa3539045d9c964ecb384bf298'),
    ReviewedSupersession(CANONICAL_REPOSITORY, 4833,
        '4059d967b675fe740921f587888a9852bf8f6784', 4785,
        'cfd7bc3aff89a7aa3539045d9c964ecb384bf298'),
)


def _validated_supersessions(records):
    records = tuple(records)
    by_retired = {}
    by_replacement = {}
    for record in records:
        if not isinstance(record, ReviewedSupersession):
            raise ValueError('supersession registry contains a non-record value')
        if record.repository != CANONICAL_REPOSITORY:
            raise ValueError('supersession record uses a non-canonical repository')
        if (type(record.retired_number) is not int or record.retired_number <= 0
                or type(record.replacement_number) is not int
                or record.replacement_number <= 0):
            raise ValueError('supersession record PR numbers must be positive integers')
        if record.retired_number == record.replacement_number:
            raise ValueError('supersession record cannot replace a PR with itself')
        if (not isinstance(record.retired_head, str)
                or not re.fullmatch(r'[0-9a-f]{40}', record.retired_head)
                or not isinstance(record.replacement_head, str)
                or not re.fullmatch(r'[0-9a-f]{40}', record.replacement_head)):
            raise ValueError('supersession record heads must be lowercase full SHAs')
        if record.retired_number in by_retired:
            raise ValueError('supersession registry has conflicting retired PR records')
        prior_replacement_head = by_replacement.get(record.replacement_number)
        if (prior_replacement_head is not None
                and prior_replacement_head != record.replacement_head):
            raise ValueError('supersession registry has conflicting replacement PR heads')
        by_retired[record.retired_number] = record
        by_replacement[record.replacement_number] = record.replacement_head

    # Alias chains may be useful, but a source record cycle cannot establish a
    # replacement. Reject it before graph discovery rather than hiding it in
    # dependency normalization.
    for start in by_retired:
        path = set()
        number = start
        while number in by_retired:
            if number in path:
                raise ValueError('supersession registry contains an alias cycle')
            path.add(number)
            number = by_retired[number].replacement_number
    return {(record.retired_number, record.retired_head): record
            for record in records}


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
                 base_branch='master', persist_comparisons=False, targets=None, gates=None,
                 available_targets=None, supersessions=None, verify_supersession=None):
        self.nodes = nodes
        self.histories = histories
        self.master = master
        self.targets = targets or {}
        self.gates = gates or {}
        self.available_targets = available_targets
        self._ancestor = ancestor
        self.conn = conn
        self.base_branch = base_branch
        self.persist_comparisons = persist_comparisons
        self.verify_supersession = verify_supersession
        self._comparisons = {}
        self._known_landed = set()
        if has_table(conn, 'automerge_commit_ancestry'):
            self._known_landed.update(row[0] for row in conn.execute(
                'SELECT ancestor FROM automerge_commit_ancestry WHERE descendant=? AND present=1', (master,)))
        self.dependencies: dict[int, tuple[Dependency, ...]] = {}
        self.problems: dict[int, str] = {}
        self.blocked: dict[int, str] = {}
        self.captured = {}
        if supersessions is None:
            supersessions = REVIEWED_SUPERSESSIONS
        self.supersessions = _validated_supersessions(supersessions)
        self._verified_replacement_targets = set()
        self._eligibility = {}
        if has_table(conn, 'automerge_pr_dependencies'):
            for row in conn.execute('SELECT * FROM automerge_pr_dependencies'):
                self.captured.setdefault((row['number'], row['head_sha']), []).append(
                    Dependency(row['prerequisite_number'], row['prerequisite_head']))

    def ancestor(self, a, b):
        if a == b:
            return True
        if not callable(self._ancestor):
            raise RuntimeError('dependency discovery requires an ancestry callback')
        pair = (a, b)
        if pair not in self._comparisons:
            value = cached_ancestor(self.conn, self._ancestor, a, b,
                                    persist=self.persist_comparisons)
            self._comparisons[pair] = value
        return self._comparisons[pair]

    def target(self, number):
        return self.targets.get(number, (self.base_branch, self.master))

    def landed(self, head, number=None):
        if self.ancestor(head, self.target(number)[1]):
            return True
        gate = self.gates.get(number)
        if gate and self.ancestor(head, gate['tip_sha']):
            return any(node.base == gate['campaign_branch']
                       and (node.data.get('merged_at') or node.data.get('merged'))
                       and head in self.histories.get(node.number, [node.head])
                       and self.ancestor(node.head, gate['tip_sha'])
                       for node in self.nodes.values())
        return False

    def _normalize_dependencies(self, owner, dependencies):
        """Replace only verified retired heads and retain their other edges."""
        pending = list(dependencies)
        normalized = []
        seen = set()
        audited_owner_history = set()
        while pending:
            dependency = pending.pop(0)
            identity = (dependency.number, dependency.head_sha)
            if identity in seen:
                continue
            seen.add(identity)
            if self.landed(dependency.head_sha, owner):
                if dependency not in normalized:
                    normalized.append(dependency)
                continue
            if identity in audited_owner_history and identity not in self.supersessions:
                continue
            record = self.supersessions.get(identity)
            if record is None:
                replacement = dependency
                verified = False
            else:
                retired = self.nodes.get(record.retired_number)
                replacement_node = self.nodes.get(record.replacement_number)
                owner_node = self.nodes.get(owner)
                verified = False
                valid = (
                    callable(self.verify_supersession)
                    and owner_node is not None
                    and retired is not None
                    and replacement_node is not None
                    and all(node.repo == CANONICAL_REPOSITORY
                            for node in (owner_node, retired, replacement_node))
                    and all(node.base_repo == CANONICAL_REPOSITORY
                            for node in (owner_node, retired, replacement_node))
                    and str(retired.data.get('state')).lower() == 'closed'
                    and not self._merged(retired.data)
                    and retired.head == record.retired_head
                    and not self._active_rejection_in_history(retired)
                    and str(replacement_node.data.get('state')).lower() == 'open'
                    and replacement_node.ready is True
                    and replacement_node.head == record.replacement_head
                    and not self._active_rejection_in_history(replacement_node)
                )
                if valid:
                    # The primary adapter owns fresh source loading and
                    # rejection/outbox authentication. The callback is still
                    # mandatory for every matching source record.
                    verified = self.verify_supersession(record, retired, replacement_node) is True
                    valid = verified and self.ancestor(record.retired_head, replacement_node.head)
                if not valid:
                    self.problems[owner] = (
                        f'audited supersession for PR #{record.retired_number} '
                        f'cannot verify closed unmerged retired head and ready open '
                        f'replacement PR #{record.replacement_number}'
                    )
                    replacement = dependency
                else:
                    replacement = Dependency(record.replacement_number, record.replacement_head)
                    self._verified_replacement_targets.add((replacement.number, replacement.head_sha))
                    # The retired edge is the expected incorporation edge for
                    # the replacement. Carry forward any separately captured
                    # prerequisites instead of dropping the old closure.
                    for captured in self.captured.get(identity, ()):
                        if (replacement.number == owner
                                and captured.number == owner
                                and captured.head_sha != owner_node.head
                                and self.ancestor(captured.head_sha, record.retired_head)):
                            captured_identity = (captured.number, captured.head_sha)
                            audited_owner_history.add(captured_identity)
                            normalized[:] = [item for item in normalized
                                              if (item.number, item.head_sha) != captured_identity]
                    if replacement.number != owner:
                        pending.append(replacement)
                    pending.extend(self.captured.get(identity, ()))
            # Only an actually verified record may erase its owner's alias.
            # An ordinary self dependency remains a real cycle edge.
            owner_node = self.nodes.get(owner)
            if (verified and owner_node is not None and replacement.number == owner
                    and owner_node.head == replacement.head_sha):
                continue
            if replacement not in normalized:
                normalized.append(replacement)
        return tuple(normalized)

    @staticmethod
    def _merged(data):
        return bool(data.get('merged_at') or data.get('mergedAt')
                    or data.get('merged') is True
                    or data.get('mergeCommit') or data.get('merge_commit'))

    def _active_rejection_in_history(self, node):
        marker = node.data.get('_mm_rejection')
        if not isinstance(marker, dict) or marker.get('active') is not True:
            return False
        rejected_head = marker.get('head_sha')
        if not isinstance(rejected_head, str):
            return False
        history = {node.head.lower()}
        history.update(head.lower() for head in self.histories.get(node.number, ()))
        return rejected_head.lower() in history

    def discover(self, roots=None):
        """Only inspect requested sources and their closure during a batch recheck.

        Hidden inheritance still considers every observed historical head. Pair
        receipts make unchanged heads incremental, independent of mutable master.
        """
        branches = {}
        for node in self.nodes.values():
            branches.setdefault((node.repo.casefold(), node.branch), []).append(node.number)
        pending = list(self.nodes if roots is None else roots)
        examined = set()
        while pending:
            number = pending.pop()
            if number in examined or number not in self.nodes:
                continue
            examined.add(number)
            node = self.nodes[number]
            if str(node.data.get('state')).lower() != 'open':
                self.dependencies[number] = tuple(self.captured.get((number, node.head), ()))
                pending.extend(d.number for d in self.dependencies[number])
                continue
            related: dict[int, str] = {}
            # Retargeting must not erase an accepted relationship. Carry a prior
            # relationship to a new descendant head, but not to unrelated work.
            for (owner, head), deps in self.captured.items():
                if owner == number and self.ancestor(head, node.head):
                    for dep in deps:
                        if dep.number not in related or self.landed(dep.head_sha, number):
                            related[dep.number] = dep.head_sha
            if node.base != self.target(number)[0]:
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
                    if (self._comparisons.get((head, self.target(number)[1])) or head == self.target(number)[1]
                            or (self.target(number)[1] == self.master and head in self._known_landed)):
                        continue
                    # Most pairs are unrelated. Check their immutable relation
                    # before comparing a historical head to a moving master.
                    if not self.ancestor(head, node.head) or self.landed(head, number):
                        continue
                    if head == node.head:
                        self.problems[number] = f'ambiguous shared head with PR #{parent}'
                    related.setdefault(parent, head)
                    break
            captured = [Dependency(parent, captured_head)
                        for parent, captured_head in sorted(related.items())]
            exact_deps = self._normalize_dependencies(number, captured)
            deps = []
            for dependency in exact_deps:
                parent, captured_head = dependency.number, dependency.head_sha
                # A matching source record is either verified or retained as
                # an exact blocked edge. Never promote either form to a mutable
                # current head. Verified replacement targets are also exact.
                if (parent, captured_head) in self.supersessions:
                    deps.append(dependency)
                    continue
                if (parent, captured_head) in self._verified_replacement_targets:
                    if not self.ancestor(captured_head, node.head):
                        self.problems[number] = (
                            f'PR #{parent} replacement head is absent from PR #{number}')
                    deps.append(dependency)
                    continue
                prior = self.nodes.get(parent)
                if prior is None:
                    self.problems[number] = f'unresolved prerequisite PR #{parent}'
                    deps.append(dependency)
                    continue
                required = captured_head if self.landed(captured_head, number) else prior.head
                if not self.ancestor(required, node.head):
                    self.problems[number] = f'PR #{parent} changed; its current head is absent from this PR'
                deps.append(Dependency(parent, required))
            self.dependencies[number] = tuple(dict.fromkeys(deps))
            pending.extend(d.number for d in self.dependencies[number])
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
                if not self.landed(dep.head_sha, number):
                    visit(dep.number, path + [number])
            visited.add(number)
        for number in self.dependencies:
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
            reason = 'prerequisite is draft, rejected, withdrawn, or closed without its head in ' + self.target(number)[0]
        if not reason:
            for dep in self.dependencies[number]:
                if self.landed(dep.head_sha, number):
                    continue
                prior = self.nodes.get(dep.number)
                if (prior is None or dep.number in path or self.target(dep.number)[0] != self.target(number)[0]
                        or not self.eligible(dep.number, path + (number,))):
                    reason = f'blocked by prerequisite PR #{dep.number}'
                    break
        if reason:
            self.blocked[number] = reason
            self._eligibility[number] = False
            return False
        self._eligibility[number] = True
        return True

    def select(self, *, priority=True, target_ref=None, priority_only=False):
        ready = [n for n in sorted(self.dependencies) if self.nodes[n].ready
                 and not self.ancestor(self.nodes[n].head, self.target(n)[1]) and self.eligible(n)
                 and (self.available_targets is None or self.target(n)[0] in self.available_targets)]
        if target_ref is not None:
            ready = [n for n in ready if self.target(n)[0] == target_ref]
        elif self.targets:
            immediate = [n for n in ready if self.nodes[n].immediate and self.target(n)[0] == self.base_branch]
            campaigns = [n for n in ready if self.target(n)[0] != self.base_branch]
            chosen = self.base_branch if immediate or priority_only or not campaigns else self.target(campaigns[0])[0]
            ready = [n for n in ready if self.target(n)[0] == chosen]
        if priority_only and not any(self.nodes[n].priority for n in ready):
            return []
        campaign_lane = bool(ready and self.target(ready[0])[0] != self.base_branch)
        seeds = [n for n in ready if self.nodes[n].priority] if priority and not campaign_lane else []
        seeds = seeds or ready
        ordered = []
        def include(number):
            if number in ordered:
                return
            for dep in self.dependencies[number]:
                if not self.landed(dep.head_sha, number):
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
                if not self.landed(dep.head_sha, pull.number) and (dep.number not in deps or deps[dep.number].head_sha != dep.head_sha):
                    blocked[pull.number] = f'captured prerequisite PR #{dep.number} changed'
                deps.setdefault(dep.number, dep)
            for dep in deps.values():
                if self.landed(dep.head_sha, pull.number):
                    continue
                included = selected.get(dep.number)
                if included is None or included.head_sha != dep.head_sha:
                    blocked[pull.number] = f'prerequisite PR #{dep.number} is absent from this batch'
        changed = True
        while changed:
            changed = False
            for pull in sources:
                for dep in self.dependencies.get(pull.number, ()):
                    if pull.number not in blocked and dep.number in blocked and not self.landed(dep.head_sha, pull.number):
                        blocked[pull.number] = f'blocked by prerequisite PR #{dep.number}'
                        changed = True
        return blocked
