#!/usr/bin/env bash
#
# house-rules.sh's matcher, asserted against fixtures.
#
# WHY THIS EXISTS (tracebloc/backend#1788)
# 852 lines of quote-aware, heredoc-aware, command-position-aware shell lexing,
# with `quality / house-rules` a REQUIRED status check on develop, staging AND
# prod across 16 repos -- and no test of any kind. A false positive here does not
# produce noise; it blocks every merge in the org, including the release train's
# own promotion PRs, so the fix would have to travel through the pipeline it just
# blocked.
#
# The blast radius arrives late by construction: callers pin `@main`, so a bad
# rule merged to .github/develop propagates nothing and surfaces during a
# develop -> staging -> main promotion. Invisible on the PR that introduces it.
#
# WHAT THIS ASSERTS, AND WHY BOTH HALVES
# Every case pins the finding COUNT and, where the rule is the point, the rule ID.
# A suite that only checked "exit non-zero" would pass while the matcher fired for
# the wrong reason -- and this script has four rules that share an exit code.
#
#   FIRES   the violation the rule was written for is caught
#   SILENT  the corrected form of that same violation is clean
#
# Neither half alone is worth having. Assert only FIRES and a matcher that flags
# everything passes; assert only SILENT and one that flags nothing does.
#
# THE ADVERSARIAL FIXTURE IS THE POINT (#1788's own argument). This checker matches
# arbitrary source content, including comments that DISCUSS the patterns being
# matched. The ticket's evidence: a five-pattern matcher over 588 real PR titles
# produced a false positive on `chore(ci): retire the WIP-limit nudge`, because
# "WIP limit" is a domain term here. That was on PR titles -- the shortest, most
# uniform text we have. So this suite runs the checker over THIS REPO'S OWN
# prose-dense scripts and requires zero findings.
#
# Run:  bash scripts/tests/house-rules-selftest.sh
set -uo pipefail

HR=${HR:-scripts/house-rules.sh}
[ -x "$HR" ] || { echo "FATAL: $HR missing or not executable"; exit 2; }

# A THROWAWAY GIT REPO, not a bare temp dir, and this is load-bearing rather than
# tidy. Several of the checker's behaviours are REPO-WIDE by design: the
# flag-holding-variable table is built with `git grep` across the repo (because
# `readonly CURL_SECURE=...` in one file is used as `curl $CURL_SECURE` in
# another), and so is the sourced-file list the pipefail rule stands down on.
#
# A fixture in a plain /tmp directory cannot reach either. My first attempt did
# exactly that and the flag-var case failed -- which I read for a minute as "the
# documented feature is inert" before checking. It is not; the fixture could not
# see the table. Recorded because the wrong conclusion was one commit away, and
# because it means any future case about repo-wide state has to live in here.
WORK=$(mktemp -d) || exit 2
# THE TRAP HAS TO CLEAN THE CHECKOUT TOO, not just $WORK (Bugbot, .github#291). The
# adversarial control below writes a deliberate `curl` violation INTO THIS REPO and
# `git add`s it, because `--all` enumerates with `git ls-files` and cannot see an
# untracked file. An interrupt between the add and the cleanup would leave
# `.hr-control.sh` staged -- and that file is itself a house-rules hit, so the next
# `make check` would fail on leftover test state and point at the wrong thing.
CONTROL=scripts/tests/.hr-control.sh
cleanup() {
  rm -rf "$WORK"
  git rm -q -f --cached "$CONTROL" >/dev/null 2>&1 || true
  rm -f "$CONTROL"
}
trap cleanup EXIT INT TERM
HR_ABS=$(cd "$(dirname "$HR")" && pwd)/$(basename "$HR")
( cd "$WORK" && git init -q . && git config user.email t@t && git config user.name t ) || exit 2

# HERE-STRINGS, NEVER `printf … | grep -q` (Bugbot, .github#291). `grep -q` exits on
# its first match and SIGPIPEs the writer; under `set -o pipefail` the PIPELINE can
# then take status 141, and the negated form (`! … | grep -q`) reads 141 as "absent"
# -- so a `disable:` directive that did nothing would record a PASS.
#
# Honest about reach: `printf` of a short string usually completes before `grep`
# leaves, so 141 is unlikely here rather than impossible -- the same measurement
# release-train's fr-evidence.sh records (SIGPIPE needs the writer alive, which for a
# fetch-write-exit process means output past the 64K pipe buffer). It is changed
# anyway because a here-string is simply the smaller construction: no pipe, so no
# pipefail interaction for the next reader to re-derive.
PASS=0; FAIL=0
record() {  # $1 ok(0/1) · $2 name · $3 detail
  if [ "$1" = 0 ]; then
    PASS=$((PASS+1)); printf 'ok    %s\n' "$2"
  else
    FAIL=$((FAIL+1)); printf 'FAIL  %s\n      %s\n' "$2" "$3"
  fi
}

