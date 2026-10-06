#!/usr/bin/env bash
set -euo pipefail

readonly REPOSITORY="BrokkAi/bifrost-dev"
readonly CREATE_RULESET_NAME='Protect `master`'
dry_run=0

if [[ "${1:-}" == "--dry-run" ]]; then
    dry_run=1
    shift
fi
if [[ $# -ne 0 ]]; then
    echo "usage: $0 [--dry-run]" >&2
    exit 2
fi

if ! command -v gh >/dev/null 2>&1; then
    echo "gh is required; authenticate as a repository administrator first." >&2
    exit 1
fi

# Listing is read-only. The list endpoint returns summaries only, so fetch
# each ruleset's conditions and rules by id before resolving the master
# ruleset; dry-run then reports the exact object that would be updated.
listing="$(gh api "repos/${REPOSITORY}/rulesets?per_page=100" --paginate --slurp)"
ids="$(python3 - "$listing" <<'PY'
import json
import sys

pages = json.loads(sys.argv[1])
if not isinstance(pages, list):
    raise SystemExit("rulesets API returned an invalid listing; refusing to create or update")
for page in pages:
    items = page if isinstance(page, list) else [page]
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            raise SystemExit("rulesets API returned an invalid ruleset summary; refusing to create or update")
        print(item["id"])
PY
)"
details="["
separator=""
for ruleset_id in $ids; do
    details+="${separator}$(gh api "repos/${REPOSITORY}/rulesets/${ruleset_id}")"
    separator=","
done
details+="]"
plan="$(python3 - "$CREATE_RULESET_NAME" "$details" <<'PY'
import json
import sys

create_name, raw_details = sys.argv[1:]
rulesets = json.loads(raw_details)
if not isinstance(rulesets, list):
    raise SystemExit("rulesets API returned invalid ruleset details; refusing to create or update")
for item in rulesets:
    if not isinstance(item, dict) or not all(
        key in item for key in ("id", "name", "target", "conditions", "rules")
    ):
        raise SystemExit("rulesets API returned an invalid ruleset; refusing to create or update")

# bifrost-dev's default branch is master, so a ruleset scoped to the default
# branch protects master.
MASTER_REFS = {"refs/heads/master", "~DEFAULT_BRANCH"}

def targets_master(item):
    conditions = item.get("conditions", {})
    refs = conditions.get("ref_name", {}) if isinstance(conditions, dict) else {}
    include = refs.get("include", []) if isinstance(refs, dict) else []
    return (item.get("target") == "branch"
            and isinstance(include, list)
            and bool(MASTER_REFS & set(include)))

def has_existing_guards(item):
    rules = item.get("rules", [])
    types = {rule.get("type") for rule in rules if isinstance(rule, dict)}
    return {"deletion", "non_fast_forward"} <= types

master_rulesets = [item for item in rulesets if targets_master(item)]
matches = [item for item in master_rulesets if has_existing_guards(item)]
if len(matches) > 1:
    named = [item for item in matches if item.get("name") == create_name]
    if len(named) == 1:
        matches = named
    else:
        raise SystemExit("multiple master rulesets match the existing guards; refusing to guess")

if matches:
    existing = matches[0]
    ruleset_id = existing.get("id")
    name = existing.get("name")
    if not isinstance(ruleset_id, int) or not isinstance(name, str) or not name:
        raise SystemExit("matching master ruleset has no numeric id or name; refusing to guess")
    action = "update"
    # Keep the existing ruleset's branch targeting exactly as it is.
    conditions = existing["conditions"]
else:
    if master_rulesets:
        raise SystemExit(
            "a ruleset already targets master but does not contain both existing guards; "
            "refusing to create a duplicate"
        )
    ruleset_id = None
    name = create_name
    action = "create"
    conditions = {"ref_name": {"include": ["refs/heads/master"], "exclude": []}}

request_body = {
    "name": name,
    "target": "branch",
    "enforcement": "active",
    "conditions": conditions,
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": False,
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "mergemarshall/verdict", "integration_id": 5203169},
                ],
                "strict_required_status_checks_policy": True,
            },
        },
    ],
    "bypass_actors": [],
}
json.dump({
    "action": action,
    "ruleset_id": ruleset_id,
    "ruleset_name": name,
    "request_body": request_body,
}, sys.stdout, indent=2)
sys.stdout.write("\n")
PY
)"

printf '%s\n' "$plan"
if [[ $dry_run -eq 1 ]]; then
    exit 0
fi

read -r -p "Apply this ruleset to ${REPOSITORY}? Type 'yes' to continue: " answer
if [[ "$answer" != "yes" ]]; then
    echo "No changes made." >&2
    exit 1
fi

action="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])' <<<"$plan")"
ruleset_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["ruleset_id"] or "")' <<<"$plan")"
ruleset_name="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["ruleset_name"])' <<<"$plan")"
request_body="$(python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["request_body"]))' <<<"$plan")"

if [[ "$action" == "update" ]]; then
    gh api "repos/${REPOSITORY}/rulesets/${ruleset_id}" --method PUT --input - <<<"$request_body"
    echo "Updated ruleset '${ruleset_name}' (id ${ruleset_id})."
else
    gh api "repos/${REPOSITORY}/rulesets" --method POST --input - <<<"$request_body"
    echo "Created ruleset '${ruleset_name}'."
fi
