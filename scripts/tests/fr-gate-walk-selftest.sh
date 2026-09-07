#!/usr/bin/env bash
#
# fr-gate-walk selftest — the gate's walk and the frontier, exercised against
# real git histories (backend#3323, RFC-0075 D6).
#
# WHAT IS UNDER TEST. scripts/fr-gate-walk.sh carries the attribution walk that
# used to sit inline in fr-gate.yml, plus a `frontier` mode the release train
# will call. Three claims, and each has cases here that redden when it breaks:
#
#   1. `discover` + `verify` reproduce the gate's VERDICT: the same PR set, the
#      same unattributable commits, the same pass/fail text and exit code, on
#      every shape the inline blocks were written for -- squash merges, a true
#      PR-merge vouching for its branch, the train's transparent release-train/*
#      PRs, a direct push, a sync merge that must NOT launder a base push
#      (Bugbot #72/#73), the ancestry-only range (.github#109), and every
#      fail-closed arm (API down, board unreadable, card missing).
#   2. `frontier` cuts CARD-granular: an inner develop commit of a hop is the
#      frontier when the un-FR'd card is later in the same hop; a commit INSIDE
#      a PR is never a cut; the three states (advanced / at-target /
#      empty-range) are distinct facts with distinct text.
#   3. Cannot-tell is UNKNOWN (exit 3), never a frontier.
#
# EXPECTATIONS ARE WRITTEN HERE, BY NAME, not derived from the script (rule 9):
# "the frontier is d4" is a statement about the fixture's shape, computed by
# hand in the comment beside each case.
#
# HOW IT WORKS. Every case builds (or reuses) a throwaway repo -- an "origin"
# with main/develop/staging and real merge commits -- and stubs `gh` on PATH.
# The stub answers commits/{sha}/pulls from per-sha fixture files,
# compare/{a}...{b} from a knob, and the ProjectV2 graphql from per-PR status
# files; an UNSTUBBED call fails the case, because a stub returning empty would
# reproduce the fail-open shape under test. FR_WALK_RETRY_UNIT=0 so the gate's
# retry COUNTS run at full count without the sleeps.
#
# Run: bash scripts/tests/fr-gate-walk-selftest.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$ROOT/scripts/fr-gate-walk.sh"
[ -x "$SCRIPT" ] || { echo "FATAL: $SCRIPT is not executable"; exit 1; }
command -v jq >/dev/null || { echo "FATAL: jq is required"; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; mkdir -p "$BIN"
FIX="$WORK/fix"; mkdir -p "$FIX/pulls" "$FIX/status"

pass=0
fail=0
CUR=""
OUT=""; ERR=""; RC=0
ok() { printf '  ok    %s\n' "$CUR — $1"; pass=$((pass + 1)); }
no() { printf '  FAIL: %s\n     %s\n' "$CUR — $1" "$2"; fail=$((fail + 1)); }

# ---------------------------------------------------------------------------
# The gh stub. Models the three reads the script makes and nothing else.
# ---------------------------------------------------------------------------
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
CALL="$*"
printf '%s\n' "$CALL" >> "$FRW_CALLLOG"
case "$CALL" in
  "api repos/"*"/commits/"*"/pulls")
    sha="${CALL#*/commits/}"; sha="${sha%/pulls}"
    [ -n "${D_PULLS_FAIL_ALL:-}" ] && exit 1
    case " ${D_PULLS_FAIL:-} " in *" $sha "*) exit 1 ;; esac
    if [ -f "$FRW_FIX/pulls/$sha" ]; then cat "$FRW_FIX/pulls/$sha"; else echo "[]"; fi
    ;;
  "api repos/"*"/compare/"*)
    [ -n "${D_COMPARE_FAIL:-}" ] && exit 1
    printf '%s\n' "${D_FILES:-1}"
    ;;
  "api graphql "*)
    [ -n "${D_GRAPHQL_FAIL:-}" ] && exit 1
    num=""; prev=""
    for a in "$@"; do
      if [ "$prev" = "-F" ]; then case "$a" in num=*) num="${a#num=}" ;; esac; fi
      prev="$a"
    done
    if [ -f "$FRW_FIX/status/$num" ]; then
      st=$(cat "$FRW_FIX/status/$num")
      printf '{"data":{"repository":{"pullRequest":{"projectItems":{"nodes":[{"project":{"number":%s},"fieldValueByName":{"name":"%s"}}]}}}}}\n' "${D_PROJECT:-2}" "$st"
    else
      printf '{"data":{"repository":{"pullRequest":{"projectItems":{"nodes":[]}}}}}\n'
    fi
    ;;
  *)
    printf '%s\n' "$CALL" >> "$FRW_UNSTUBBED"
    exit 99
    ;;
