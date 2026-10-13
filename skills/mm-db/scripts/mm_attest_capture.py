"""Retain compact standalone Attest summaries in supervisor command receipts.

Runner interception and native command parsing belong entirely to Attest.
"""
import json
import re
from pathlib import Path

SUMMARY_FIELDS = ("format_version", "run_id", "result", "object", "published", "suite", "mode",
                  "metrics", "head", "tree", "exit_code")


def collect(receipt, root):
    summaries, unavailable = [], 0
    paths = sorted(Path(root).glob("*/summary.json"))
    for path in paths:
        try:
            if path.stat().st_size > 48 * 1024:
                raise ValueError("oversized summary")
            summary = json.loads(path.read_text())
            if (not isinstance(summary, dict) or summary.get("format_version") not in (1, 2, 3)
                    or summary.get("mode") != "shadow"
                    or not re.fullmatch(r"[0-9a-f]{32}", str(summary.get("run_id", "")))
                    or any(not re.fullmatch(r"[0-9a-f]{40}", str(summary.get(k, ""))) for k in ("head", "tree"))
                    or type(summary.get("exit_code")) is not int
                    or type(summary.get("published")) is not bool
                    or not isinstance(summary.get("metrics"), dict)
                    or any(not isinstance(summary.get(k), str) for k in ("result", "object", "suite"))):
                raise ValueError("unsupported summary")
            summaries.append({key: summary[key] for key in SUMMARY_FIELDS})
        except (OSError, ValueError, KeyError):
            unavailable += 1
    fallbacks = sum(1 for p in Path(root).glob("*/capture-status.json"))
    receipt["attest_capture"] = {"recorded": len(summaries), "unavailable": unavailable,
                                "native_fallbacks": fallbacks, "omitted": 0}
    # Detailed immutable results remain in the store; retain bounded references.
    receipt["attest_runs"] = []
    for summary in summaries:
        receipt["attest_runs"].append(summary)
        if len(receipt["attest_runs"]) > 16 or len(json.dumps(receipt).encode()) > 44 * 1024:
            receipt["attest_runs"].pop()
            receipt["attest_capture"]["omitted"] += 1
    return receipt