# Write a fixture and run the checker over it alone. Echoes the output; sets RC.
# `--config /dev/null` so a repo `.house-rules.conf` cannot change the answer --
# without it this suite would measure whatever config the checkout happens to
# carry, which is the same class of defect as a fixture richer than production.
run_on() {  # $1 = path RELATIVE to $WORK ; sets OUT, RC
  OUT=$( cd "$WORK" && sh "$HR_ABS" --config /dev/null "$1" 2>&1 ); RC=$?
}

fixture() {  # $1 = name · $2 = body -> writes it into the throwaway repo and
             # `git add`s it, because `git grep` only sees tracked content
  printf '%s\n' "$2" > "$WORK/$1"
  ( cd "$WORK" && git add -- "$1" >/dev/null 2>&1 )
  printf '%s' "$1"
}

# THE EXPECTED RULE SET, not a count and not "somewhere in the output" (Bugbot,
# .github#291). The first version of this helper took a count as $3, computed `n`,
# and NEVER COMPARED THEM -- a parameter that reads as an assertion and is dead. So
# a non-zero case only checked rc=1 plus "the named rule appears", and extra rules
# passed silently.
#
# Bugbot's example is the one that mattered: the scoped-pragma case `ignore=curl-tls`
# stayed green even if the scoping were a NO-OP, because `curl-timeout` fires either
# way and the test only asked whether `curl-timeout` appeared. The case existed to
# prove `ignore=` narrows to one rule and could not have failed if it didn't.
#
# A SET is strictly more informative than a count and kills the dead parameter: the
# expected value is now the exact comma-separated rule ids, `-` for none. Extra
# findings fail, missing findings fail, and the wrong rule fails.
#
# Parsed from `[rule-id]` occurrences, never from the summary line -- the old grep
# alternation included the word `house-rules`, which matches the summary itself, so
# `n` was not even a finding count.
#
# $1 desc · $2 body · $3 expected rule set ("-" = clean, else "a" or "a,b")
expect() {
  local desc="$1" body="$2" want="$3" f got
  f=$(fixture "t$$.sh" "$body")
  run_on "$f"
  # `grep -oE`, not `sed -n s/.../\1/p`: BSD sed has no `\|` alternation, so the
  # first version matched nothing and every firing case reported "-". Portability is
  # this script's whole design constraint, and the suite has to honour it too.
  got=$(printf '%s\n' "$OUT" | grep -oE '\[(curl-tls|curl-timeout|helm-timeout|pipefail|no-print)\]' \
        | tr -d '[]' | sort -u | paste -sd, - )
  [ -n "$got" ] || got="-"
  if [ "$want" = "-" ]; then
    if [ "$RC" = 0 ] && [ "$got" = "-" ]; then record 0 "$desc" ""
    else record 1 "$desc" "rc=$RC rules=[$got] want clean; out=$OUT"; fi
    return
  fi
  if [ "$RC" != 1 ]; then record 1 "$desc" "rc=$RC (want 1) rules=[$got]"; return; fi
  if [ "$got" != "$want" ]; then
    record 1 "$desc" "rules=[$got] want [$want]"; return
  fi
  record 0 "$desc" ""
}

# ============================================================ the four rules
# Each rule twice: the violation it exists for, then the corrected form. The
# corrected forms are taken from what the rule's own header says satisfies it,
# not invented -- a fixture I made up would assert my reading of the rule
# rather than the rule.

