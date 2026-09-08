#!/usr/bin/env bash
#
# Attribute each pushed commit to the PR whose merge produced it, DERIVED from
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
# `GET /repos/{repo}/commits/{sha}/pulls` returns the PR whose merge produced the
# commit -- the same read `release-train/scripts/hop-status.sh` and the FR tooling
# already use. Keep only a MERGED PR whose base is THIS branch (the push target,
# which is exactly a PR's base). Fall back to the subject grep ONLY when the API
# answers nothing, and emit a `::warning::` naming the sha so an unattributed
# commit is visible rather than silent.
#
# Environment:
#   BEFORE              github.event.before (zero hash on a branch's first push)
#   SHA                 github.sha (tip of the push)
#   BRANCH              github.ref_name (the pushed branch = a merged PR's base)
#   GITHUB_REPOSITORY   owner/repo
#   GITHUB_OUTPUT       optional; when set, `prs=<list>` is appended for the step
#
# Prints "Found PRs: <space-separated>" as its last line (the selftest reads it).
set -euo pipefail

: "${BEFORE:?BEFORE is required}"
: "${SHA:?SHA is required}"
: "${BRANCH:?BRANCH is required}"
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

# The fallback ONLY: PR numbers named in a single commit's subject. Squash subject
# `Title (#NNN)`, merge-commit subject `Merge pull request #NNN`. `|| true` so a
# subject that names nothing is empty rather than a `set -e` exit under the pipe.
subject_prs() {
  git log --format='%s' -1 "$1" \
    | grep -oE '\(#[0-9]+\)|Merge pull request #[0-9]+' \
    | grep -oE '[0-9]+' || true
}

prs=""
for sha in $(git log --format='%H' "${range[@]}"); do
  # The PR whose merge produced this commit, filtered to a merged PR that targeted
  # THIS branch. `|| true` so an API hiccup falls through to the subject rather
  # than failing the whole step closed on one unreadable commit.
  api=$(gh api "repos/${GITHUB_REPOSITORY}/commits/${sha}/pulls" \
          --jq ".[] | select(.merged_at != null and .base.ref == \"${BRANCH}\") | .number" \
          2>/dev/null || true)
  if [ -n "$api" ]; then
    prs+=" ${api}"
    continue
  fi
  sub=$(subject_prs "$sha")
  if [ -n "$sub" ]; then
    prs+=" ${sub}"
    echo "::warning::commit ${sha}: PR attributed from its SUBJECT ($(echo "$sub" | tr '\n' ' ')), not the API (no merged PR with base=${BRANCH}). Verify its card."
  else
    echo "::warning::commit ${sha}: no PR could be attributed (API returned none; subject names none) - its card was not advanced."
  fi
done

# Numeric, unique, space-separated. `printf ... | ...` splits the accumulated
# words onto their own lines for sort; the trailing-space trim keeps the output
# stable for the caller's `for prnum in $PR_NUMBERS`. `|| true` because a push
# where NOTHING attributes (all commits unattributable) leaves `grep` matching
# zero lines and exiting 1 -- under `pipefail` that would crash the step before
# it can report the empty result, which is a legitimate outcome, not an error.
prs=$(printf '%s\n' $prs | grep -E '^[0-9]+$' | sort -un | tr '\n' ' ' | sed 's/ *$//' || true)

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "prs=${prs}" >> "$GITHUB_OUTPUT"
fi
echo "Found PRs: ${prs}"