esac
STUB
chmod +x "$BIN/gh"

# pulls_fixture <sha> <pr-number> <merge_commit_sha> <head-ref> [<pr2> <mcs2> <ref2>]
# Writes the commits/{sha}/pulls document for one commit: every merged PR whose
# branch contains it, which is what the real API returns.
pulls_fixture() {
  local sha="$1"; shift
  local doc="[" first=1
  while [ "$#" -ge 3 ]; do
    [ "$first" -eq 1 ] || doc="$doc,"
    first=0
    doc="$doc{\"number\":$1,\"merged_at\":\"2026-09-01T00:00:00Z\",\"merge_commit_sha\":\"$2\",\"head\":{\"ref\":\"$3\"}}"
    shift 3
  done
  printf '%s]\n' "$doc" > "$FIX/pulls/$sha"
}

set_status()   { printf '%s' "$2" > "$FIX/status/$1"; }
clear_status() { rm -f "$FIX/status/"*; }
all_ready()    { clear_status; for n in 11 12 13 14 16; do set_status "$n" "Ready for prod"; done; }

# ---------------------------------------------------------------------------
# Fixture repos. Commits are named so the expectations below can be read.
# ---------------------------------------------------------------------------
gitc() { git -C "$1" -c user.email=t@t.io -c user.name=t "${@:2}"; }
commit() {   # commit <repo> <file> <msg> -> echoes sha
  echo "$3" > "$1/$2"
  gitc "$1" add "$2" >/dev/null
  gitc "$1" commit -q -m "$3" >/dev/null
  gitc "$1" rev-parse HEAD
}