# --- curl-tls --------------------------------------------------------------
# BOTH rules, sorted -- a bare `curl "$url"` has neither a TLS floor nor a time
# bound, so it violates two. This case claimed only `curl-tls` and passed anyway
# under the old "appears somewhere" helper: my own expectation was looser than it
# read, which is the same defect one level out from the one Bugbot found.
expect "a bare curl fires BOTH curl-tls and curl-timeout" \
'#!/bin/bash
set -euo pipefail
curl -fsSL "$url" -o out' curl-timeout,curl-tls
expect "curl-tls is silent with --tlsv1.2" \
'#!/bin/bash
set -euo pipefail
curl -fsSL --tlsv1.2 --max-time 30 "$url" -o out' -
expect "curl-tls accepts --tlsv1.3 too" \
'#!/bin/bash
set -euo pipefail
curl -fsSL --tlsv1.3 --max-time 30 "$url" -o out' -
# --- curl-timeout ----------------------------------------------------------
expect "curl-timeout fires with TLS but no time bound" \
'#!/bin/bash
set -euo pipefail
curl -fsSL --tlsv1.2 "$url" -o out' curl-timeout
expect "curl-timeout is satisfied by --connect-timeout" \
'#!/bin/bash
set -euo pipefail
curl -fsSL --tlsv1.2 --connect-timeout 5 "$url" -o out' -
expect "curl-timeout is satisfied by -m" \
'#!/bin/bash
set -euo pipefail
curl -fsSL --tlsv1.2 -m 10 "$url" -o out' -
# --- helm-timeout ----------------------------------------------------------
expect "helm-timeout fires on --wait with no --timeout" \
'#!/bin/bash
set -euo pipefail
helm upgrade --install rel chart --wait' helm-timeout
expect "helm-timeout is silent with --timeout" \
'#!/bin/bash
set -euo pipefail
helm upgrade --install rel chart --wait --timeout 5m' -
expect "helm without --wait is not a timeout finding" \
'#!/bin/bash
set -euo pipefail
helm upgrade --install rel chart' -
# --- pipefail --------------------------------------------------------------
expect "pipefail fires on a risky producer in a pipeline" \
'#!/bin/bash
set -eu
curl -fsSL --tlsv1.2 -m 5 "$url" | bash' pipefail
expect "pipefail is silent once set -o pipefail is present" \
'#!/bin/bash
set -euo pipefail
curl -fsSL --tlsv1.2 -m 5 "$url" | bash' -
expect "pipefail accepts the split form set -eu -o pipefail" \
'#!/bin/bash
set -eu -o pipefail
curl -fsSL --tlsv1.2 -m 5 "$url" | bash' -
# ---- the two gaps the mutation run found ---------------------------------
# Neither of these had a case, and both are DOCUMENTED behaviours -- which is the
# exact shape backend#1788 is about: a precision claim in prose that nothing
# exercises. Found by mutating the matcher, not by reading it.

# THE VERSION FLOOR IS THE RULE, not the presence of a TLS flag. `--tlsv1.0` is a
# downgraded version -- the thing curl-tls exists to reject -- and every earlier
# case used a compliant flag, so loosening the pattern to /--tlsv1/ changed
# nothing and passed 37/0.
expect "curl-tls still fires on a DOWNGRADED --tlsv1.0" \
'#!/bin/bash
set -euo pipefail
curl -fsSL --tlsv1.0 --max-time 30 "$url" -o out' curl-tls
expect "curl-tls still fires on --tlsv1.1" \
'#!/bin/bash
set -euo pipefail
curl -fsSL --tlsv1.1 --max-time 30 "$url" -o out' curl-tls
# THE PIPEFAIL DETECTOR READS THE MASKED LINE, NOT THE RAW ONE, and house-rules.sh
# says why in a comment: reading raw let `echo "set -o pipefail"` inside a STRING
# mark the whole file safe and suppress every real finding. That is a bug that was
# already found and fixed once -- and nothing pinned the fix, so a refactor back to
# `lraw` passed the entire suite.
# THE FIXTURE HAS TO PUT WHITESPACE BEFORE `set`, and finding that out was the
# whole exercise. `echo "set -o pipefail"` does NOT reproduce the bug: the detector
# anchors on `(^|[[:space:];])set`, and there the `set` is preceded by a double
# quote, so a raw-line read would not match it either. My first fixture used exactly
# that form and the mutation stayed green -- a case written for a bug it could not
# construct, which is worth less than no case because it reads like coverage.
#
# Verified by applying the `lraw` mutation and enumerating: `echo "please set -o
# pipefail here"`, `MSG="run set -o pipefail first"`, `echo " set -o pipefail"` and
# `printf "x; set -o pipefail\n"` all reproduce it; a COMMENT does not (comments are
# stripped before either variable is built).
expect "a quoted \"set -o pipefail\" does NOT mark the file safe" \
'#!/bin/bash
set -eu
echo "please set -o pipefail here"
curl -fsSL --tlsv1.2 -m 5 "$url" | bash' pipefail
expect "...nor does one in a variable assignment" \
'#!/bin/bash
set -eu
MSG="run set -o pipefail first"
curl -fsSL --tlsv1.2 -m 5 "$url" | bash' pipefail
# ==================================================== the lexer's own claims
# The header makes six precision promises. Each is a documented reason the rules
# do NOT fire, and each is therefore a way the checker could start crying wolf
# after a refactor. Untested, they are prose.

