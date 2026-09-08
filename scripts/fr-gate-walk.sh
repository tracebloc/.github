#!/usr/bin/env bash
# fr-gate-walk.sh — the FR gate's attribution walk, as one script with three
# modes (backend#3323, RFC-0075 D6 prerequisite).
#
# WHY THIS FILE EXISTS
# --------------------
# `fr-gate.yml` used to hold its walk inline in two `run:` blocks and export
# nothing. The release train needs the same walk to cut the prod hop at the FR
# FRONTIER (release-train#102, RFC-0075 D6): the newest commit in
# `<prod>..staging` such that every item contained in `<prod>..<commit>` is at
# `Ready for prod` or later. A resolver written in release-train would be a
# second definition of "which items does this range contain" -- the drift this
# org keeps finding (CLAUDE.md rule 9) -- and the gate is the one that decides
# the merge, so the gate's walk is the authority. Hence: the walk moved HERE,
# the gate calls it, and the train fetches THIS file by sha.
#
# THE THREE MODES, and what each is a contract for
# ------------------------------------------------
#   discover   fr-gate.yml step "Discover items in this promotion". Reads the
#              gate's step env, prints the same log lines, writes the same three
#              outputs (`prs`, `unattributed`, `changed_files`) to $GITHUB_OUTPUT.
#   verify     fr-gate.yml step "Verify each item is in required Status". Same
#              env, same text, same exit code (0 pass / 1 fail).
#   frontier   the train's resolver. Walks `<TARGET>..<SOURCE>` and reports the
#              frontier as a key=value block on STDOUT (human log on stderr):
#                frontier_state=advanced | at-target | empty-range | unknown
#                frontier=<sha>  target=<sha>  source=<sha>
#                range_commits=N  frontier_commits=M  behind=N-M
#                blocker=<first blocker>  blockers=<all, "; "-joined>  items_beyond_ready=K
#              THREE DISTINCT FACTS that must never be printed for one another
#              (#102): `advanced` (a real cut), `at-target` (the range is
#              non-empty and NOTHING in it has passed FR -- the frontier is the
#              target itself, the hop is a no-op for this repo and the first
#              blocker names who needs to run FR next), and `empty-range` (there
#              are no commits to compare at all). Exit 0 for all three: they are
#              answers. Exit 3 = UNKNOWN: the board or the commits/pulls API could
#              not be read, and "cannot tell" is never a frontier (rule 3).
#
# WHAT THE FRONTIER WALK IS, PRECISELY
# ------------------------------------
# 1. The range is `git rev-list --topo-order --reverse TARGET..SOURCE` -- every
#    commit SOURCE contributes over TARGET, parents before children. NOT
#    `--first-parent`: staging's first-parent chain is the train's mirror
#    merges, so a first-parent frontier is hop-granular and one un-FR'd card
#    pins its whole staging hop. Walking the full range makes the frontier
#    CARD-granular: an inner `develop` commit of a hop is a legal mirror head
#    (it is an ancestor of `staging`, and D1's tree lookup resolves it because
#    `develop` built that tree).
# 2. Attribution is the gate's own two passes over the whole range -- a true
#    PR-merge vouches for M^1..M^2 and contributes its PR only when it is that
#    PR's `merge_commit_sha` (Bugbot #73); every other non-merge commit is looked
#    up on `commits/{sha}/pulls`; release-train/* promotion PRs are transparent
#    plumbing. Identical code path to `discover`: there is one walk.
# 3. A commit is BAD when it has no merged PR (a direct push), or any PR it
#    carries is below the required rank, or not on the board. A candidate C
#    passes when no bad commit is an ancestor-or-self of C. The frontier is the
#    passing candidate whose `TARGET..C` is largest (the "newest" in the only
#    sense a DAG has); no passing candidate means `at-target`.
# 4. COMMITS INSIDE A PR ARE NOT CANDIDATES. A commit vouched by a PR-merge M
#    (in M^1..M^2, not on M's mainline side) is part of that PR, and a cut there
#    would ship a partial PR -- "a prefix is not a partial payload" holds only if
#    the prefix ends on a commit that was actually a tip of the integration
#    branch. Squash commits and the merge commits themselves are candidates;
#    the merged-in commits are not.
# 5. Merge commits with no vouching PR (a routine merge, the train's own mirror
#    merge) neither vouch nor contribute and are NOT bad -- exactly as the gate
#    treats them. Only a NON-merge commit can be unattributed.
#
# FAIL CLOSED, in every mode. `discover` marks a commit the API never answered
# for as unattributable and `verify` blocks on it; `frontier` refuses outright
# (exit 3), because an unverifiable commit cannot bound a cut in either
# direction. A PR whose Status cannot be read after 5 attempts blocks the gate
# and makes the frontier UNKNOWN; a PR with no card blocks the gate and is a
# BLOCKER for the frontier (it cannot have passed FR; it is named so someone
# adds the card).
#
# PINNING FROM ANOTHER REPO. This file is self-contained (bash, git, gh, jq) and
# is meant to be fetched by COMMIT SHA, never `@main`:
#   curl -fsSL --tlsv1.2 "https://raw.githubusercontent.com/tracebloc/.github/<sha>/scripts/fr-gate-walk.sh"
# The gate itself checks it out of tracebloc/.github at `inputs.quality-ref`
# (default `main`, the version that matches the `@main` callers), the same shape
# bugbot-gate.yml and code-quality.yml use.
#
# ENVIRONMENT (the gate's step env is the contract; nothing is positional
# except the mode):
#   all modes   GH_TOKEN
#   discover    REPO (owner/name) BASE_SHA HEAD_SHA GITHUB_OUTPUT   [REPO_FULL]
#   verify      ORG PROJECT_NUMBER REPO_FULL REQUIRED PRS UNATTRIB PROMOTION_PR
#               CHANGED_FILES
#   frontier    REPO ORG PROJECT_NUMBER TARGET SOURCE [REQUIRED=Ready for prod]
#   FR_WALK_RETRY_UNIT  seconds per retry step (default 2; the selftest sets 0).
#               The retry COUNTS are fixed -- only the wait scales.
set -euo pipefail

