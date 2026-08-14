#!/usr/bin/env bash
#
# git-reap selftest — the decision, exercised against real git repositories.
#
# git-reap deletes branches. Its entire safety argument is one sentence in its
# own header: "FAILS CLOSED … 'I could not tell' is never treated as 'it
# merged'." A claim like that in a comment is a claim that should be a machine
# check — where it isn't, it decays into a doc that teaches the bypass. Two of
# the three defects this suite was written for were the code contradicting that
# exact sentence.
#
# Every case builds a throwaway repo with real commits and a real squash-merge,
# and stubs `gh` on PATH so the merged-PR list can be made unreadable, truncated
# or empty on demand. Nothing here talks to GitHub.
#
# Run: bash scripts/tests/git-reap-selftest.sh
set -uo pipefail

REAP="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/git-reap"
[ -x "$REAP" ] || { echo "FATAL: $REAP is not executable"; exit 1; }

pass=0
fail=0

ok() { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
no() { printf '  FAIL  %s\n     %s\n' "$1" "$2"; fail=$((fail + 1)); }

# assert_out <name> <expected-substring> <output>
assert_out() {
  if grep -qF -- "$2" <<<"$3"; then ok "$1"; else no "$1" "expected to find: $2"; fi
}
assert_not_out() {
  if grep -qF -- "$2" <<<"$3"; then no "$1" "must NOT contain: $2"; else ok "$1"; fi
}

# make_repo <dir> — an "origin" with develop, plus a clone holding:
#   squashed  — content landed on develop as a DIFFERENT commit (the squash case)
#   unmerged  — real work that never landed
#   ancestral — a plain fast-forward merge, an ancestor of develop
# Each of squashed/unmerged has its upstream deleted, so both are [gone]: that
# is the whole point — [gone] alone cannot tell them apart.
make_repo() {
  local root="$1" origin="$1/origin" work="$1/work"
  mkdir -p "$origin"
  git init -q --bare --initial-branch=develop "$origin"

  git init -q --initial-branch=develop "$work"
  git -C "$work" config user.email t@t.io
  git -C "$work" config user.name t
  echo base >"$work/f"
  git -C "$work" add f
  git -C "$work" commit -qm base
  git -C "$work" remote add origin "$origin"
  git -C "$work" push -q -u origin develop

  # ancestral: merged by fast-forward, so it IS an ancestor of develop.
  git -C "$work" checkout -qb ancestral
  echo a >>"$work/f"
  git -C "$work" commit -qam a
  git -C "$work" push -q -u origin ancestral
  git -C "$work" checkout -q develop
  git -C "$work" merge -q --ff-only ancestral
  git -C "$work" push -q origin develop

  # squashed: its content is on develop under a different sha.
  git -C "$work" checkout -qb squashed
  echo s >"$work/g"
  git -C "$work" add g
  git -C "$work" commit -qm s
  git -C "$work" push -q -u origin squashed
  git -C "$work" checkout -q develop
  echo s >"$work/g"
  git -C "$work" add g
  git -C "$work" commit -qm "squash of squashed"
  git -C "$work" push -q origin develop

  # unmerged: real work, nowhere on develop.
  git -C "$work" checkout -qb unmerged
  echo u >"$work/h"
  git -C "$work" add h
  git -C "$work" commit -qm u
  git -C "$work" push -q -u origin unmerged

  git -C "$work" checkout -q develop
  git -C "$origin" branch -D ancestral >/dev/null 2>&1
  git -C "$origin" branch -D squashed >/dev/null 2>&1
  git -C "$origin" branch -D unmerged >/dev/null 2>&1
  git -C "$work" fetch -q --prune origin
  git -C "$origin" symbolic-ref HEAD refs/heads/develop
  git -C "$work" remote set-head origin develop >/dev/null 2>&1
}

# stub_gh <bindir> <mode> <work> — a fake `gh` on PATH. Rows are "name sha",
# which is what the real query returns and what the match requires.
#   ok        a readable list holding "squashed" at its CURRENT tip
#   stale-sha "squashed" is listed, but at some OTHER sha — a branch whose PR
#             merged and then got another commit, or a reused branch name
#   fail      `gh pr list` exits non-zero (network down, rate limit, bad token)
#   truncated exactly PR_LIMIT rows, none of them "squashed"
#   empty     a readable, complete, empty list
#   other-base
#             "squashed" merged, but into a SIBLING FEATURE branch. The real `gh`
#             filters by base server-side, so this row is only visible to a query
#             that forgot --base — which is precisely the bug being asserted.
#   no-remote-default
#             `gh repo view` fails, so the default branch cannot be confirmed and
#             a possibly-stale origin/HEAD must be announced as UNVERIFIED.
#
# The stub reads --base out of its own argv rather than ignoring it, because the
# behaviour under test IS whether git-reap passes it. A stub that discarded the
# flag would answer identically either way and the case below could not fail.
stub_gh() {
  local bin="$1" mode="$2" work="$3"
  local sha
  sha=$(git -C "$work" rev-parse squashed 2>/dev/null || echo 0000000000000000000000000000000000000000)
  mkdir -p "$bin"
  cat >"$bin/gh" <<STUB
#!/usr/bin/env bash
[ "\$1" = "auth" ] && exit 0
# The authority for the default branch. git-reap asks this because origin/HEAD is
# a clone-time cache that fetch never refreshes; the stub answers "develop", which
# is what make_repo actually builds.
if [ "\$1" = "repo" ] && [ "\$2" = "view" ]; then
  case "$mode" in
    no-remote-default) exit 1 ;;
    *) echo "develop"; exit 0 ;;
  esac