# make_repo <dir> [push] -- the standard shape:
#
#   main:     m0
#   develop:  m0 -> d1(#11) -> d2(#12) -> M3(merge #13: f1,f2 off d1) -> d4(#14) [-> p5 push] -> d6(#16)
#   staging:  m0 -> S1 = merge(mirror @ d2)  -> S2 = merge(mirror @ d6)
#
# Squash commits ARE their PR's merge_commit_sha; M3 is a true PR-merge; S1/S2
# are the train's mirror merges (their PRs have head release-train/*). Every
# inner commit also lists the train PR that carried it, as the real API does.
make_repo() {
  local r="$1" with_push="${2:-}"
  rm -rf "$r"; mkdir -p "$r"
  gitc "$r" init -q --initial-branch=main
  M0=$(commit "$r" f "m0")
  gitc "$r" checkout -q -b develop
  D1=$(commit "$r" a "d1 (#11)")
  D2=$(commit "$r" b "d2 (#12)")
  gitc "$r" checkout -q -b feature13 "$D1"
  F1=$(commit "$r" c "f1")
  F2=$(commit "$r" d "f2")
  gitc "$r" checkout -q develop
  gitc "$r" merge -q --no-ff -m "Merge pull request #13" feature13
  M3=$(gitc "$r" rev-parse HEAD)
  D4=$(commit "$r" e "d4 (#14)")
  P5=""
  if [ -n "$with_push" ]; then P5=$(commit "$r" p "p5 direct push"); fi
  D6=$(commit "$r" g "d6 (#16)")
  gitc "$r" checkout -q -b staging "$M0"
  gitc "$r" merge -q --no-ff -m "Merge release-train/hop-1" "$D2"
  S1=$(gitc "$r" rev-parse HEAD)
  gitc "$r" merge -q --no-ff -m "Merge release-train/hop-2" "$D6"
  S2=$(gitc "$r" rev-parse HEAD)
  gitc "$r" checkout -q develop

  rm -f "$FIX/pulls/"*
  pulls_fixture "$D1" 11 "$D1" feat/11   901 "$S1" release-train/hop-1
  pulls_fixture "$D2" 12 "$D2" feat/12   901 "$S1" release-train/hop-1
  pulls_fixture "$F1" 13 "$M3" feat/13   902 "$S2" release-train/hop-2
  pulls_fixture "$F2" 13 "$M3" feat/13   902 "$S2" release-train/hop-2
  pulls_fixture "$M3" 13 "$M3" feat/13   902 "$S2" release-train/hop-2
  pulls_fixture "$D4" 14 "$D4" feat/14   902 "$S2" release-train/hop-2
  pulls_fixture "$D6" 16 "$D6" feat/16   902 "$S2" release-train/hop-2
  pulls_fixture "$S1" 901 "$S1" release-train/hop-1
  pulls_fixture "$S2" 902 "$S2" release-train/hop-2
  # The direct push rode the train, so the ONLY PR containing it is the train's.
  [ -n "$P5" ] && pulls_fixture "$P5" 902 "$S2" release-train/hop-2
  return 0
}

# make_sync_repo <dir> -- the Bugbot #72/#73 shape:
#
#   develop:  m0 -> d1(#11) -> p(direct push) -> M7(merge #17)
#   feature17 (off d1): g1 -> X = "Merge branch develop into feature17" (sync merge; parents g1, p)
#   staging:  m0 -> S = merge(mirror @ M7)
#
# commits/pulls for X lists #17 (its branch contains X) but X is NOT #17's
# merge_commit_sha, so X must not vouch; p is on M7's mainline side, so M7
# must not cover it. Either failure launders p into "attributed".
make_sync_repo() {
  local r="$1"
  rm -rf "$r"; mkdir -p "$r"
  gitc "$r" init -q --initial-branch=main
  M0=$(commit "$r" f "m0")
  gitc "$r" checkout -q -b develop
  D1=$(commit "$r" a "d1 (#11)")
  gitc "$r" checkout -q -b feature17
  G1=$(commit "$r" c "g1")
  gitc "$r" checkout -q develop
  P=$(commit "$r" p "p direct push")
  gitc "$r" checkout -q feature17
  gitc "$r" merge -q --no-ff -m "Merge branch 'develop' into feature17" develop
  X=$(gitc "$r" rev-parse HEAD)
  gitc "$r" checkout -q develop
  gitc "$r" merge -q --no-ff -m "Merge pull request #17" feature17
  M7=$(gitc "$r" rev-parse HEAD)
  gitc "$r" checkout -q -b staging "$M0"
  gitc "$r" merge -q --no-ff -m "Merge release-train/hop-1" "$M7"
  S=$(gitc "$r" rev-parse HEAD)
  gitc "$r" checkout -q develop

  rm -f "$FIX/pulls/"*
  pulls_fixture "$D1" 11 "$D1" feat/11   901 "$S" release-train/hop-1
  pulls_fixture "$G1" 17 "$M7" feat/17   901 "$S" release-train/hop-1
  pulls_fixture "$X"  17 "$M7" feat/17   901 "$S" release-train/hop-1
  pulls_fixture "$M7" 17 "$M7" feat/17   901 "$S" release-train/hop-1
  pulls_fixture "$P"  901 "$S" release-train/hop-1
  pulls_fixture "$S"  901 "$S" release-train/hop-1
}

