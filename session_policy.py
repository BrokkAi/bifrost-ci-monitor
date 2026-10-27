"""Launch policy for new merger and issue-fixer sessions."""

CONFIGURATION = "opus-high-dsf-high"
FAILOVER_CONFIGURATION = "flash-dsf-high"
MERGER_CONFIGURATION = "opus-high-dsf-high-sonnet-high"
MERGER_FAILOVER_CONFIGURATION = "flash-dsf-high-sonnet-high"
MODEL = "opus"
EFFORT = "high"
TARGET = "bedrock-podman"
SUBAGENT_MODEL = "deepseek-flash"
SUBAGENT_EFFORT = "high"
SUBTLE_MODEL = "us.anthropic.claude-sonnet-5-5"
SUBTLE_EFFORT = "high"

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

MERGER_DELEGATION_GUIDANCE = (
    "Delegate straightforward work with a clear method to DeepSeek Flash/high "
    "Mjolnir subagents, and subtle work requiring judgment, ambiguous diagnosis "
    "or correctness review to Sonnet/high Mjolnir subagents. Select the matching "
    "task_type (straightforward or subtle), with bounded tasks and explicit file "
    "ownership. The primary reviews their results and owns integration, Git "
    "operations, test assessment and publication. Builds are expensive: "
    "consolidate validation through the primary and avoid duplicate builds. "
    "Assign a subagent a build only when its result resolves a specific "
    "decision, using the existing mbx configuration."
)


def subagent_args():
    # Keep the global subtle choice unset so fixers retain one task type.
    return ["--subagents", "mjolnir",
            "--subagent-straightforward", SUBAGENT_MODEL,
            "--subagent-straightforward-effort", SUBAGENT_EFFORT]


def merger_subagent_args():
    return subagent_args() + ["--subagent-subtle", SUBTLE_MODEL,
                             "--subagent-subtle-effort", SUBTLE_EFFORT]