MODE="${1:-}"
RC_UNKNOWN=3
RETRY_UNIT="${FR_WALK_RETRY_UNIT:-2}"

usage() {
  echo "usage: fr-gate-walk.sh discover|verify|frontier   (env-driven; see the header)" >&2
  exit 2
}

need() {   # need VAR ... -- every named variable must be set and non-empty
  local v
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then
      echo "fr-gate-walk.sh $MODE: \$$v is required and empty -- refusing to guess (fail closed)." >&2
      exit 2
    fi
  done
}

# ---------------------------------------------------------------------------
# Shared primitives. These are the gate's own, moved -- not rewritten.
# ---------------------------------------------------------------------------

# repos/{owner}/{repo}/commits/{sha}/pulls returns the actual MERGED PR(s) that
# introduced a commit -- the source of truth for both "which PRs are in this
# range" and "is every commit attributable". Retry, and report an API error as
# rc 1 so the caller fails CLOSED (an unverifiable commit must never pass).
fetch_pulls() {   # $1=sha; echoes the commits/pulls JSON array; rc 1 = API error
  local attempt out
  for attempt in 1 2 3; do
    if out=$(gh api "repos/$REPO/commits/$1/pulls" 2>/dev/null); then
      printf '%s' "$out"
      return 0
    fi
    sleep $((attempt * RETRY_UNIT))
  done
  return 1
}

# Pipeline rank for each kanban Status. An item satisfies the gate when it is
# AT OR BEYOND the required stage -- e.g. an item already in "Prod" trivially
# satisfies an "On dev" gate. Strict equality used to falsely block
# already-promoted items that reappear in a later promotion's commit range.
# Unknown statuses return empty and fall through to the strict-equality path,
# preserving the old behavior.
rank() {
  case "$1" in
    "Backlog")           echo 1  ;;
    "North Stars")       echo 2  ;;
    "Ready")             echo 3  ;;
    "In progress")       echo 4  ;;
    "Code review")       echo 5  ;;
    "On dev")            echo 6  ;;
    # RANK 7: the agent stage, between `On dev` and human FR. READ-ONLY for
    # now -- nothing writes this value yet (#1578 does that, in a LATER hop).
    # An unknown Status returns "" here, the guard below fails, and evaluation
    # falls through to strict equality: the card BLOCKS every prod promotion
    # carrying it. That is the backend#1411 shape, and the column already
    # EXISTS on the board, so this was a live landmine waiting for the first
    # card to land in it (#1577, RFC-BACKEND-1552 D5).
    "Staging (agent review)")  echo 7  ;;
    # Both names, for the #1592 rename window.
    "Staging (human review)"|"FR on staging")     echo 8  ;;
    "Ready for prod")    echo 9  ;;
    "Prod")              echo 10 ;;
    # Terminal, and therefore NOT blocking. Both fell through to "" and then to
    # the strict-equality path, so a single Done card blocked every prod
    # promotion that carried it (RFC-BACKEND-1405 D8, backend#1411).
    "Done")              echo 11 ;;
    "Cancelled")         echo 11 ;;
    *)                   echo "" ;;
  esac
}