# ---------------------------------------------------------------------------
# Runners. `env -i` so nothing but the stub and the case knobs reach the script.
# ---------------------------------------------------------------------------
BASE_ENV=(PATH="$BIN:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin" HOME="$WORK"
          GH_TOKEN=tok FR_WALK_RETRY_UNIT=0
          FRW_FIX="$FIX" FRW_CALLLOG="$WORK/calls.log" FRW_UNSTUBBED="$WORK/unstubbed.log")

reset_logs() { : > "$WORK/calls.log"; : > "$WORK/unstubbed.log"; }
check_unstubbed() {
  if [ -s "$WORK/unstubbed.log" ]; then
    no "no unstubbed gh call" "$(cat "$WORK/unstubbed.log")"
  fi
}

# run_discover <repo> <base-sha> <head-sha> [KEY=VAL ...]
run_discover() {
  local r="$1" base="$2" head="$3"; shift 3
  reset_logs
  ERR=""
  : > "$WORK/gh_output"
  OUT=$(cd "$r" && env -i "${BASE_ENV[@]}" REPO=tracebloc/fixture REPO_FULL=tracebloc/fixture \
          BASE_SHA="$base" HEAD_SHA="$head" GITHUB_OUTPUT="$WORK/gh_output" "$@" \
          bash "$SCRIPT" discover 2>&1)
  RC=$?
  PRS_OUT=$(sed -n 's/^prs=//p' "$WORK/gh_output")
  UNATTRIB_OUT=$(sed -n 's/^unattributed=//p' "$WORK/gh_output")
  FILES_OUT=$(sed -n 's/^changed_files=//p' "$WORK/gh_output")
  check_unstubbed
}

# run_verify <required> <promotion-pr> [KEY=VAL ...]  (uses the last discover's outputs)
run_verify() {
  local required="$1" ppr="$2"; shift 2
  reset_logs
  ERR=""
  OUT=$(env -i "${BASE_ENV[@]}" ORG=tracebloc PROJECT_NUMBER=2 REPO_FULL=tracebloc/fixture \
          REQUIRED="$required" PRS="$PRS_OUT" UNATTRIB="$UNATTRIB_OUT" PROMOTION_PR="$ppr" \
          CHANGED_FILES="$FILES_OUT" "$@" \
          bash "$SCRIPT" verify 2>&1)
  RC=$?
  check_unstubbed
}

# run_frontier <repo> <target> <source> [KEY=VAL ...]
run_frontier() {
  local r="$1" target="$2" source="$3"; shift 3
  reset_logs
  OUT=$(cd "$r" && env -i "${BASE_ENV[@]}" REPO=tracebloc/fixture ORG=tracebloc PROJECT_NUMBER=2 \
          TARGET="$target" SOURCE="$source" "$@" \
          bash "$SCRIPT" frontier 2>"$WORK/frontier.err")
  RC=$?
  ERR=$(cat "$WORK/frontier.err")
  check_unstubbed
}

kv() { sed -n "s/^$1=//p" <<<"$OUT"; }

expect_rc()  { [ "$RC" = "$1" ] && ok "exit $1" || no "exit $1" "got exit $RC; output:
$OUT
$ERR"; }
expect()     { case "$OUT$ERR" in *"$1"*) ok "says: $1" ;; *) no "says: $1" "output was:
$OUT
$ERR" ;; esac; }
expect_not() { case "$OUT$ERR" in *"$1"*) no "never says: $1" "but it did" ;; *) ok "never says: $1" ;; esac; }
expect_kv()  { local got; got=$(kv "$1"); [ "$got" = "$2" ] && ok "$1=$2" || no "$1=$2" "got $1='$got'"; }
expect_eq()  { [ "$2" = "$3" ] && ok "$1" || no "$1" "expected '$3', got '$2'"; }

# ===========================================================================
# GATE MODE — discover + verify reproduce the inline verdict
# ===========================================================================
R="$WORK/std"; make_repo "$R"

