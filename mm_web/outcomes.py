"""Dashboard categories for the supervisor's persisted terminal vocabulary.

Operator/priority/predecessor abort reasons are prose; the authoritative stored
terminal_status is aborted. Empty membership is no_sources_remain regardless
of the diagnostic reason (withdrawal, changed/draft sources or exclusions).
"""
from enum import Enum


class Outcome(str, Enum):
    landed = 'landed'
    rejected = 'rejected'
    failed = 'failed'
    aborted = 'aborted'
    withdrawn = 'withdrawn'
    other = 'other'


TERMINAL_OUTCOMES = {
    'merged': Outcome.landed,
    'direct_rejected': Outcome.rejected,
    'direct_rejected_at_head': Outcome.rejected,
    'ci_failed': Outcome.failed,
    'ci_round_limit': Outcome.failed,
    'integration_pr_missing': Outcome.failed,
    'integration_pr_not_mergeable': Outcome.failed,
    'mj_launch_ambiguous_expired': Outcome.failed,
    'aborted': Outcome.aborted,
    'no_sources_remain': Outcome.withdrawn,
    'direct_source_changed': Outcome.withdrawn,
    'direct_fell_back_to_batch': Outcome.withdrawn,
}


def category(row):
    terminal = row.get('terminal_status')
    if terminal in TERMINAL_OUTCOMES:
        return TERMINAL_OUTCOMES[terminal]
    # Failed-launch rows historically carried varying provider reason strings.
    return Outcome.failed if row.get('status') == 'failed' else Outcome.other
