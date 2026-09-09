#!/usr/bin/env bash
#
# Attribute each pushed commit to the PR whose merge introduced it, DERIVED from
# GitHub rather than parsed from the commit subject (backend#3365).
#
# The subject-grep this replaces read `(#N)` / `Merge pull request #N` out of the
# squash subject. But the org commit convention is `type(scope): summary
# (backend#N)`, which puts a TICKET in the `(#N)` slot, and an edited squash
# subject may carry no `(#N)` at all -- so the grep attributed the WRONG card (an
# issue number) or none. Measured on the 2026-09-07 staging hop, 2 of 48 cards:
#   * client#985           subject `... (#979)`            -> read `#979`, an ISSUE
#   * tracebloc-engine#914 subject `... (backend#3013)`    -> `(backend#3013)` matched nothing
# Both shipped code that sat at "On dev" while the board understated it, so fr-gate
# later blocked a prod leg for work that had already promoted.
#
# `GET /repos/{repo}/commits/{sha}/pulls` returns the merged PR that INTRODUCED the
# commit -- the same read `release-train/scripts/hop-status.sh` uses, and its
# contract is branch-independent (the feature PR, whatever its base). So take the
# merged PR the API returns; do NOT filter by base. On a staging/main hop the
# feature commit's PR has base `develop`, not the pushed branch -- filtering by
# base was the whole bug, re-created (Bugbot/@saadqbal on .github#438). The one
# thing to keep OUT is a promotion PR (`release-train/*` / `hotfix*` head): those
# carry no kanban card, so attributing one would move nothing or the wrong thing.
#
# Fall back to the subject grep only when the API returns NO PR, and distinguish a
# genuinely empty answer from a FAILED read (a 403/5xx) -- each gets its own
# `::warning::` so an unattributed or degraded commit is visible, never silent.
#
# Environment:
#   BEFORE              github.event.before (zero hash on a branch's first push)
#   SHA                 github.sha (tip of the push)
#   GITHUB_REPOSITORY   owner/repo
#   GITHUB_OUTPUT       optional; when set, `prs=<list>` is appended for the step
#
# Prints "Found PRs: <space-separated>" as its last line (the selftest reads it).
set -euo pipefail

: "${BEFORE:?BEFORE is required}"
: "${SHA:?SHA is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

# First push to a branch reports a zero hash -- fall back to the last 50 commits.
# Keep the range as an ARRAY: the zero-hash path needs two argv entries
# (--max-count=50 and the SHA); a plain quoted string would hand git one bogus
# argument and break the first-push path.
if [ "$BEFORE" = "0000000000000000000000000000000000000000" ]; then
  range=(--max-count=50 "$SHA")
else
  range=("$BEFORE..$SHA")
fi

# The merged, NON-promotion PRs the API associates with a commit. A promotion PR
# (release-train/* or hotfix* head) is excluded by head ref -- explicit, per
# @saadqbal on .github#438, rather than leaning on "it has no card anyway".
API_JQ='.[] | select(.merged_at != null and (((.head.ref) // "") | test("^release-train/|^hotfix") | not)) | .number'

# The fallback ONLY: PR numbers named in a single commit's subject. Squash subject
# `Title (#NNN)`, merge-commit subject `Merge pull request #NNN`. `|| true` so a
# subject that names nothing is empty rather than a `set -e` exit under the pipe.
subject_prs() {
  git log --format='%s' -1 "$1" \
    | grep -oE '\(#[0-9]+\)|Merge pull request #[0-9]+' \
    | grep -oE '[0-9]+' || true
}

# Capture the commit list with an explicit status check. `for sha in $(git log
# ...)` swallows a git-log failure (a missing BEFORE after a force-push, an
# invalid range) into an EMPTY word list, and errexit does NOT fire on a command
# substitution in a for-loop head -- so the script would write `prs=` and exit 0,
# reporting a clean empty run for a range it never read (Bugbot on .github#438).
# The earlier inline assignment aborted on the same failure; restore that.
if ! commits=$(git log --format='%H' "${range[@]}"); then
  echo "::error::git log failed for range '${range[*]}' (a missing BEFORE after a force-push, or an invalid range); cannot attribute commits." >&2
  exit 1
fi

prs=""
# A FAILED API read below fails the whole step (see the failure branch): track it
# and exit non-zero at the end rather than degrading silently.
api_read_failed=0
for sha in $commits; do
  # Capture the API read's SUCCESS separately from its OUTPUT: a failed call (rate
  # limit, 5xx) must not read as "no PR" and silently degrade to the subject grep
  # while the log claims the API answered (Bugbot/@saadqbal on .github#438).
  # Keep the read's STDERR: for twelve hours on 2026-09-08/09 every private-repo
  # run printed the "rate limit / 5xx" guess below while the real answer was a
  # 403 from a token without `pull-requests: read` (backend#3447). The status
  # belongs in the error line, not in a discarded stream.
  api_err=$(mktemp)
  if api=$(gh api "repos/${GITHUB_REPOSITORY}/commits/${sha}/pulls" --jq "$API_JQ" 2>"$api_err"); then
    if [ -n "$api" ]; then
      prs+=" ${api}"
      continue
    fi
    # The API answered and named no merged non-promotion PR -- a real "none".
    sub=$(subject_prs "$sha")
    if [ -n "$sub" ]; then
      prs+=" ${sub}"
      echo "::warning::commit ${sha}: the API names no merged PR for it; attributed from its SUBJECT ($(echo "$sub" | tr '\n' ' ')). Verify its card."
    else
      echo "::warning::commit ${sha}: no PR could be attributed (the API names none; the subject names none) - its card was not advanced."
    fi
  else
    # The API READ itself FAILED (403 from a token without pull-requests:read, 5xx,
    # rate limit). Do NOT degrade to the
    # subject grep this PR exists to replace -- that re-introduces the wrong-card
    # risk -- and do NOT let the run go green: this is a one-shot `push` job, so a
    # silently-skipped commit is never re-examined and its card stays behind the
    # shipped code, the very outage this fixes. Record it and fail the step at the
    # end so the read is retried (Bugbot High on .github#438).
    api_read_failed=1
    # No pipe here: `tr | head -c` closes early and pipefail reads that as a
    # failure of the message itself (quality/pipefail-early-close).
    api_why=$(<"$api_err")
    api_why=${api_why//$'\n'/ }
    api_why=${api_why:0:300}
    echo "::error::commit ${sha}: the /pulls API read FAILED (gh said: ${api_why:-nothing on stderr}); NOT attributing from the unreliable subject, and failing the step so it is retried rather than leaving the card behind."
  fi
done

# Numeric, unique, space-separated. `printf ... | ...` splits the accumulated
# words onto their own lines for sort; the trailing-space trim keeps the output
# stable for the caller's `for prnum in $PR_NUMBERS`. `|| true` because a push
# where NOTHING attributes leaves `grep` matching zero lines and exiting 1 --
# under `pipefail` that would crash the step before it can report the empty
# result, which is a legitimate outcome, not an error.
prs=$(printf '%s\n' $prs | grep -E '^[0-9]+$' | sort -un | tr '\n' ' ' | sed 's/ *$//' || true)

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "prs=${prs}" >> "$GITHUB_OUTPUT"
fi
echo "Found PRs: ${prs}"

# A failed API read above is not a clean run: exit non-zero so the step is RED and
# gets retried, rather than advancing a partial set and leaving the failed
# commits' cards behind. The successful attributions are still printed/emitted
# above for visibility (Bugbot High on .github#438).
if [ "$api_read_failed" -ne 0 ]; then
  exit 1
fi