# G1 all items at the required rank: every cargo PR attributed, the train's own
# PRs (901/902) NOT in the item list, nothing unattributed, PASS.
CUR="G1 prod promotion, every item Ready for prod"
all_ready; run_discover "$R" "$M0" "$S2"
expect_rc 0
expect_eq "prs is exactly the five cargo PRs (train PRs 901/902 filtered)" "$PRS_OUT" "11 12 13 14 16 "
expect_eq "no unattributable commits" "$(echo "$UNATTRIB_OUT" | tr -d ' ')" ""
expect "Range contributes 1 file(s)"
run_verify "Ready for prod" 99
expect_rc 0
expect "✓ FR gate PASSED. All items are at 'Ready for prod' or later."

# G2 one item still at FR on staging: BLOCKED names it with its Status.
CUR="G2 one item at FR on staging blocks"
all_ready; set_status 16 "FR on staging"
run_discover "$R" "$M0" "$S2"; run_verify "Ready for prod" 99
expect_rc 1
expect "::error::FR gate FAILED. Items not yet at 'Ready for prod' (or later): #16(FR on staging)"
expect "❌ #16 — Status='FR on staging', required 'Ready for prod' or later"

# G3 items BEYOND the rank pass: Prod and Done both satisfy Ready for prod
# (RFC-1405 D8, backend#1411).
CUR="G3 Prod and Done are beyond Ready for prod"
all_ready; set_status 11 "Prod"; set_status 12 "Done"
run_discover "$R" "$M0" "$S2"; run_verify "Ready for prod" 99
expect_rc 0
expect "✅ #11 — Status='Prod' (beyond 'Ready for prod')"
expect "✅ #12 — Status='Done' (beyond 'Ready for prod')"

# G3b a Status the rank table does not know falls to strict equality and BLOCKS.
CUR="G3b an unknown Status blocks"
all_ready; set_status 14 "Somewhere new"
run_discover "$R" "$M0" "$S2"; run_verify "Ready for prod" 99
expect_rc 1
expect "#14(Somewhere new)"

# G4 direct push (its only PR is the train's): unattributable, gate FAILS.
CUR="G4 a direct push that rode the train is unattributable"
RP="$WORK/push"; make_repo "$RP" push
all_ready; run_discover "$RP" "$M0" "$S2"
expect_eq "prs unchanged by the push" "$PRS_OUT" "11 12 13 14 16 "
case " $UNATTRIB_OUT " in *" $P5 "*) ok "unattributed names p5" ;; *) no "unattributed names p5" "got '$UNATTRIB_OUT'" ;; esac
expect "Unattributable commits (introduced by no merged PR): $P5"
run_verify "Ready for prod" 99
expect_rc 1
expect "::error::FR gate FAILED. Commits with no attributable PR: $P5"

# G5 the commits/pulls API never answers for one commit: BLOCKED, fail closed.
CUR="G5 commits/pulls unanswerable -> BLOCKED, failing closed"
make_repo "$R"; all_ready
run_discover "$R" "$M0" "$S2" D_PULLS_FAIL="$D4"
expect "BLOCKED $D4 -- could not verify via API after 3 attempts (failing closed)"
expect_eq "the other four PRs are still attributed" "$PRS_OUT" "11 12 13 16 "
run_verify "Ready for prod" 99
expect_rc 1
expect "Commits with no attributable PR: $D4"

# G5b ... and for a MERGE commit, the merge-specific message.
CUR="G5b unanswerable merge commit"
run_discover "$R" "$M0" "$S2" D_PULLS_FAIL="$M3"
expect "BLOCKED $M3 -- merge commit unverifiable via API after 3 attempts (failing closed)"