# Read one PR's Status column, retrying on both an API failure and an empty
# result.
#
# The empty case needs retrying because it is usually a RACE, not an absence:
# fr-gate-caller.yml and add-to-kanban.yml both fire on `pull_request: opened`
# and run concurrently, so the gate regularly queries the board before the card
# has been created. Observed on tracebloc/.github#61 -- the gate finished at
# 16:48:09 and the card appeared at 16:48:14. It won the race by one second and
# passed.
#
# Echoes the column on success. Returns 1 if the API never answered, 2 if the
# API answered but the PR has no card. The caller must treat both as blocking;
# neither is evidence that review happened.
resolve_status() {
  local num attempt api_ok resp status
  num="$1"
  attempt=0
  api_ok=0
  while [ "$attempt" -lt 5 ]; do
    attempt=$((attempt + 1))
    # shellcheck disable=SC2016  # $names are GraphQL variables, not shell - keep literal
    if resp=$(gh api graphql -f query='
      query($org: String!, $repo: String!, $num: Int!) {
        repository(owner: $org, name: $repo) {
          pullRequest(number: $num) {
            projectItems(first: 10) {
              nodes {
                project { number }
                fieldValueByName(name: "Status") {
                  ... on ProjectV2ItemFieldSingleSelectValue { name }
                }
              }
            }
          }
        }
      }' -F org="$ORG" -F repo="$REPO_NAME" -F num="$num" 2>/dev/null); then
      api_ok=1
      # Parse is deliberately non-fatal. Under `set -euo pipefail` a jq error or
      # a SIGPIPE from `head` closing the pipe early would otherwise abort
      # resolve_status mid-loop, skipping the remaining retries and returning an
      # ambiguous code. `2>/dev/null` + `|| true` turn any parse failure into an
      # empty result, which just falls through to the next attempt. (The gh call
      # above is captured with `2>/dev/null`, not `2>&1`, so a stderr warning can
      # never be fed into jq as if it were the JSON body.)
      status=$(printf '%s' "$resp" \
        | jq -r --arg n "$PROJECT_NUMBER" '.data.repository.pullRequest.projectItems.nodes[]?
            | select(.project.number == ($n | tonumber)) | .fieldValueByName.name // ""' 2>/dev/null \
        | head -1 || true)
      if [ -n "$status" ]; then
        printf '%s' "$status"
        return 0
      fi
    else
      echo "    (attempt $attempt/5: API call failed for #$num)" >&2
    fi
    if [ "$attempt" -lt 5 ]; then sleep $((attempt * RETRY_UNIT)); fi
  done
  [ "$api_ok" -eq 1 ] && return 2
  return 1
}

# ---------------------------------------------------------------------------
# The walk. Reads WALK_BASE..WALK_HEAD in the current git checkout and records,
# per commit, in $WALK_DIR:
#   attr/<sha>     the merged PR numbers that introduced it (may be empty)
#   apifail/<sha>  present when commits/pulls never answered (3 attempts)
#   covered/<sha>  present when a true PR-merge vouches for it (it is INSIDE a PR)
#   merges         the true PR-merge shas, in walk order
# and sets ATTRIBUTED / UNATTRIB exactly as the inline block did, so `discover`
# prints what it always printed. Files rather than arrays: this must run on the
# bash 3.2 a macOS `make check` has, and a file per sha is the portable map.
# ---------------------------------------------------------------------------
walk_attribution() {
  local msha csha raw mprs cprs covered p1 p2 cover_of
  mkdir -p "$WALK_DIR/attr" "$WALK_DIR/apifail" "$WALK_DIR/covered"
  : > "$WALK_DIR/merges"

  ATTRIBUTED=""   # real merged PR numbers (deduped at the end)
  UNATTRIB=""     # commits introduced by no merged PR -- i.e. a direct push

  # Pass 1: a merge commit vouches for its merged-in range (M^1..M^2) and
  # contributes its PR ONLY IF it is that PR's actual merge_commit_sha.
  # commits/pulls returns EVERY PR whose branch CONTAINS the commit, so a sync
  # merge ("Merge branch 'develop' into feature") also lists the feature PR;
  # without the merge_commit_sha match it would enter MERGES and mark a
  # direct-push-to-base it happened to pull in as "covered", skipping Pass 2 and
  # reopening the fail-open hole (Bugbot #73). Requiring merge_commit_sha==sha
  # admits only true PR merges -- verified on real history: the sync merge
  # ba80b3a (backend #1128 back-merge) yields no merge_commit_sha match and drops
  # to Pass 2, while every "Merge pull request #N" matches #N. A routine or
  # non-PR merge neither vouches nor contributes; any push it carries is caught
  # by Pass 2.
  MERGES=""
  while IFS= read -r msha; do
    [ -z "$msha" ] && continue
    : > "$WALK_DIR/attr/$msha"
    if raw=$(fetch_pulls "$msha"); then
      # Promotion PRs (head release-train/*) are the train's own plumbing: they
      # never vouch for their range and are never gated items -- their cargo is
      # attributed individually (inner merges here, non-merge commits in Pass 2).
      mprs=$(printf '%s' "${raw:-[]}" | jq -r --arg s "$msha" \
        '[.[] | select(.merged_at != null and .merge_commit_sha == $s
                 and (((.head.ref // "") | startswith("release-train/")) | not))
          | .number] | join(" ")')
      if [ -n "$mprs" ]; then
        ATTRIBUTED="$ATTRIBUTED $mprs"
        MERGES="$MERGES $msha"
        printf '%s\n' "$mprs" > "$WALK_DIR/attr/$msha"
        printf '%s\n' "$msha" >> "$WALK_DIR/merges"
      fi
    else
      echo "  BLOCKED ${msha} -- merge commit unverifiable via API after 3 attempts (failing closed)"
      UNATTRIB="$UNATTRIB ${msha}"
      : > "$WALK_DIR/apifail/$msha"
    fi
  done < <(git log --merges --format='%H' "$WALK_BASE..$WALK_HEAD")

  # Pass 2: non-merge commits not already vouched by a true PR-merge above. A
  # merge M introduces a commit only if it is in M^1..M^2 -- on the merged-in
  # side and NOT already on the mainline side; requiring "not ancestor of ^1"
  # stops a base-branch push being laundered into attribution once a feature
  # branch merges the base in (Bugbot #72).
  while IFS= read -r csha; do
    [ -z "$csha" ] && continue
    covered=0
    cover_of=""
    for msha in $MERGES; do
      p2=$(git rev-parse --verify --quiet "${msha}^2") || continue
      p1=$(git rev-parse --verify --quiet "${msha}^1") || continue
      if git merge-base --is-ancestor "$csha" "$p2" 2>/dev/null \
         && ! git merge-base --is-ancestor "$csha" "$p1" 2>/dev/null; then
        covered=1
        cover_of="$msha"
        break
      fi
    done
    if [ "$covered" -eq 1 ]; then
      # Inside the PR that M merged: it carries M's PR(s), and it is not a
      # candidate for a cut (header, point 4).
      cp "$WALK_DIR/attr/$cover_of" "$WALK_DIR/attr/$csha"
      : > "$WALK_DIR/covered/$csha"
      continue
    fi
    : > "$WALK_DIR/attr/$csha"
    if raw=$(fetch_pulls "$csha"); then
      # commits/pulls lists every PR whose branch contains the commit, so a
      # promotion PR (head release-train/*) shows up for every commit it
      # carried. Filter it out; a commit whose ONLY merged PR is a promotion PR
      # is a direct push that rode the train -- correctly unattributed.
      cprs=$(printf '%s' "${raw:-[]}" | jq -r \
        '[.[] | select(.merged_at != null
                 and (((.head.ref // "") | startswith("release-train/")) | not))
          | .number] | join(" ")')
      if [ -z "$cprs" ]; then
        UNATTRIB="$UNATTRIB ${csha}"   # no merged PR introduced it: a direct push
      else
        ATTRIBUTED="$ATTRIBUTED $cprs"
        printf '%s\n' "$cprs" > "$WALK_DIR/attr/$csha"
      fi
    else
      echo "  BLOCKED ${csha} -- could not verify via API after 3 attempts (failing closed)"
      UNATTRIB="$UNATTRIB ${csha}"
      : > "$WALK_DIR/apifail/$csha"
    fi
  done < <(git log --no-merges --format='%H' "$WALK_BASE..$WALK_HEAD")
}

# ---------------------------------------------------------------------------
# discover -- fr-gate.yml step "Discover items in this promotion"
# ---------------------------------------------------------------------------
mode_discover() {
  need REPO BASE_SHA HEAD_SHA GITHUB_OUTPUT
  REPO_FULL="${REPO_FULL:-$REPO}"
  WALK_DIR=$(mktemp -d)
  trap 'rm -rf "$WALK_DIR"' EXIT

  # -- Build the item list AUTHORITATIVELY from GitHub's commit->PR attribution,
  # NOT from commit subjects. Scraping "(#N)" from subjects is unreliable three
  # ways, all seen in real ranges (backend#1228): this org's convention is
  # "<subject> (#issue) (#PR)", so it (a) picks up ISSUE numbers, which fail the
  # later pullRequest() lookup and used to fail the gate CLOSED; (b) picks up
  # bare "#N" MENTIONS of unrelated PRs; and (c) MISSES a commit's real PR when
  # the (#PR) is not in the subject at all (rebase-merges, or a squash whose
  # subject kept only the issue ref) -- letting un-reviewed code ride in
  # unevaluated (fail OPEN). The base fetch is --depth=200, so the commit set --
  # and the call count -- is bounded.
  WALK_BASE="$BASE_SHA" WALK_HEAD="$HEAD_SHA" walk_attribution

  PRS=$(echo "$ATTRIBUTED" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -un | tr '\n' ' ' || true)
  echo "Items in promotion (commit->PR attribution): $PRS"
  echo "prs=$PRS" >> "$GITHUB_OUTPUT"
  echo "unattributed=$UNATTRIB" >> "$GITHUB_OUTPUT"

  # Does this range change any FILE at all? A promotion can be ancestry-only:
  # the train's mirror merge records that a branch's history is contained,
  # while the base already holds every byte of that content (data-ingestors
  # 2026-07-30 -- 1 commit ahead, 0 files changed, because a manual promotion
  # had already carried the content to master). There is nothing reviewable in
  # such a range, which is different from "we failed to attribute real content"
  # -- and the fallback in `verify` depends on telling those apart.
  # Ask the compare API, three-dot semantics: "what did HEAD contribute relative
  # to the merge base", NOT "how do the two tips differ".
  #
  # A two-dot `git diff BASE..HEAD` counts files the BASE moved on alone, which
  # is wrong here and would have defeated this whole fix: on data-ingestors
  # two-dot reports 6 files (master's own commits that staging lacks) while
  # three-dot reports 0 -- so the ancestry-only branch would never have been
  # taken and the deadlock would have survived (Bugbot, .github#109).
  #
  # The API is used rather than local three-dot git because this repo's history
  # has multiple merge bases after the mirror reconciliations, and
  # `git diff A...B` silently picks one of them. FAIL CLOSED on any error: an
  # uncounted range must never be treated as empty.
  local attempt
  DIFF_FILES=""
  for attempt in 1 2 3; do
    DIFF_FILES=$(gh api "repos/$REPO_FULL/compare/$BASE_SHA...$HEAD_SHA" \
                   --jq '.files | length' 2>/dev/null) && break
    DIFF_FILES=""
    sleep $((attempt * RETRY_UNIT))
  done
  case "${DIFF_FILES:-}" in
    ''|*[!0-9]*)
      echo "::warning::could not count changed files for this range - treating it as non-empty (fail closed)."
      DIFF_FILES=-1
      ;;
  esac
  echo "changed_files=$DIFF_FILES" >> "$GITHUB_OUTPUT"
  echo "Range contributes $DIFF_FILES file(s) relative to the merge base."
  if [ -n "$UNATTRIB" ]; then
    echo "Unattributable commits (introduced by no merged PR):$UNATTRIB"
  fi
}

# ---------------------------------------------------------------------------
# verify -- fr-gate.yml step "Verify each item is in required Status"
# ---------------------------------------------------------------------------
mode_verify() {
  need ORG PROJECT_NUMBER REPO_FULL REQUIRED PROMOTION_PR
  REPO_NAME="${REPO_FULL#*/}"
  UNATTRIB="${UNATTRIB:-}"
  PRS="${PRS:-}"

  # No attributable items. Two very different causes, and conflating them
  # deadlocked the train (data-ingestors 2026-07-30):
  #
  #  a) ANCESTRY-ONLY range - the only new commit is the train's own mirror
  #     merge and the base already contains every byte of its content (0 files
  #     changed). Nothing shipped, so there is nothing to gate. Gating the
  #     promotion PR here is unsatisfiable BY CONSTRUCTION: set-pr-status parks
  #     it in "Code review", and it can only leave that column by merging -
  #     which this gate blocks.
  #
  #  b) Real content we could not attribute - fall back to gating the promotion
  #     PR, which fails closed and forces a human look. Never let unreviewed
  #     content through just because attribution broke.
  NUMBERS="$PRS"
  if [ -z "$(echo "$NUMBERS" | tr -d ' ')" ]; then
    # UNATTRIB must ALSO be empty. Its fail-closed check lives further down this
    # function, so exiting here would jump over it: a tree-identical range that
    # contains a commit we could not verify (e.g. commits/{sha}/pulls erroring
    # three times) would pass the gate. 0 files changed means "nothing shipped"
    # only if we successfully accounted for every commit (Bugbot, .github#109).
    if [ "${CHANGED_FILES:-1}" = "0" ] \
       && [ -z "$(echo "${UNATTRIB}" | tr -d ' ')" ]; then
      echo "No attributable items, no unattributable commits, and the range changes no files — ancestry-only promotion, nothing to gate."
      echo "::notice::FR gate PASSED (ancestry-only promotion: 0 files changed, every commit accounted for)."
      exit 0
    fi
    echo "No PR refs in commit subjects — falling back to gating the promotion PR (#$PROMOTION_PR) itself."
    NUMBERS="$PROMOTION_PR"
  fi

  BLOCKED=""
  MISSING=""
  UNREADABLE=""
  PASSED=""

  local num STATUS rc SR RR
  for num in $NUMBERS; do
    STATUS=""
    rc=0
    STATUS=$(resolve_status "$num") || rc=$?

    if [ "$rc" -eq 1 ]; then
      echo "  ⛔ #$num — Status unreadable after 5 attempts (API failure)"
      UNREADABLE="$UNREADABLE #$num"
      continue
    fi
    if [ "$rc" -eq 2 ] || [ -z "$STATUS" ]; then
      echo "  ⛔ #$num — not on the kanban board after 5 attempts"
      MISSING="$MISSING #$num"
      continue
    fi

    SR=$(rank "$STATUS")
    RR=$(rank "$REQUIRED")
    if { [ -n "$SR" ] && [ -n "$RR" ] && [ "$SR" -ge "$RR" ]; } || [ "$STATUS" = "$REQUIRED" ]; then
      if [ "$STATUS" = "$REQUIRED" ]; then
        echo "  ✅ #$num — Status='$REQUIRED'"
      else
        echo "  ✅ #$num — Status='$STATUS' (beyond '$REQUIRED')"
      fi
      PASSED="$PASSED #$num"
    else
      echo "  ❌ #$num — Status='$STATUS', required '$REQUIRED' or later"
      BLOCKED="$BLOCKED #$num($STATUS)"
    fi
  done

  echo ""

  # FAIL CLOSED. An item the gate could not evaluate is not evidence that review
  # happened, and until now it was treated as one: an unreadable or absent
  # Status hit `continue`, and if every item was unreadable the gate printed
  # "PASSED". A missing token, an expired token, a repo-level secret shadowing
  # the org one, or a transient 5xx all collapsed into a silent pass — the only
  # promotion gate disarming itself, with failure indistinguishable from
  # success.
  if [ -n "$BLOCKED" ] || [ -n "$MISSING" ] || [ -n "$UNREADABLE" ] || [ -n "${UNATTRIB// /}" ]; then
    if [ -n "$BLOCKED" ]; then
      echo "::error::FR gate FAILED. Items not yet at '$REQUIRED' (or later):$BLOCKED"
      echo ""
      echo "How to unblock:"
      echo "  1. Run /fr-pass on each PR once functional review passes on the previous env, OR"
      echo "  2. Drag each card to '$REQUIRED' on the engineer kanban."
    fi
    if [ -n "$MISSING" ]; then
      echo "::error::FR gate FAILED. Not on the kanban board:$MISSING"
      echo ""
      echo "These items were queried 5 times and never appeared. Either the PR"
      echo "was never added to project #$PROJECT_NUMBER, or its card was removed."
      echo "Add it to the board, then re-run this check."
    fi
    if [ -n "$UNREADABLE" ]; then
      echo "::error::FR gate FAILED. Status unreadable:$UNREADABLE"
      echo ""
      echo "The board could not be queried after 5 attempts. This is an"
      echo "infrastructure failure, not a review failure — this gate"
      echo "authenticates as the tracebloc-release-train App, so check the"
      echo "installation still covers this repo and still holds the"
      echo "organization Projects permission (backend#2036)."
    fi
    if [ -n "${UNATTRIB// /}" ]; then
      echo "::error::FR gate FAILED. Commits with no attributable PR:$UNATTRIB"
      echo ""
      echo "These commits are in the promotion range but carry no (#N) and did"
      echo "not arrive via a PR merge — a direct push, or a release branch edited"
      echo "after its last PR. The gate can only vouch for reviewed commits, so"
      echo "an unattributable commit rides in unevaluated (RFC-0008 D27-L1)."
      echo "Open a PR for the change, or if it is a legitimate release-mechanics"
      echo "commit, add the 'skip-fr-gate' label with a written reason."
    fi
    echo ""
    echo "Emergency override: add the 'skip-fr-gate' label to this PR, with a"
    echo "written reason in a comment."
    exit 1
  fi

  echo "✓ FR gate PASSED. All items are at '$REQUIRED' or later."
}

# ---------------------------------------------------------------------------
# frontier -- the train's resolver (RFC-0075 D6 part 2)
# ---------------------------------------------------------------------------
say() { printf '%s\n' "$*" >&2; }      # human log -> stderr; stdout is the key=value contract

unknown() {   # unknown <reason> -- print the refusal, emit the state, exit 3
  say "UNKNOWN — $*"
  say "A frontier that cannot be verified is not a frontier. Nothing was cut (fail closed)."
  echo "frontier_state=unknown"
  exit "$RC_UNKNOWN"
}

mode_frontier() {
  need REPO ORG PROJECT_NUMBER TARGET SOURCE
  REQUIRED="${REQUIRED:-Ready for prod}"
  REPO_NAME="${REPO#*/}"
  local RR TARGET_SHA SOURCE_SHA RANGE range_n

  RR=$(rank "$REQUIRED")
  [ -n "$RR" ] || unknown "REQUIRED='$REQUIRED' is not a Status this walk can rank."

  # A shallow checkout cannot show the whole range, and a range with a hole in
  # it walks fewer commits than the promotion would carry. Refuse rather than
  # cut on what happens to be present.
  if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
    unknown "this checkout is shallow; the walk needs the full history of both refs."
  fi
  TARGET_SHA=$(git rev-parse --verify --quiet "${TARGET}^{commit}") \
    || unknown "TARGET '$TARGET' does not resolve to a commit in this checkout."
  SOURCE_SHA=$(git rev-parse --verify --quiet "${SOURCE}^{commit}") \
    || unknown "SOURCE '$SOURCE' does not resolve to a commit in this checkout."

  WALK_DIR=$(mktemp -d)
  trap 'rm -rf "$WALK_DIR"' EXIT

  # Parents before children, the WHOLE range (header, point 1). Not --first-parent.
  RANGE=$(git rev-list --topo-order --reverse "$TARGET_SHA..$SOURCE_SHA")
  if [ -z "$RANGE" ]; then
    say "no commits to compare: $TARGET_SHA..$SOURCE_SHA is empty."
    echo "frontier_state=empty-range"
    echo "frontier=$TARGET_SHA"
    echo "target=$TARGET_SHA"
    echo "source=$SOURCE_SHA"
    echo "range_commits=0"
    echo "frontier_commits=0"
    echo "behind=0"
    echo "blocker="
    echo "blockers="
    echo "items_beyond_ready=0"
    exit 0
  fi
  range_n=$(printf '%s\n' "$RANGE" | grep -c . || true)

  # The gate's walk, over the whole range. Its BLOCKED lines go to stderr here.
  WALK_BASE="$TARGET_SHA" WALK_HEAD="$SOURCE_SHA" walk_attribution 1>&2

  # An API that never answered for a commit leaves that commit unverifiable in
  # BOTH directions -- it might carry an un-FR'd card, or none. Refuse.
  if [ -n "$(ls -A "$WALK_DIR/apifail")" ]; then
    unknown "commits/{sha}/pulls never answered for: $(ls "$WALK_DIR/apifail" | tr '\n' ' ')"
  fi

  # Resolve every distinct PR's Status once. rc 1 (API never answered) makes the
  # whole answer UNKNOWN; rc 2 / empty (no card) is a fact -- the item cannot
  # have passed FR -- and is recorded as a blocker under its own name.
  mkdir -p "$WALK_DIR/status"
  local num STATUS src
  for num in $(echo "$ATTRIBUTED" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -un); do
    STATUS=""
    src=0
    STATUS=$(resolve_status "$num") || src=$?
    if [ "$src" -eq 1 ]; then
      unknown "the board could not be read for #$num after 5 attempts (backend#2036: check the App's organization Projects permission)."
    fi
    if [ "$src" -eq 2 ] || [ -z "$STATUS" ]; then
      STATUS="not on the kanban board"
    fi
    printf '%s' "$STATUS" > "$WALK_DIR/status/$num"
  done

  # BAD commits: a non-merge commit with no merged PR (direct push), or any
  # carried PR below the required rank / off the board. A merge with no vouching
  # PR is not bad (header, point 5).
  : > "$WALK_DIR/bad"        # sha<TAB>reason, in topo order
  local sha attr reason SR pr_status
  while IFS= read -r sha; do
    [ -z "$sha" ] && continue
    attr=$(cat "$WALK_DIR/attr/$sha" 2>/dev/null || true)
    reason=""
    if [ -z "$attr" ]; then
      if ! git rev-parse --verify --quiet "${sha}^2" >/dev/null 2>&1; then
        reason="${sha}(no merged PR)"
      fi
    else
      for num in $attr; do
        pr_status=$(cat "$WALK_DIR/status/$num")
        SR=$(rank "$pr_status")
        if [ -n "$SR" ] && [ "$SR" -ge "$RR" ]; then
          continue
        fi
        [ "$pr_status" = "$REQUIRED" ] && continue
        reason="${reason:+$reason|}#${num}(${pr_status})"
      done
    fi
    [ -n "$reason" ] && printf '%s\t%s\n' "$sha" "$reason" >> "$WALK_DIR/bad"
  done <<<"$RANGE"

  # TAINTED = every bad commit and everything that descends from one, within
  # the range. A candidate passes iff it is not tainted.
  : > "$WALK_DIR/tainted"
  while IFS=$'\t' read -r sha reason; do
    [ -z "$sha" ] && continue
    printf '%s\n' "$sha" >> "$WALK_DIR/tainted"
    git rev-list --ancestry-path "${sha}..${SOURCE_SHA}" >> "$WALK_DIR/tainted"
  done < "$WALK_DIR/bad"
  sort -u "$WALK_DIR/tainted" -o "$WALK_DIR/tainted"

  # The frontier: the untainted, non-covered commit with the largest TARGET..C.
  local best best_n c_n
  best=""
  best_n=0
  while IFS= read -r sha; do
    [ -z "$sha" ] && continue
    [ -e "$WALK_DIR/covered/$sha" ] && continue          # inside a PR: never a cut (point 4)
    if [ -s "$WALK_DIR/tainted" ] && grep -qxF -- "$sha" "$WALK_DIR/tainted"; then continue; fi
    c_n=$(git rev-list --count "$TARGET_SHA..$sha")
    if [ "$c_n" -ge "$best_n" ]; then
      best="$sha"
      best_n="$c_n"
    fi
  done <<<"$RANGE"

  # Blockers, in topo order: the FIRST is the actionable one (#102 scope item 3
  # -- it names who needs to run FR next). Deduplicated by reason text.
  local blockers first_blocker beyond_ready
  # `|`-joined per commit above, one per line here, deduplicated, then "; "-joined:
  # a Status carries spaces ("FR on staging"), so space is not the separator.
  blockers=$(cut -f2 "$WALK_DIR/bad" | tr '|' '\n' | awk '!seen[$0]++' | paste -sd ';' - | sed 's/;/; /g')
  first_blocker="${blockers%%;*}"

  # How many items BEYOND the frontier are already at the required rank -- the
  # visibility number D6 asks `prepare` to print (they are FR'd, and pinned by
  # an older card).
  beyond_ready=0
  if [ -n "$best" ]; then
    beyond_ready=$(
      { git rev-list "$TARGET_SHA..$SOURCE_SHA"; git rev-list "$TARGET_SHA..$best"; } \
        | sort | uniq -u \
        | while IFS= read -r sha; do cat "$WALK_DIR/attr/$sha" 2>/dev/null; done \
        | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u \
        | while IFS= read -r num; do
            SR=$(rank "$(cat "$WALK_DIR/status/$num")")
            if [ -n "$SR" ] && [ "$SR" -ge "$RR" ]; then echo "$num"; fi
          done | grep -c . || true)
  else
    beyond_ready=$(
      cat "$WALK_DIR"/attr/* | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u \
        | while IFS= read -r num; do
            SR=$(rank "$(cat "$WALK_DIR/status/$num")")
            if [ -n "$SR" ] && [ "$SR" -ge "$RR" ]; then echo "$num"; fi
          done | grep -c . || true)
  fi

  if [ -z "$best" ]; then
    # Non-empty range, nothing passed. A different fact from "nothing to
    # promote", and printed as one (#102).
    say "frontier at $TARGET_SHA — nothing has passed FR yet: $range_n commit(s) in the range, none of them cuttable."
    say "first blocker: $first_blocker  (review oldest-first: it pins everything behind it)"
    echo "frontier_state=at-target"
    echo "frontier=$TARGET_SHA"
    echo "target=$TARGET_SHA"
    echo "source=$SOURCE_SHA"
    echo "range_commits=$range_n"
    echo "frontier_commits=0"
    echo "behind=$range_n"
    echo "blocker=$first_blocker"
    echo "blockers=$blockers"
    echo "items_beyond_ready=$beyond_ready"
    exit 0
  fi

  say "frontier $best — $best_n of $range_n commit(s) in the range are cuttable; $((range_n - best_n)) behind."
  if [ -n "$first_blocker" ]; then
    say "first blocker: $first_blocker  (review oldest-first: it pins everything behind it)"
  fi
  echo "frontier_state=advanced"
  echo "frontier=$best"
  echo "target=$TARGET_SHA"
  echo "source=$SOURCE_SHA"
  echo "range_commits=$range_n"
  echo "frontier_commits=$best_n"
  echo "behind=$((range_n - best_n))"
  echo "blocker=$first_blocker"
  echo "blockers=$blockers"
  echo "items_beyond_ready=$beyond_ready"
}

case "$MODE" in
  discover) mode_discover ;;
  verify)   mode_verify ;;
  frontier) mode_frontier ;;
  *)        usage ;;
esac
