"""Launch policy for new merger and issue-fixer sessions."""

CONFIGURATION = "opus-high-dsf-high"
FAILOVER_CONFIGURATION = "flash-dsf-high"
MODEL = "opus"
EFFORT = "high"
TARGET = "bedrock-podman"
SUBAGENT_MODEL = "deepseek-flash"
SUBAGENT_EFFORT = "high"

DELEGATION_GUIDANCE = (
    "Delegate only straightforward work with a clear method to the configured "
    "DeepSeek Flash/high Mjolnir subagents, with bounded tasks and explicit file "
    "ownership. There are no subtle subagents. The primary owns ambiguous "
    "diagnosis, design choices, subtle correctness decisions, integration, Git "
    "operations, test assessment and publication. Subagents may inspect code, "
    "implement a settled change, or run an assigned check; the primary reviews "
    "their results. Builds are expensive: consolidate validation through the "
    "primary and avoid duplicate builds. Assign a subagent a build only when "
    "its result resolves a specific decision, using the existing mbx configuration."
)


def subagent_args():
    # The CI daemon must have no global subtle choice: an omitted per-type
    # override otherwise inherits it. Do not configure a second task type here.
    return ["--subagents", "mjolnir",
            "--subagent-straightforward", SUBAGENT_MODEL,
            "--subagent-straightforward-effort", SUBAGENT_EFFORT]