# G6 ancestry-only: develop tip -> staging tip contains only the two mirror
# merges, 0 files changed -> nothing to gate, PASS (.github#109 shape).
CUR="G6 ancestry-only promotion passes"
all_ready; run_discover "$R" "$D6" "$S2" D_FILES=0
expect_eq "no cargo PRs in an ancestry-only range" "$(echo "$PRS_OUT" | tr -d ' ')" ""
expect_eq "changed_files=0" "$FILES_OUT" "0"
run_verify "Ready for prod" 99
expect_rc 0
expect "::notice::FR gate PASSED (ancestry-only promotion: 0 files changed, every commit accounted for)."

# G6b ancestry-only BUT one commit could not be verified: must NOT pass
# (Bugbot, .github#109 -- 0 files means nothing shipped only if every commit
# was accounted for).
CUR="G6b ancestry-only with an unverified commit fails closed"
run_discover "$R" "$D6" "$S2" D_FILES=0 D_PULLS_FAIL="$S1"
run_verify "Ready for prod" 99
expect_rc 1
expect_not "ancestry-only promotion"
expect "Commits with no attributable PR: $S1"

# G6c the compare API is down: an uncounted range is NOT empty -> fall back to
# gating the promotion PR itself, which has no card -> FAIL, naming #99.
CUR="G6c uncounted range falls back to gating the promotion PR"
run_discover "$R" "$D6" "$S2" D_COMPARE_FAIL=1
expect "::warning::could not count changed files for this range - treating it as non-empty (fail closed)."
expect_eq "changed_files=-1" "$FILES_OUT" "-1"
run_verify "Ready for prod" 99
expect_rc 1
expect "falling back to gating the promotion PR (#99) itself."
expect "::error::FR gate FAILED. Not on the kanban board: #99"

# G7 the fallback PASSES when the promotion PR's own card is at the rank.
CUR="G7 fallback passes on the promotion PR's own card"
set_status 99 "Ready for prod"
run_verify "Ready for prod" 99
expect_rc 0
expect "✅ #99 — Status='Ready for prod'"

# G8 the board is unreadable: every item UNREADABLE, FAIL, infrastructure text.
CUR="G8 unreadable board fails closed"
all_ready; run_discover "$R" "$M0" "$S2"
run_verify "Ready for prod" 99 D_GRAPHQL_FAIL=1
expect_rc 1
expect "::error::FR gate FAILED. Status unreadable: #11 #12 #13 #14 #16"
expect "organization Projects permission (backend#2036)"

# G9 a card on ANOTHER project is not this board's card: MISSING.
CUR="G9 a card on a different project counts as missing"
all_ready; run_verify "Ready for prod" 99 D_PROJECT=9
expect_rc 1
expect "::error::FR gate FAILED. Not on the kanban board: #11 #12 #13 #14 #16"
expect "⛔ #11 — not on the kanban board after 5 attempts"

# G10 the sync-merge shape (Bugbot #72/#73): p is a base push that a feature
# branch merged in; X lists #17 without being its merge_commit_sha. p must stay
# unattributable -- neither X vouching nor M7 covering may launder it.
CUR="G10 a base push is not laundered through a sync merge (Bugbot #72/#73)"
RS="$WORK/sync"; make_sync_repo "$RS"
clear_status; set_status 11 "Ready for prod"; set_status 17 "Ready for prod"
run_discover "$RS" "$M0" "$S"
expect_eq "prs = the two real PRs" "$PRS_OUT" "11 17 "
case " $UNATTRIB_OUT " in *" $P "*) ok "the base push p is unattributable" ;; *) no "the base push p is unattributable" "got '$UNATTRIB_OUT'" ;; esac
case " $UNATTRIB_OUT " in *" $G1 "*) no "g1 (inside #17) is NOT unattributable" "got '$UNATTRIB_OUT'" ;; *) ok "g1 (inside #17) is covered by M7" ;; esac
run_verify "Ready for prod" 99
expect_rc 1
expect "Commits with no attributable PR: $P"

# G11 staging gate: On dev is the requirement, and an item at Code review blocks.
CUR="G11 staging target requires On dev"
make_repo "$R"; all_ready; set_status 14 "Code review"
run_discover "$R" "$M0" "$S2"; run_verify "On dev" 99
expect_rc 1
expect "Items not yet at 'On dev' (or later): #14(Code review)"

