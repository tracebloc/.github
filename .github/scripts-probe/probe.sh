#!/usr/bin/env bash
set -uo pipefail
T="$1"; LABEL="$2"
out() { printf '%s\t%s\t%s\n' "$LABEL" "$1" "$2" >> "$RESULTS"; }

# ---- END TO END 1: the REAL bricked-prs.py, over a private + a public repo.
# Reads protection, rulesets, the branch list, the rollup, check-suites and the
# commit clock -- every call the workflow makes, through the production code
# path rather than a re-implementation of it.
GH_TOKEN="$T" python3 scripts/bricked-prs.py \
  --repo backend --repo client-runtime --repo client --repo .github \
  > /tmp/bricked.out 2>/tmp/bricked.err
rc=$?
out "bricked.rc" "$rc"
out "bricked.could_not_audit" "$(grep -c 'COULD NOT AUDIT' /tmp/bricked.out 2>/dev/null || echo 0)"
out "bricked.first_cna" "$(grep -m1 'COULD NOT AUDIT' /tmp/bricked.out 2>/dev/null | head -c 110 | tr '\n\t' '  ')"
out "bricked.stderr" "$(head -c 110 /tmp/bricked.err | tr '\n\t' '  ')"
out "bricked.lines" "$(wc -l < /tmp/bricked.out | tr -d ' ')"

# ---- END TO END 2: the REAL merge-settings-drift audit loop, verbatim.
GH_TOKEN="$T" gh repo list tracebloc --limit 200 --json name,isArchived,isFork \
  --jq '.[] | select(.isArchived == false and .isFork == false) | .name' 2>/tmp/list.err | sort > /tmp/repos.txt
out "msd.list_rc" "$?"
out "msd.list_count" "$(wc -l < /tmp/repos.txt | tr -d ' ')"
out "msd.list_err" "$(head -c 90 /tmp/list.err | tr '\n\t' '  ')"
unreadable=0; readable=0
while IFS= read -r repo; do
  [ -n "$repo" ] || continue
  if ! settings=$(GH_TOKEN="$T" gh api "repos/tracebloc/$repo" \
       --jq '[.allow_merge_commit, .allow_squash_merge, .allow_rebase_merge, .delete_branch_on_merge] | @tsv' \
       < /dev/null 2>/dev/null); then
    unreadable=$((unreadable+1)); continue
  fi
  bad=0
  for i in 1 2 3 4; do
    v=$(printf '%s' "$settings" | cut -f"$i")
    case "$v" in true|false) ;; *) bad=1 ;; esac
  done
  if [ "$bad" = 1 ]; then unreadable=$((unreadable+1)); else readable=$((readable+1)); fi
done < /tmp/repos.txt
out "msd.readable" "$readable"
out "msd.unreadable" "$unreadable"
