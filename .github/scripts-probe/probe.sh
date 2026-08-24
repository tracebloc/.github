#!/usr/bin/env bash
set -uo pipefail
T="$1"; LABEL="$2"
out() { printf '%s\t%s\t%s\n' "$LABEL" "$1" "$2" >> "$RESULTS"; }
req() {
  curl -sS -o /tmp/body.json -w '%{http_code}' \
    -H "Authorization: token $T" -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" "https://api.github.com/$1"
}

# EDGE 1: an UNPROTECTED branch must still answer 404 (a FACT: "no classic
# protection"), not 403 (a read failure). read_protection turns 404 into
# "no classic protection" and anything else into an error that reddens the run,
# so a narrow token that converts 404->403 would redden every unprotected branch.
S=$(req "repos/tracebloc/.github/branches/ci%2Fwip-limit-check-fixups/protection")
out "unprot.status" "$S"
out "unprot.message" "$(jq -r '.message // "NONE"' /tmp/body.json 2>/dev/null | head -c 40)"

# EDGE 2: ruleset-only branch (rules present, classic absent).
S=$(req "repos/tracebloc/backend/branches/main/protection")
out "rulesetonly.status" "$S"

# EDGE 3: does the merge-settings visibility differ on a PUBLIC repo?
S=$(req "repos/tracebloc/model-zoo")
out "public.repo.status" "$S"
for f in allow_merge_commit allow_squash_merge allow_rebase_merge delete_branch_on_merge; do
  out "public.repo.$f" "$(jq -r --arg f "$f" 'if has($f) then (.[$f]|tostring) else "ABSENT" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
done

# EDGE 4: same, on a PRIVATE repo other than the one already measured.
S=$(req "repos/tracebloc/backend")
out "private2.repo.status" "$S"
for f in allow_merge_commit allow_rebase_merge delete_branch_on_merge; do
  out "private2.repo.$f" "$(jq -r --arg f "$f" 'if has($f) then (.[$f]|tostring) else "ABSENT" end' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"
done

# EDGE 5: the bricked-prs reader end-to-end on a THIRD repo, protection + rules.
S=$(req "repos/tracebloc/client-runtime/branches/develop/protection")
out "third.protection.status" "$S"
out "third.rsc.checks[]" "$(jq -r '(.required_status_checks.checks // []) | length | tostring' /tmp/body.json 2>/dev/null || echo PARSE_FAIL)"

# EDGE 6: rollup on a repo with a big PR set, to confirm actions:read holds up.
GH_TOKEN="$T" gh pr list --repo tracebloc/client --state open --limit 10 \
  --json number,statusCheckRollup,mergeStateStatus,reviewDecision > /tmp/prs.json 2>/tmp/prs.err
out "third.prlist.exit" "$?"
out "third.prlist.count" "$(jq -r 'if type=="array" then length else -1 end|tostring' /tmp/prs.json 2>/dev/null || echo PARSE_FAIL)"
out "third.prlist.err" "$(head -c 100 /tmp/prs.err | tr "\n\t" "  ")"
