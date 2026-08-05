#!/usr/bin/env bash
#
# public-pii-gate.yml's own decision table, asserted offline with a stubbed `gh`.
#
# WHY THIS EXISTS (tracebloc/backend#1409)
# This gate is the only thing standing between a customer name and a public repo,
# and it had no test. Every defect in #1409 was a path that returned "clean" while
# having compared nothing: an unset secret exiting 0, a denylist of only commas, a
# swallowed Compare API error, and a match discarded because `grep -q` closed a
# pipe and `pipefail` turned SIGPIPE into "no match". Each was found by reading,
# after it had already shipped. Reading is not the tool for this — a fail-open is
# invisible in the diff and obvious in a run.
#
# The gate's `run:` block is deliberately env-driven (no `${{ }}` inside it), so
# it can be extracted and executed directly. That property is load-bearing for
# this test; if a future edit interpolates an expression into the script body,
# EXTRACTION FAILS LOUDLY here rather than silently testing less.
#
# WHAT A GREEN RUN MEANS, AND WHAT IT DOES NOT
# Every case asserts BOTH the exit status and a distinguishing phrase from the
# output. Status alone is not enough: a fork PR and an unconfigured org both
# exit 1, and the whole point of the fork branch is that they must not say the
# same thing — one is fixable by an admin and the other is not. A test that
# checked only rc would pass while the gate gave impossible advice.
#
# Run:  bash scripts/tests/pii-gate-selftest.sh
set -uo pipefail

GATE=${GATE:-.github/workflows/public-pii-gate.yml}
WORK=$(mktemp -d) || exit 1
trap 'rm -rf "$WORK"' EXIT

# --- extract the run block -------------------------------------------------
if ! python3 - "$GATE" "$WORK/gate.sh" <<'PY'
import sys, re, yaml
gate, out = sys.argv[1], sys.argv[2]
doc = yaml.safe_load(open(gate))
try:
    run = doc['jobs']['pii-check']['steps'][0]['run']
except (KeyError, IndexError) as e:
    sys.exit(f"cannot locate jobs.pii-check.steps[0].run in {gate}: {e}")
leaked = re.findall(r'\$\{\{.*?\}\}', run)
if leaked:
    sys.exit(
        "the run block now interpolates GitHub expressions, so it can no longer "
        "be executed standalone and this test would silently cover less:\n  "
        + "\n  ".join(leaked)
        + "\nPass values through `env:` instead."
    )
open(out, 'w').write(run)
PY
then
  echo "FAIL: could not extract the gate script from $GATE" >&2
  exit 1
fi

# --- stub `gh`: serves a compare fixture, or fails on demand ---------------
cat > "$WORK/gh" <<'STUB'
#!/usr/bin/env bash
if [ "${STUB_GH_FAIL:-0}" = "1" ]; then
  echo "HTTP 403: API rate limit exceeded" >&2
  exit 1
fi
cat "$STUB_COMPARE_FIXTURE"
STUB
chmod +x "$WORK/gh"
export PATH="$WORK:$PATH"

pass=0; fail=0

