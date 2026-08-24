#!/usr/bin/env bash

set -uo pipefail
T="$1"; LABEL="$2"
out() { printf '%s\t%s\t%s\n' "$LABEL" "$1" "$2" >> "$RESULTS"; }

req() { # req <path> -> prints "STATUS<TAB>BODY"
  curl -sS -o /tmp/body.json -w '%{http_code}' \
    -H "Authorization: token $T" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/$1"
}

# --- 1. classic branch protection (bricked-prs, via caller_drift.read_protection)
S=$(req "repos/tracebloc/.github/branches/develop/protection")
out "protection.status" "$S"
out "protection.required_status_checks" "$(jq -r 'if has("required_status_checks") then "PRESENT" else "ABSENT" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "protection.rsc.checks[]" "$(jq -r '(.required_status_checks.checks // []) | length | tostring' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "protection.rsc.contexts[]" "$(jq -r '(.required_status_checks.contexts // []) | length | tostring' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "protection.rsc.strict" "$(jq -r 'if (.required_status_checks//{})|has("strict") then (.required_status_checks.strict|tostring) else "ABSENT" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "protection.enforce_admins" "$(jq -r 'if has("enforce_admins") then (.enforce_admins.enabled|tostring) else "ABSENT" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "protection.rpr.count" "$(jq -r 'if (.required_pull_request_reviews//{})|has("required_approving_review_count") then (.required_pull_request_reviews.required_approving_review_count|tostring) else "ABSENT" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "protection.req_conv_resolution" "$(jq -r 'if has("required_conversation_resolution") then (.required_conversation_resolution.enabled|tostring) else "ABSENT" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"

# --- 2. rulesets resolved for a branch (bricked-prs, same reader)
S=$(req "repos/tracebloc/backend/rules/branches/main")
out "rules.status" "$S"
out "rules.count" "$(jq -r 'if type=="array" then length else -1 end | tostring' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "rules.type[0]" "$(jq -r 'if type=="array" and length>0 then (.[0].type // "ABSENT") else "NONE" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "rules.parameters[0]" "$(jq -r 'if type=="array" and length>0 then (if .[0]|has("parameters") then "PRESENT" else "ABSENT" end) else "NONE" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "rules.ruleset_source[0]" "$(jq -r 'if type=="array" and length>0 then (.[0].ruleset_source // "ABSENT") else "NONE" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"

# --- 3. repo object merge settings (merge-settings-drift)
S=$(req "repos/tracebloc/.github")
out "repo.status" "$S"
for f in allow_merge_commit allow_squash_merge allow_rebase_merge delete_branch_on_merge; do
  out "repo.$f" "$(jq -r --arg f "$f" 'if has($f) then (.[$f]|tostring) else "ABSENT" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
done
out "repo.archived" "$(jq -r 'if has("archived") then (.archived|tostring) else "ABSENT" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "repo.fork" "$(jq -r 'if has("fork") then (.fork|tostring) else "ABSENT" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"

# --- 4. branch list (bricked-prs resolve_roles; merge-settings-drift repo list)
S=$(req "repos/tracebloc/.github/branches?per_page=100")
out "branches.status" "$S"
out "branches.count" "$(jq -r 'if type=="array" then length else -1 end | tostring' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"

# --- 5. org repo listing (merge-settings-drift enumerates by listing)
S=$(req "orgs/tracebloc/repos?per_page=100&type=all")
out "orgrepos.status" "$S"
out "orgrepos.count" "$(jq -r 'if type=="array" then length else -1 end | tostring' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "orgrepos.has_merge_fields" "$(jq -r 'if type=="array" and length>0 then (if .[0]|has("allow_rebase_merge") then "PRESENT" else "ABSENT" end) else "NONE" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"

# --- 6. check-suites on a head (bricked-prs head_age_minutes)
SHA=$(GH_TOKEN="$GH_ADMIN" gh api repos/tracebloc/.github/commits/develop --jq .sha)
S=$(req "repos/tracebloc/.github/commits/$SHA/check-suites")
out "checksuites.status" "$S"
out "checksuites.total" "$(jq -r '.total_count // "ABSENT"' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
out "checksuites.created_at[0]" "$(jq -r 'if (.check_suites|length)>0 then (.check_suites[0].created_at // "ABSENT") else "NONE" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"

# --- 7. commit object (bricked-prs fallback clock)
S=$(req "repos/tracebloc/.github/commits/$SHA")
out "commit.status" "$S"
out "commit.committer.date" "$(jq -r '.commit.committer.date // "ABSENT"' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"

# --- 8. statusCheckRollup via GraphQL (bricked-prs open_prs)
GH_TOKEN="$T" gh pr list --repo tracebloc/backend --state open --limit 5 \
  --json number,isDraft,mergeStateStatus,reviewDecision,statusCheckRollup,headRefOid,author \
  > /tmp/prs.json 2>/tmp/prs.err
out "prlist.exit" "$?"
out "prlist.count" "$(jq -r 'if type=="array" then length else -1 end | tostring' /tmp/prs.json 2>/dev/null || echo PARSE_FAIL)"
out "prlist.rollup_present" "$(jq -r 'if type=="array" and length>0 then (if (.[0].statusCheckRollup|type)=="array" then "PRESENT" else "ABSENT" end) else "NONE" end' /tmp/prs.json 2>/dev/null || echo PARSE_FAIL)"
out "prlist.rollup_len[0]" "$(jq -r 'if type=="array" and length>0 then ((.[0].statusCheckRollup // [])|length|tostring) else "NONE" end' /tmp/prs.json 2>/dev/null || echo PARSE_FAIL)"
out "prlist.mergeState[0]" "$(jq -r 'if type=="array" and length>0 then (.[0].mergeStateStatus // "ABSENT") else "NONE" end' /tmp/prs.json 2>/dev/null || echo PARSE_FAIL)"
out "prlist.reviewDecision[0]" "$(jq -r 'if type=="array" and length>0 then (.[0].reviewDecision // "NULL_OR_ABSENT") else "NONE" end' /tmp/prs.json 2>/dev/null || echo PARSE_FAIL)"
out "prlist.err" "$(head -c 120 /tmp/prs.err | tr "\n\t" "  ")"