fi
if [ "\$1" = "pr" ] && [ "\$2" = "list" ]; then
  base=""
  while [ \$# -gt 0 ]; do
    if [ "\$1" = "--base" ]; then base="\$2"; fi
    shift
  done
  case "$mode" in
    fail) exit 1 ;;
    truncated) i=1; while [ \$i -le 1000 ]; do echo "filler-\$i deadbeef"; i=\$((i+1)); done; exit 0 ;;
    empty) exit 0 ;;
    stale-sha) echo "squashed 1111111111111111111111111111111111111111"; exit 0 ;;
    other-base) [ -n "\$base" ] || echo "squashed $sha"; exit 0 ;;
    *) echo "squashed $sha"; exit 0 ;;
  esac
fi
exit 0
STUB
  chmod +x "$bin/gh"
}

run_reap() {
  local root="$1" mode="$2"
  shift 2
  stub_gh "$root/bin" "$mode" "$root/work"
  (cd "$root/work" && PATH="$root/bin:$PATH" bash "$REAP" "$@" 2>&1)
}

echo "== git-reap selftest =="

# --- 1. The happy path, so the rest of the suite is known not to be vacuous ---
T=$(mktemp -d)
make_repo "$T"
out=$(run_reap "$T" ok)
assert_out "a squash-merged branch is reaped" "squashed" "$out"
assert_out "  …on PR evidence" "squash-merged (MERGED PR for this exact head)" "$out"
assert_out "an ancestor of develop is reaped" "ancestor of develop" "$out"
assert_out "unmerged work is KEPT" "KEEP     unmerged" "$out"
assert_not_out "unmerged work is never skipped as unprovable" "SKIP     unmerged" "$out"
rm -rf "$T"

# --- 2. An unreadable PR list is NOT an empty one -------------------------
# The defect: `|| prs=""` made a failed query indistinguishable from "no merged
# PRs", so every branch was reported KEEP "no merge evidence" — a proven
# negative asserted from a read that never happened.
T=$(mktemp -d)
make_repo "$T"
out=$(run_reap "$T" fail)
assert_out "a failed PR read SKIPs rather than claiming no evidence" \
  "SKIP     squashed" "$out"
assert_out "  …and says why" "could not be read" "$out"
assert_not_out "a failed PR read never asserts the negative" \
  "KEEP     squashed — no merge evidence" "$out"
assert_out "the ancestor case still decides — it needs no PR list" \
  "ancestor of develop" "$out"
rm -rf "$T"

# --- 3. A truncated PR list cannot prove absence --------------------------
# gh truncates at --limit silently. The old backlog this tool exists to clear
# is precisely what falls out of a newest-first 1000 window.
T=$(mktemp -d)
make_repo "$T"
out=$(run_reap "$T" truncated)
assert_out "a capped PR list SKIPs rather than proving absence" \
  "SKIP     squashed" "$out"
assert_out "  …and names the cap" "hit the --limit 1000 cap" "$out"
rm -rf "$T"

# --- 4. A genuinely empty list is still evidence --------------------------
# The fail-closed fix must not swallow the real answer: a readable, complete,
# empty list DOES prove no merged PR exists.
T=$(mktemp -d)
make_repo "$T"
out=$(run_reap "$T" empty)
assert_out "an empty-but-readable list still yields a verdict" \
  "KEEP     squashed" "$out"
assert_not_out "  …not a SKIP" "SKIP     squashed" "$out"
rm -rf "$T"

# --- 5. Nothing is deleted in the default dry run -------------------------
T=$(mktemp -d)
make_repo "$T"
run_reap "$T" ok >/dev/null
if git -C "$T/work" rev-parse --verify --quiet squashed >/dev/null; then
  ok "the default run deletes nothing"
else
  no "the default run deletes nothing" "squashed was deleted without --delete"
fi
out=$(run_reap "$T" ok --delete)
if git -C "$T/work" rev-parse --verify --quiet squashed >/dev/null; then
  no "--delete removes a proven-merged branch" "squashed survived --delete"
else
  ok "--delete removes a proven-merged branch"
fi
if git -C "$T/work" rev-parse --verify --quiet unmerged >/dev/null; then
  ok "--delete leaves unproven work alone"
else
  no "--delete leaves unproven work alone" "unmerged was destroyed"
fi
rm -rf "$T"

# --- 6. A branch checked out in a linked worktree is pinned ---------------
# One of the `printf | grep -q` sites. Read as a miss, this deletes the branch
# a colleague is standing on.
T=$(mktemp -d)
make_repo "$T"
git -C "$T/work" worktree add -q "$T/wt" squashed 2>/dev/null
out=$(run_reap "$T" ok)
assert_out "a worktree-pinned branch is kept" "checked out in a worktree" "$out"
rm -rf "$T"