# mkfixture <total_commits> <returned_count> [message ...]
# total > returned models the Compare API's 250-commit cap.
mkfixture() {
  local total=$1 n=$2; shift 2
  local msgs=()
  local i
  for ((i = 1; i <= n; i++)); do
    local m="${1:-commit $i}"; [ $# -gt 0 ] && shift
    msgs+=("$(printf '%s' "$m" | python3 -c 'import json,sys; print(json.dumps({"commit":{"message":sys.stdin.read()}}))')")
  done
  local joined
  joined=$(IFS=,; echo "${msgs[*]}")
  printf '{"total_commits":%s,"commits":[%s]}' "$total" "$joined" > "$WORK/fixture.json"
  export STUB_COMPARE_FIXTURE="$WORK/fixture.json"
}

# mkfixture_big <commit_count>
# Padding for the defect-3 regression case, generated straight into the fixture
# file by python. It must NOT travel via argv or the environment: Linux caps a
# single exec argument/env string at 128KB (MAX_ARG_STRLEN), so a ~150KB
# PR_BODY export made every later exec in this script die with E2BIG — green on
# macOS, "Argument list too long" on the runner. Padding via commit messages is
# also the faithful shape: backend#1409 describes the haystack as title + body
# + up to 250 commit messages, title first.
mkfixture_big() {
  python3 - "$1" "$WORK/fixture.json" <<'PY'
import json, sys
n = int(sys.argv[1])
pad = "padding line to fill the pipe buffer\n" * 8
commits = [{"commit": {"message": f"chore: routine commit {i}\n{pad}"}} for i in range(n)]
json.dump({"total_commits": n, "commits": commits}, open(sys.argv[2], "w"))
PY
  export STUB_COMPARE_FIXTURE="$WORK/fixture.json"
}

# check <name> <expected_rc> <expected_substring>
check() {
  local name=$1 want_rc=$2 want_txt=$3 out rc ok=1
  out=$(bash "$WORK/gate.sh" 2>&1); rc=$?
  [ "$rc" = "$want_rc" ] || ok=0
  grep -qF -- "$want_txt" <<<"$out" || ok=0
  if [ "$ok" = 1 ]; then
    printf '  ok   %s\n' "$name"; pass=$((pass + 1))
  else
    printf '  FAIL %s\n' "$name"
    printf '         rc=%s (want %s)\n' "$rc" "$want_rc"
    printf '         want text: %s\n' "$want_txt"
    printf '         got: %s\n' "$(head -3 <<<"$out" | tr '\n' ' ')"
    fail=$((fail + 1))
  fi
}

# A same-repo PR with a clean title/body and one ordinary commit.
base() {
  export PR_TITLE="a normal title" PR_BODY="a normal body"
  export PR_BASE_SHA=aaaa PR_HEAD_SHA=bbbb REPO_FULL=tracebloc/docs
  export LABELS='[]' IS_FORK=false DENYLIST=""
  unset STUB_GH_FAIL
  mkfixture 1 1 "chore: routine commit"
}

echo "unset secret — the state every public repo is in until PII_DENYLIST exists"
base; check "no denylist refuses, and names the admin fix" 1 "An org admin must run:"
base; LABELS='["pii-gate-override"]'
check "the override label rescues an unset-secret PR" 0 "bypassed via 'pii-gate-override'"

echo
echo "fork PRs — secrets are never passed to them, so no admin action helps"
base; IS_FORK=true
check "a fork refuses with a fork-specific reason" 1 "comes from a fork"
base; IS_FORK=true
check "a fork does NOT repeat the impossible admin advice" 1 "Setting the org secret will NOT fix"
base; IS_FORK=true; LABELS='["pii-gate-override"]'
check "a maintainer can record a hand review on a fork" 0 "bypassed via 'pii-gate-override'"

echo
echo "configured secret — does it actually catch anything"
base; DENYLIST="acme,globex"
check "a clean PR passes and states what it compared" 0 "PII gate passed"
base; DENYLIST="acme,globex"; PR_TITLE="fix for Acme Corp"
check "a match in the title blocks, case-insensitively" 1 "contain a denylisted term"
base; DENYLIST="acme,globex"; PR_BODY="reported by globex"
check "a match in the body blocks" 1 "contain a denylisted term"
base; DENYLIST="acme"; mkfixture 1 1 "fix: patch for ACME rollout"
check "a match in a commit message blocks" 1 "contain a denylisted term"

echo
echo "fail-closed paths — 'could not check' must never read as 'nothing found'"
base; DENYLIST=",,, ,"
check "a denylist of only separators refuses" 1 "no usable terms"
base; DENYLIST="acme"; mkfixture 300 2 "a" "b"
check "a truncated commit list refuses" 1 "truncated haystack"
base; DENYLIST="acme"; export STUB_GH_FAIL=1
check "an unreadable Compare response refuses" 1 "will not pass a haystack it failed to read"
base; DENYLIST="acme"; PR_BASE_SHA=""
check "a missing base SHA refuses" 1 "failing closed"
base; DENYLIST="ac*me"
check "a glob-shaped term is matched literally, not expanded" 0 "PII gate passed"

echo
echo "#1409 defect 3 regression — an early match in a haystack past the pipe buffer"
# The original shape: match on an early line, then enough padding to fill the
# 64KB pipe buffer. `grep -q` exited on the match and closed the pipe, `printf`
# took SIGPIPE, `pipefail` surfaced 141, and `if` read that as "no match" — so
# the gate was least reliable exactly when the PR was largest.
base; DENYLIST="acme"; PR_TITLE="Acme Corp early match"; mkfixture_big 250
# Assert the haystack actually clears the pipe buffer. Without this the case
# could quietly stop reproducing the bug — and a regression test that no longer
# reaches the defect is green for the wrong reason, which is the whole failure
# mode #1409 is about.
HAY_BYTES=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sum(len(c["commit"]["message"]) for c in d["commits"]))' "$WORK/fixture.json")
if [ "$HAY_BYTES" -le 65536 ]; then
  printf '  FAIL regression fixture is only %s bytes — under the 64KB pipe buffer,\n' "$HAY_BYTES"
  printf '       so this case no longer exercises defect 3. Raise the commit count.\n'
  fail=$((fail + 1))
else
  check "an early match in a >64KB haystack ($HAY_BYTES B) still blocks" 1 "contain a denylisted term"
fi

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
