"""Stabilize campaigns: the shared state contract for merger, fixer and triage.

A campaign cuts a branch from master and runs the push, hourly and nightly
suites on it in order. Fixes land in that branch until every stage up to the
current one is green at one tip; each passed stage opens a gate PR from an
immutable snapshot of that tip to master. stabilize.py owns every write here
except `classify`, which triage calls under its publication lock.
"""
import dataclasses

STAGES = ('push', 'hourly', 'nightly')
STAGE_WORKFLOWS = {'push': 'CI', 'hourly': 'Hourly CI', 'nightly': 'Nightly CI'}
LIVE_STATUSES = ('starting', 'running', 'paused')
LABEL = 'mergemarshall:stabilize'
# Issue states. `open` needs a ready PR against the campaign branch; `pr_ready`
# has one; `landed` waits for the next stage run at the new tip. `escalated`,
# `closed` (closed on GitHub without a campaign landing) and `dropped` (absent
# from a later branch run) no longer hold the round gate or target the branch.
ISSUE_STATES = ('open', 'pr_ready', 'landed', 'escalated', 'closed', 'dropped')


@dataclasses.dataclass(frozen=True)
class Campaign:
    id: str
    branch: str
    anchor_sha: str
    tip_sha: str | None
    stage: str
    status: str
    round: int
    thread_ts: str | None

    @property
    def live(self):
        return self.status in LIVE_STATUSES


def ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS stabilize_campaigns (
        id TEXT PRIMARY KEY, branch TEXT NOT NULL UNIQUE, requested_ref TEXT,
        requested_by TEXT, idempotency_key TEXT UNIQUE,
        anchor_sha TEXT NOT NULL, tip_sha TEXT,
        stage TEXT NOT NULL DEFAULT 'push', status TEXT NOT NULL DEFAULT 'starting',
        pause_reason TEXT, round INTEGER NOT NULL DEFAULT 0, thread_ts TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT);
    CREATE TABLE IF NOT EXISTS stabilize_issues (
        campaign_id TEXT NOT NULL, issue_number INTEGER NOT NULL,
        stage TEXT NOT NULL, source TEXT NOT NULL, round INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'open', pr_number INTEGER, pr_head_sha TEXT,
        added_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (campaign_id, issue_number));
    CREATE TABLE IF NOT EXISTS stabilize_gate_merges (
        campaign_id TEXT NOT NULL, stage TEXT NOT NULL, tip_sha TEXT NOT NULL,
        gate_branch TEXT NOT NULL UNIQUE, pr_number INTEGER,
        state TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (campaign_id, stage));
    CREATE TABLE IF NOT EXISTS stabilize_failures (
        campaign_id TEXT NOT NULL, stage TEXT NOT NULL,
        workflow TEXT NOT NULL, job_name TEXT NOT NULL,
        identity_kind TEXT NOT NULL, identity TEXT NOT NULL,
        first_seen_sha TEXT NOT NULL, first_seen_run_id INTEGER NOT NULL,
        first_seen_run_url TEXT NOT NULL, first_seen_at TEXT NOT NULL,
        last_seen_sha TEXT NOT NULL, last_seen_run_id INTEGER NOT NULL,
        last_seen_run_url TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        last_seen_failed_steps_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'open', fixed_at TEXT, fixed_by_sha TEXT,
        diagnosis TEXT, diagnosis_source TEXT, triage_outcome TEXT,
        linked_issue_url TEXT, linked_issue_state TEXT NOT NULL DEFAULT 'OPEN',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (campaign_id, workflow, job_name, identity_kind, identity));
    """)


def _campaign(row):
    return Campaign(row['id'], row['branch'], row['anchor_sha'], row['tip_sha'],
                    row['stage'], row['status'], row['round'], row['thread_ts'])


def live_campaigns(conn):
    marks = ','.join('?' * len(LIVE_STATUSES))
    return [_campaign(row) for row in conn.execute(
        f"SELECT * FROM stabilize_campaigns WHERE status IN ({marks}) ORDER BY created_at,id",
        LIVE_STATUSES)]


def campaign_for_branch(conn, branch):
    """Any campaign, live or finished, whose working branch is `branch`."""
    row = conn.execute("SELECT * FROM stabilize_campaigns WHERE branch=?", (branch,)).fetchone()
    return _campaign(row) if row else None


def gate_for_branch(conn, head_branch):
    """The gate merge whose snapshot branch is `head_branch`, with its campaign."""
    row = conn.execute("SELECT * FROM stabilize_gate_merges WHERE gate_branch=?",
                       (head_branch,)).fetchone()
    if row is None:
        return None
    campaign = conn.execute("SELECT * FROM stabilize_campaigns WHERE id=?",
                            (row['campaign_id'],)).fetchone()
    return dict(row), _campaign(campaign)


def campaign_issue(conn, issue_number, issue_url=None):
    """(live campaign, issue state) for an issue, or None.

    Membership comes from stabilize_issues. Triage links new branch
    observations before stabilize.py records membership, so a linked open
    observation also counts, as state `open`.
    """
    for campaign in live_campaigns(conn):
        row = conn.execute("SELECT state FROM stabilize_issues WHERE campaign_id=? "
                           "AND issue_number=?", (campaign.id, issue_number)).fetchone()
        if row:
            return campaign, row['state']
        if issue_url and conn.execute(
                "SELECT 1 FROM stabilize_failures WHERE campaign_id=? AND status='open' "
                "AND linked_issue_url=?", (campaign.id, issue_url)).fetchone():
            return campaign, 'open'
    return None


def issue_priorities(conn):
    """Sort keys for open campaign issues: earlier stage first, then issue number.

    Like `campaign_issue`, an open branch observation that triage linked before
    stabilize.py recorded membership counts as an open issue at its stage.
    """
    result = {}
    for campaign in live_campaigns(conn):
        members = set()
        for row in conn.execute("SELECT issue_number,stage,state FROM stabilize_issues "
                                "WHERE campaign_id=?", (campaign.id,)):
            members.add(row['issue_number'])
            if row['state'] == 'open':
                result.setdefault(row['issue_number'],
                                  (STAGES.index(row['stage']), row['issue_number']))
        for row in conn.execute("SELECT stage,linked_issue_url FROM stabilize_failures "
                                "WHERE campaign_id=? AND status='open' "
                                "AND linked_issue_url IS NOT NULL", (campaign.id,)):
            tail = row['linked_issue_url'].rstrip('/').rsplit('/', 1)[-1]
            if not tail.isdigit() or int(tail) in members:
                continue
            key = (STAGES.index(row['stage']), int(tail))
            result[int(tail)] = min(result.get(int(tail), key), key)
    return result


def merge_gate_open(conn, campaign_id):
    """Whether the merger may select PRs into this campaign's branch now.

    The round gate opens when the campaign is running, no issue at or before
    the current stage still needs a PR, and at least one issue has a ready PR.
    Ready PRs for later-stage issues are included without holding the gate.
    The merger checks this at selection/expansion only; an in-flight batch is
    not aborted when the gate closes.
    """
    row = conn.execute("SELECT * FROM stabilize_campaigns WHERE id=?", (campaign_id,)).fetchone()
    if row is None or row['status'] != 'running' or row['stage'] not in STAGES:
        return False
    current = STAGES[:STAGES.index(row['stage']) + 1]
    states = {(r['stage'], r['state']) for r in conn.execute(
        "SELECT stage,state FROM stabilize_issues WHERE campaign_id=?", (campaign_id,))}
    if any(stage in current and state == 'open' for stage, state in states):
        return False
    return any(state == 'pr_ready' for _, state in states)


def observations(conn, issue_url=None):
    """Open branch observations, shaped like ledger rows for triage and the fixer."""
    query = ("SELECT f.*, c.branch FROM stabilize_failures f JOIN stabilize_campaigns c "
             "ON c.id=f.campaign_id WHERE f.status='open'")
    args = ()
    if issue_url is not None:
        query += " AND f.linked_issue_url=?"
        args = (issue_url,)
    return [dict(row, stabilize_campaign_id=row['campaign_id'],
                 stabilize_branch=row['branch'])
            for row in conn.execute(query + " ORDER BY f.last_seen_at DESC,f.workflow,"
                                    "f.job_name,f.identity", args)]


def classify(conn, campaign_id, key, outcome, diagnosis, issue_url=None):
    """Record triage's accepted outcome for one branch observation.

    `key` is (workflow, job_name, identity_kind, identity). Only the unchanged
    open observation is updated; stabilize.py retires rows from later runs.
    """
    conn.execute("UPDATE stabilize_failures SET diagnosis=?,triage_outcome=?,"
                 "linked_issue_url=COALESCE(?,linked_issue_url) WHERE campaign_id=? "
                 "AND workflow=? AND job_name=? AND identity_kind=? AND identity=? "
                 "AND status='open'", (diagnosis, outcome, issue_url, campaign_id, *key))