expect "a tool named only in a COMMENT is not a finding" \
'#!/bin/bash
set -euo pipefail
# curl -fsSL "$url" would need --tlsv1.2 and --max-time here
echo ok' -
expect "a tool inside a HEREDOC BODY is data, not code" \
'#!/bin/bash
set -euo pipefail
cat <<EOF
curl -fsSL http://example.com
EOF' -
expect "a quoted heredoc tag is still skipped" \
"#!/bin/bash
set -euo pipefail
cat <<'EOF'
curl -fsSL http://example.com
EOF" -

expect "a tool name in ARGUMENT position is not a command" \
'#!/bin/bash
set -euo pipefail
command -v curl >/dev/null || exit 1
echo "using curlimages/curl as the image"' -
expect "a \`-continuation is ONE logical command" \
'#!/bin/bash
set -euo pipefail
curl -fsSL --tlsv1.2 \
  --max-time 30 \
  "$url" -o out' -
expect "curl inside \$( ) IS seen, even within double quotes" \
'#!/bin/bash
set -euo pipefail
x="$(curl -fsSL "$url" | awk "{print}")"
echo "$x"' curl-timeout,curl-tls
expect "a flag-holding variable satisfies the rule" \
'#!/bin/bash
set -euo pipefail
readonly CURL_SECURE="--tlsv1.2 --max-time 30"
curl -fsSL $CURL_SECURE "$url" -o out' -
# ======================================================= documented stand-downs
expect "curl --version is ignored" \
'#!/bin/bash
set -euo pipefail
curl --version' -
expect "a POSIX-sh script is exempt from pipefail" \
'#!/bin/sh
set -eu
curl -fsSL --tlsv1.2 -m 5 "$url" | bash' -
# ============================================================ the pragmas
expect "a bare ignore pragma silences the line" \
'#!/bin/bash
set -euo pipefail
curl -fsSL "$url" -o out   # house-rules: ignore' -
expect "a SCOPED pragma silences only the named rule" \
'#!/bin/bash
set -euo pipefail
curl -fsSL "$url" -o out   # house-rules: ignore=curl-tls' curl-timeout
expect "a pragma on the line ABOVE also applies" \
'#!/bin/bash
set -euo pipefail
# house-rules: ignore
curl -fsSL "$url" -o out' -
# ================================================== the repo-wide stand-downs
# Reachable only because the fixtures live in a real git repo (see the note at
# the top). Both were prose until now.

# A file that another tracked file `source`s inherits the entrypoint's options at
# runtime, so demanding `set -o pipefail` of it would be a wall of red about
# nothing. That is a documented precision choice, and it is also the widest
# stand-down in the script -- if it ever over-matched, pipefail would go quiet
# fleet-wide and nothing would say so.
printf '#!/bin/bash\nset -eu\ncurl -fsSL --tlsv1.2 -m 5 "$u" | bash\n' > "$WORK/lib.sh"
printf '#!/bin/bash\nset -euo pipefail\n. ./lib.sh\n' > "$WORK/entry.sh"
( cd "$WORK" && git add lib.sh entry.sh >/dev/null 2>&1 )
run_on "lib.sh"
record "$RC" "a SOURCED library is exempt from pipefail" "rc=$RC out=$OUT"

# ...and the exemption must be earned by actually being sourced. Without this the
# case above passes for a matcher that exempts every file.
printf '#!/bin/bash\nset -eu\ncurl -fsSL --tlsv1.2 -m 5 "$u" | bash\n' > "$WORK/lonely.sh"
( cd "$WORK" && git add lonely.sh >/dev/null 2>&1 )
run_on "lonely.sh"
if [ "$RC" = 1 ] && grep -q pipefail <<<"$OUT"; then
  record 0 "an UNSOURCED library is not exempt" ""