# ===========================================================================
# FRONTIER MODE
# ===========================================================================
# The standard repo's range main..staging, topo order (parents first):
#   d1 d2 S1 f1 f2 M3 d4 d6 S2          = 9 commits
# Candidates exclude f1/f2 (inside #13). |main..C|: d1=1 d2=2 S1=3 M3=5 d4=6 d6=7 S2=9

# F4 everything passed FR -> the frontier is the source tip itself.
CUR="F4 all items ready -> frontier is staging's tip"
all_ready; run_frontier "$R" main staging
expect_rc 0
expect_kv frontier_state advanced
expect_kv frontier "$S2"
expect_kv range_commits 9
expect_kv frontier_commits 9
expect_kv behind 0
expect_kv blocker ""
expect_kv items_beyond_ready 0

# F2 THE CARD-GRANULAR WIN. #16 (d6, last in hop 2) is un-FR'd; #14 (d4, same
# hop, older) has passed. A first-parent walk would cut at S1 (hop 1 only); the
# real frontier is d4 -- an inner develop commit of hop 2.
CUR="F2 an un-FR'd card pins only what is behind it (frontier = d4, inner commit)"
all_ready; set_status 16 "FR on staging"
run_frontier "$R" main staging
expect_rc 0
expect_kv frontier_state advanced
expect_kv frontier "$D4"
expect_kv frontier_commits 6
expect_kv behind 3
expect_kv blocker "#16(FR on staging)"
expect_kv blockers "#16(FR on staging)"
expect "first blocker: #16(FR on staging)"
# The fixture really has the shape the claim needs: d4 is NOT on staging's
# first-parent chain, so this case can only pass card-granular.
if git -C "$R" rev-list --first-parent main..staging | grep -qxF "$D4"; then
  no "fixture: d4 is off staging's first-parent chain" "d4 is first-parent; the case proves nothing"
else
  ok "fixture: d4 is off staging's first-parent chain"
fi

# F3 the un-FR'd card is a merged PR (#13 = M3 with f1,f2): everything from M3
# on is tainted; the frontier is S1, hop 1's mirror merge.
CUR="F3 an un-FR'd PR-merge pins its branch and everything after (frontier = S1)"
all_ready; set_status 13 "FR on staging"
run_frontier "$R" main staging
expect_kv frontier "$S1"
expect_kv frontier_commits 3
expect_kv behind 6
expect_kv blocker "#13(FR on staging)"
# #14 and #16 are FR'd but sit beyond the frontier: the visibility number.
expect_kv items_beyond_ready 2

# F3b two blockers: both named, oldest first, "; "-joined (a Status has spaces,
# so space cannot be the separator), and `blocker` is the first alone.
CUR="F3b several blockers are listed oldest-first"
set_status 16 "FR on staging"
run_frontier "$R" main staging
expect_kv frontier "$S1"
expect_kv blocker "#13(FR on staging)"
expect_kv blockers "#13(FR on staging); #16(FR on staging)"
expect_kv items_beyond_ready 1

# F12 NO CUT INSIDE A PR. #12 (d2) is un-FR'd. f1/f2 branched off d1 so they do
# not descend from d2 and would PASS as candidates with |main..f2| = 3 > |main..d1|
# = 1 -- but they are inside #13, and a cut at f2 ships half a PR. Frontier = d1.
CUR="F12 a commit inside a PR is never the frontier (frontier = d1, not f2)"
all_ready; set_status 12 "FR on staging"
run_frontier "$R" main staging
expect_kv frontier_state advanced
expect_kv frontier "$D1"
expect_kv frontier_commits 1
expect_kv behind 8