# --- 7. Integration branches are never candidates ------------------------
T=$(mktemp -d)
make_repo "$T"
git -C "$T/work" branch main develop 2>/dev/null
git -C "$T/work" branch staging develop 2>/dev/null
# Stand somewhere else. Checked out on develop, the worktree pin protects it and
# this case proves nothing about PROTECTED_RE for the one branch that matters
# most — it passed under a mutated regex for exactly that reason.
git -C "$T/work" checkout -q unmerged
out=$(run_reap "$T" ok --all)
for protected in develop staging main; do
  # The needle is the DRY-RUN verb. The first version of this case looked for
  # "deleted", which a dry run never prints — so a broken PROTECTED_RE emitting
  # "would go main (sha) — …" left the suite green, and the guard protecting
  # integration branches from `--delete --all` could not fail (Bugbot). Assert
  # the branch is not named as a candidate in either verb.
  assert_not_out "$protected is not a dry-run candidate" "would go $protected " "$out"
  assert_not_out "$protected is not a delete candidate" "deleted  $protected " "$out"
  # Not merely un-reaped — never CONSIDERED. A protected branch reaching any
  # verdict line means PROTECTED_RE let it into the loop.
  assert_not_out "$protected is never considered" " $protected " "$out"
done
rm -rf "$T"

# --- 8. A merged NAME is not a merged BRANCH ------------------------------
# The evidence matched on headRefName alone, so a branch that merged and then
# took another commit — or a reused branch name — still counted as merged. With
# --delete that force-removes work that never landed, which is the exact
# opposite of the header's promise, and `git branch -D` issues no warning.
T=$(mktemp -d)
make_repo "$T"
out=$(run_reap "$T" stale-sha)
assert_out "a merged name at a different sha is not evidence" \
  "KEEP     squashed" "$out"
assert_not_out "  …and is certainly not reaped" "would go squashed" "$out"
run_reap "$T" stale-sha --delete >/dev/null
if git -C "$T/work" rev-parse --verify --quiet squashed >/dev/null; then
  ok "--delete leaves a name-only match alone"
else
  no "--delete leaves a name-only match alone" "unlanded work was force-deleted"
fi
rm -rf "$T"

# --- 9. A PR merged into a SIBLING BRANCH is not merged HERE ---------------
# `gh pr list --state merged` is unfiltered by base, so a branch squash-merged
# into another feature branch answered "yes, merged" to a question about
# develop. `--is-ancestor` had already said it was not on develop; the PR row
# overrode that, and --delete force-removed commits that never landed. The org
# has a `sibling-merge` closure label, so this is a path that happens.
#
# The stub only emits the row when --base is ABSENT, mirroring the real gh's
# server-side filter — so this case reddens if the flag is ever dropped again.
T=$(mktemp -d)
make_repo "$T"
out=$(run_reap "$T" other-base)
assert_out "a PR merged into a sibling branch is not evidence for develop" \
  "KEEP     squashed" "$out"
assert_not_out "  …and is certainly not reaped" "would go squashed" "$out"
run_reap "$T" other-base --delete >/dev/null
if git -C "$T/work" rev-parse --verify --quiet squashed >/dev/null; then
  ok "--delete leaves a sibling-merged branch alone"
else
  no "--delete leaves a sibling-merged branch alone" "unlanded work was force-deleted"
fi
rm -rf "$T"

# --- 10. A stale origin/HEAD is corrected, not trusted ---------------------
# origin/HEAD is a clone-time cache and `git fetch` never refreshes it. Stale, it
# poisons every verdict: --is-ancestor measures against the wrong branch, and the
# --base filter reads a COMPLETE merged-PR list for the wrong base and calls that
# a proven negative. Measured on 9 of 19 clones in this org, all still on `main`.
T=$(mktemp -d)
make_repo "$T"
# A REAL main on the origin, then point origin/HEAD at it while develop is still
# the true default — the exact drift these clones have. It has to resolve: a
# dangling symref just makes `git symbolic-ref` return empty and the fallback
# loop picks develop anyway, so the case would pass without testing anything.
git -C "$T/origin" branch main develop
git -C "$T/work" fetch origin --quiet
git -C "$T/work" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main
out=$(run_reap "$T" ok --all)
assert_out "a stale origin/HEAD is reported rather than trusted" \
  "the remote says develop" "$out"
# With the default corrected back to develop, the squash evidence resolves again.
assert_out "  …and the corrected default restores the merge evidence" \
  "squash-merged" "$out"
rm -rf "$T"

# --- 11. An unconfirmable default branch says so --------------------------
# gh cannot answer, so origin/HEAD might be stale and nothing can prove otherwise.
# Silence here would be the fail-open the header forbids.
T=$(mktemp -d)
make_repo "$T"
out=$(run_reap "$T" no-remote-default --all)
assert_out "an unconfirmable default branch is announced as UNVERIFIED" \
  "UNVERIFIED" "$out"
rm -rf "$T"

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