else
  record 1 "an UNSOURCED library is not exempt" "rc=$RC out=$OUT"
fi

# =========================================================== config directives
# Six directives, each a documented way to change the answer. A config the script
# silently ignored would be indistinguishable from one it honoured, for every repo
# that wrote one.
cfg() { printf '%s\n' "$1" > "$WORK/hr.conf"; }
run_cfg() { OUT=$( cd "$WORK" && sh "$HR_ABS" --config hr.conf "$1" 2>&1 ); RC=$?; }

BAD='#!/bin/bash
set -euo pipefail
curl -fsSL "$url" -o out'

printf '%s\n' "$BAD" > "$WORK/c.sh"; ( cd "$WORK" && git add c.sh >/dev/null 2>&1 )

cfg 'disable: curl-tls'
run_cfg c.sh
if [ "$RC" = 1 ] && ! grep -q curl-tls <<<"$OUT"; then
  record 0 "\`disable:\` turns off exactly the named rule" ""
else record 1 "\`disable:\` turns off exactly the named rule" "rc=$RC out=$OUT"; fi

cfg 'exclude: c.sh'
run_cfg c.sh
record "$RC" "\`exclude:\` skips the path" "rc=$RC out=$OUT"

cfg 'timeout-wrapper: guard'
printf '#!/bin/bash\nset -euo pipefail\nguard curl -fsSL --tlsv1.2 "$url"\n' > "$WORK/tw.sh"
( cd "$WORK" && git add tw.sh >/dev/null 2>&1 )
run_cfg tw.sh
record "$RC" "\`timeout-wrapper:\` stands the *-timeout rules down" "rc=$RC out=$OUT"

cfg 'wrapper: spin_cmd'
printf '#!/bin/bash\nset -euo pipefail\nspin_cmd curl -fsSL "$url"\n' > "$WORK/wr.sh"
( cd "$WORK" && git add wr.sh >/dev/null 2>&1 )
run_cfg wr.sh
if [ "$RC" = 1 ] && grep -q curl-tls <<<"$OUT"; then
  record 0 "\`wrapper:\` puts the wrapped tool back in command position" ""
else record 1 "\`wrapper:\` puts the wrapped tool back in command position" "rc=$RC out=$OUT"; fi

cfg 'rule: no-print | *.py | ^[[:space:]]*print\( | use the logger, not print()'
printf 'def f():\n    print("x")\n' > "$WORK/p.py"
( cd "$WORK" && git add p.py >/dev/null 2>&1 )
run_cfg p.py
if [ "$RC" = 1 ] && grep -q no-print <<<"$OUT"; then
  record 0 "a custom \`rule:\` matches a language the script knows nothing about" ""
else record 1 "a custom \`rule:\` matches a language the script knows nothing about" "rc=$RC out=$OUT"; fi

# ================================================================ fail-closed
# The exit contract: 0 clean, 1 findings, 2 usage/internal. A checker that
# returned 0 on a path it could not evaluate would be a required check that
# passes when it cannot tell -- the shape this whole ticket is about.
# A NAMED PATH THAT DOES NOT EXIST reports "no shell files to check" and exits 0.
# That looks like the fail-open this ticket is about, and I nearly filed it as one.
# It is NOT REACHABLE FROM CI: code-quality.yml never passes explicit paths -- it
# builds `--all` or `--base "$BASE_SHA"` (code-quality.yml:755-758). So the only way
# in is a human typing a path, where "no shell files to check" is the right answer
# to a typo they can see.
#
# Asserted as the CURRENT behaviour rather than fixed, per this org's rule that a
# fix for an unreachable path costs more than filing nothing (backend#1729 rule 8).
# It is pinned so that if it ever CHANGES, the change is deliberate -- and if CI
# ever starts passing explicit paths, this case is where the exposure shows up.
OUT=$( cd "$WORK" && sh "$HR_ABS" --config /dev/null nosuchfile.sh 2>&1 ); RC=$?
if [ "$RC" = 0 ] && grep -q "no shell files" <<<"$OUT"; then
  record 0 "a named missing path is 'no shell files' + rc 0 (CI-unreachable)" ""
else record 1 "a named missing path is 'no shell files' + rc 0 (CI-unreachable)" "rc=$RC out=$OUT"; fi