# F5 the OLDEST card is un-FR'd: nothing is cuttable. This is `at-target`, a
# different fact from an empty range, printed as one, and the blocker is named.
CUR="F5 oldest card un-FR'd -> at-target, nothing has passed FR yet"
all_ready; set_status 11 "FR on staging"
run_frontier "$R" main staging
expect_rc 0
expect_kv frontier_state at-target
expect_kv frontier "$M0"
expect_kv range_commits 9
expect_kv frontier_commits 0
expect_kv behind 9
expect_kv blocker "#11(FR on staging)"
expect "nothing has passed FR yet"
expect_not "no commits to compare"
expect_kv items_beyond_ready 4

# F6 an EMPTY range is the other fact, and says so.
CUR="F6 empty range -> empty-range, distinct text"
all_ready; run_frontier "$R" main main
expect_rc 0
expect_kv frontier_state empty-range
expect_kv frontier "$M0"
expect_kv range_commits 0
expect "no commits to compare"
expect_not "nothing has passed FR yet"

# F7 the board cannot be read: UNKNOWN, exit 3, no frontier.
CUR="F7 unreadable board -> UNKNOWN (exit 3)"
all_ready; run_frontier "$R" main staging D_GRAPHQL_FAIL=1
expect_rc 3
expect_kv frontier_state unknown
expect_eq "no frontier= line is emitted" "$(kv frontier)" ""
expect "UNKNOWN — the board could not be read for #11 after 5 attempts"
expect "Nothing was cut (fail closed)."

# F8 a PR with no card cannot have passed FR: it is a BLOCKER, named as such.
CUR="F8 a PR with no card is a blocker (frontier = M3)"
all_ready; rm -f "$FIX/status/14"
run_frontier "$R" main staging
expect_rc 0
expect_kv frontier "$M3"
expect_kv frontier_commits 5
expect_kv blocker "#14(not on the kanban board)"

# F9 commits/pulls never answers for a commit: UNKNOWN, not a cut before it.
CUR="F9 unanswerable commits/pulls -> UNKNOWN (exit 3)"
all_ready; run_frontier "$R" main staging D_PULLS_FAIL="$D4"
expect_rc 3
expect_kv frontier_state unknown
expect "commits/{sha}/pulls never answered for: $D4"

# F10 a direct push has no card and never will: the frontier stops before it and
# names the sha. Range is 10 here (p5 added): d1 d2 S1 f1 f2 M3 d4 p5 d6 S2.
CUR="F10 a direct push caps the frontier (frontier = d4, blocker = the sha)"
make_repo "$RP" push; all_ready
run_frontier "$RP" main staging
expect_rc 0
expect_kv frontier_state advanced
expect_kv frontier "$D4"
expect_kv range_commits 10
expect_kv behind 4
expect_kv blocker "$P5(no merged PR)"

# F11 a shallow checkout cannot show the range: refuse.
CUR="F11 shallow checkout -> UNKNOWN"
make_repo "$R"; all_ready
RSH="$WORK/shallow"; rm -rf "$RSH"
git clone -q --depth 1 --no-single-branch "file://$R" "$RSH" 2>/dev/null
run_frontier "$RSH" origin/main origin/staging
expect_rc 3
expect "this checkout is shallow"

# F13 REQUIRED must be a Status the walk can rank; a typo is UNKNOWN, not a pass.
CUR="F13 unrankable REQUIRED -> UNKNOWN"
run_frontier "$R" main staging REQUIRED="Ready for prd"
expect_rc 3
expect "is not a Status this walk can rank"

# F14 an item BEYOND the required rank (Prod) is fine for the frontier too.
CUR="F14 Prod/Done satisfy the frontier's rank"
all_ready; set_status 11 "Prod"; set_status 12 "Done"; set_status 16 "FR on staging"
run_frontier "$R" main staging
expect_kv frontier "$D4"

echo ""
total=$((pass + fail))
if [ "$fail" -gt 0 ]; then
  echo "fr-gate-walk-selftest: $fail/$total FAILED"
  exit 1
fi
echo "fr-gate-walk-selftest: $total assertions, all passed"