# THE REACHABLE VARIANT, which is the one worth guarding: `--base` over a diff that
# DELETED a shell file. CI takes this path on every PR that removes one, and a
# checker that tried to read the deleted path would either crash or -- worse --
# abort the run and report nothing about the files that DO exist.
BASEDIR="$WORK/basecase"
mkdir -p "$BASEDIR"
( cd "$BASEDIR" && git init -q . && git config user.email t@t && git config user.name t
  printf '#!/bin/bash\nset -euo pipefail\ncurl -fsSL --tlsv1.2 -m 5 "$u"\n' > a.sh
  git add a.sh && git commit -q -m base ) >/dev/null 2>&1
BASE_REF=$( cd "$BASEDIR" && git rev-parse HEAD )
( cd "$BASEDIR" && git rm -q a.sh
  printf '#!/bin/bash\nset -eu\ncurl -fsSL "$u" | bash\n' > b.sh
  git add b.sh && git commit -q -m "delete a.sh, add a bad b.sh" ) >/dev/null 2>&1
OUT=$( cd "$BASEDIR" && sh "$HR_ABS" --base "$BASE_REF" --config /dev/null 2>&1 ); RC=$?
if [ "$RC" = 1 ] && grep -q curl-tls <<<"$OUT"; then
  record 0 "--base over a diff with a DELETED file still checks the survivors" ""
else
  record 1 "--base over a diff with a DELETED file still checks the survivors" "rc=$RC out=$OUT"
fi

OUT=$( cd "$WORK" && sh "$HR_ABS" --nonsense-flag 2>&1 ); RC=$?
record "$([ "$RC" = 2 ] && echo 0 || echo 1)" \
  "an unknown flag exits 2 (usage), not 0" "rc=$RC out=$OUT"

OUT=$( cd "$WORK" && sh "$HR_ABS" --config /dev/null --soft-fail c.sh 2>&1 ); RC=$?
if [ "$RC" = 0 ] && grep -q curl-tls <<<"$OUT"; then
  record 0 "--soft-fail reports the finding and still exits 0" ""
else record 1 "--soft-fail reports the finding and still exits 0" "rc=$RC out=$OUT"; fi

# ===================================================== the adversarial corpus
# #1788's own argument: this checker matches arbitrary source content, including
# comments that DISCUSS the patterns being matched. Its evidence was a five-pattern
# matcher over 588 real PR titles producing a false positive on a domain term --
# and that was on titles, the most uniform text we have.
#
# So: the checker over THIS REPO's own tracked files, which include house-rules.sh
# itself (852 lines that name every pattern it matches) and this suite (which
# writes `curl -fsSL "$url"` as fixture data). Zero findings required.
OUT=$( sh "$HR" --all --config /dev/null 2>&1 ); RC=$?
NF=$(printf '%s' "$OUT" | grep -oE 'across [0-9]+ file' | grep -oE '[0-9]+' | head -1)
record "$RC" "ZERO findings over this repo's own prose-dense scripts" "rc=$RC out=$OUT"

# AND A CONTROL, because "zero findings" and "the matcher stopped working" print
# the same thing. Injecting a known violation into that same corpus must fire --
# without this, a matcher that matched nothing would pass the case above.
# `git add`ed, because `--all` enumerates with `git ls-files` -- TRACKED files only
# (house-rules.sh:252). An untracked control is invisible, so the first version of
# this case "passed" the clean assertion and failed the control for the wrong reason.
printf '#!/bin/bash\nset -eu\ncurl -fsSL "$u" | bash\n' > "$CONTROL"
git add -f "$CONTROL" >/dev/null 2>&1
OUT2=$( sh "$HR" --all --config /dev/null 2>&1 ); RC2=$?
# Removed here for the normal path AND by the EXIT trap for every other path.
git rm -q -f --cached "$CONTROL" >/dev/null 2>&1
rm -f "$CONTROL"
if [ "$RC2" = 1 ] && grep -q curl-tls <<<"$OUT2"; then
  record 0 "...and a violation injected into that corpus IS caught" \
    "so the clean result above is a clean corpus, not a dead matcher"
else
  record 1 "...and a violation injected into that corpus IS caught" "rc=$RC2 out=$OUT2"
fi

printf '\n%d passed, %d failed  (%s files in the adversarial pass)\n' "$PASS" "$FAIL" "${NF:-?}"
[ "$FAIL" = 0 ] || exit 1
