#!/usr/bin/env bash
# =============================================================================
#  pipefail-early-close-selftest.sh — the early-close gate, tested (backend#2264)
#
#  WHY THIS EXISTS. The gate runs in code-quality.yml against every repo in the
#  fleet. A scanner that over-reports gets switched off; one that under-reports
#  is worse, because it looks like coverage. Both directions are asserted here.
#
#  Every case drives the REAL scripts. There is no re-implementation of the
#  rules in this file — an inline copy drifts from production and then proves
#  that a regex nobody runs would have caught the bug (backend#1729 rule 9).
#
#  THE THREE FALSE POSITIVES THIS SUITE EXISTS TO PIN. All three were found by
#  reviewers, on the PRs that built the rule, and all three were shapes the
#  author had not thought to test:
#    - `cmd || grep -q x <<<"$y"` — the second bar of a `||` read as a pipe, so
#      the gate rejected the form its own remediation produces.
#    - `producer | grep needle && cmd || grep -q x` — neutralising `||`
#      destroyed the boundary `grep[^|]*` relied on, so a plain `| grep` reached
#      a later `-q`.
#    - `producer | head||die` — the mirror, on the head arm, which was left
#      behind when the boundary was fixed for grep only.
#
#  ASSERTIONS USE HERE-STRINGS, never `printf … | grep -q`. This suite runs
#  under `set -uo pipefail`, so on a large enough $OUT the `grep -q` closes
#  early, the producer takes SIGPIPE, and the pipeline returns 141 -- a real
#  match reads as ABSENT and the assertion then passes or fails for the wrong
#  reason. That is precisely the hazard this file exists to prove, and its own
#  first version committed it in eight assertions (Bugbot, .github#300).
#  `house-rules-selftest.sh` already uses here-strings for the same reason.
#
#  Run:  bash scripts/tests/pipefail-early-close-selftest.sh
# =============================================================================
set -uo pipefail

GATE=${GATE:-scripts/pipefail-early-close.sh}
SCANNER=${SCANNER:-scripts/pipefail-early-close.awk}
[ -r "$GATE" ]    || { echo "FATAL: $GATE missing";    exit 2; }
[ -r "$SCANNER" ] || { echo "FATAL: $SCANNER missing"; exit 2; }
GATE_ABS=$(cd "$(dirname "$GATE")" && pwd)/$(basename "$GATE")
SCANNER_ABS=$(cd "$(dirname "$SCANNER")" && pwd)/$(basename "$SCANNER")

WORK=$(mktemp -d) || exit 2
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
record() {  # $1 ok(0/1) · $2 name · $3 detail
  if [ "$1" = 0 ]; then
    PASS=$((PASS+1)); printf 'ok    %s\n' "$2"
  else
    FAIL=$((FAIL+1)); printf 'FAIL  %s\n      %s\n' "$2" "$3"
  fi
}

# Write a file that DOES enable both options — i.e. the hazardous context — and
# scan it alone. Sets OUT.
scan_hazardous() {  # $1 = name ; $2.. = body lines
  local name="$1"; shift
  { printf '#!/usr/bin/env bash\nset -euo pipefail\n'; printf '%s\n' "$@"; } > "$WORK/$name"
  OUT=$(awk -f "$SCANNER_ABS" "$WORK/$name")
}
# Same, but the caller supplies the whole file including its `set` lines.
#
# THE FIXTURE IS A printf FORMAT, ON ONE LINE, and that is load-bearing rather
# than a style choice. Written as a multi-line quoted string, a fixture's
# `set -euo pipefail` sits at COLUMN 0 of *this* file -- and the scanner cannot
# tell a quoted string from code (the documented limitation, client#777). It
# therefore reads this suite as a script that enables errexit, and flags all
# ~20 fixture pipes below as real findings. That is not hypothetical: it is
# exactly how the first version of this file failed CI, and only in CI, because
# `git ls-files` skips an untracked file and the suite was still untracked when
# it passed locally. Keep fixtures on one line.
scan_raw() {  # $1 = name ; $2 = printf FORMAT for the whole file
  # shellcheck disable=SC2059  # $2 IS the format, by contract
  printf "$2" > "$WORK/$1"
  OUT=$(awk -f "$SCANNER_ABS" "$WORK/$1")
}
flags()   { [ -n "$OUT" ]; }
spares()  { [ -z "$OUT" ]; }

case_flag()  { # $1 name ; $2 desc ; rest = body
  local n="$1" d="$2"; shift 2
  scan_hazardous "$n" "$@"
  if flags; then record 0 "$d" ""; else record 1 "$d" "expected a finding, got none"; fi
}
case_spare() { # $1 name ; $2 desc ; rest = body
  local n="$1" d="$2"; shift 2
  scan_hazardous "$n" "$@"
  if spares; then record 0 "$d" ""; else record 1 "$d" "expected no finding, got: $OUT"; fi
}

echo "== the hazard is detected at all =============================================="
# Without these, every "spares X" case below is satisfied by a dead scanner.
case_flag bad.sh      "a pipe into head IS flagged"                '  x="$(ls /tmp | head -1)"'
case_flag badq.sh     "a pipe into grep -q IS flagged"             '  producer | grep -q needle'
case_flag badm.sh     "a pipe into grep -m N IS flagged"           '  kubectl get deploy -o name | grep -m1 needle'
case_flag badamp.sh   "|& is a pipe too"                           '  noisy |& head -1'
case_flag badampq.sh  "|& into grep -q likewise"                   '  noisy |& grep -q needle'

echo
echo "== discrimination: readers that do NOT close early ============================"
case_spare plaingrep.sh "a grep that reads to EOF is not the hazard" \
  '  producer | grep needle | sed s/a/b/'

echo
echo "== the house idioms are spared ================================================"
case_spare good.sh   "here-string" '  out="$(producer || true)"' '  head -25 <<<"$out"'
case_spare slice.sh  "capture-then-slice" '  out="$(producer)"' '  first="${out%%$'"'"'\n'"'"'*}"'
case_spare ortrue.sh "a line whose status is discarded with || true" \
  '  ver="$(tool --version | head -1 || true)"'
case_spare comment.sh "comments — prose about the hazard is not the hazard" \
  '  # NOT `producer | head -1`: head closes the pipe'
case_spare allow.sh  "the '# pipefail-guard: allow' marker opts a line out" \
  '  x="$(ls /tmp | head -1)"   # pipefail-guard: allow'

echo
echo "== BOTH options are required =================================================="
scan_raw nopipe.sh '#!/usr/bin/env bash\nset -uo pipefail\n  x="$(ls /tmp | head -1)"\n'
if spares; then record 0 "pipefail without errexit is not the hazard" ""; else record 1 "pipefail without errexit is not the hazard" "$OUT"; fi
scan_raw noeo.sh '#!/usr/bin/env bash\nset -eu\n  x="$(ls /tmp | head -1)"\n'
if spares; then record 0 "errexit without pipefail is not the hazard" ""; else record 1 "errexit without pipefail is not the hazard" "$OUT"; fi

echo
echo "== options are POSITIONAL, not per-file ======================================="
scan_raw infunc.sh '#!/usr/bin/env bash\nmain() {\n  set -euo pipefail\n  x="$(ls /tmp | head -1)"\n}\n'
if flags; then record 0 "options set inside a function are seen" ""; else record 1 "options set inside a function are seen" "no finding"; fi

scan_raw plusE.sh '#!/usr/bin/env bash\nset -euo pipefail\nrun() {\n  set +e\n  x="$(ls /tmp | head -1)"\n}\n'
if spares; then record 0 "a 'set +e' region is NOT flagged" ""; else record 1 "a 'set +e' region is NOT flagged" "$OUT"; fi

scan_raw longform.sh '#!/usr/bin/env bash\nset -o errexit\nset -o pipefail\n  x="$(ls /tmp | head -1)"\n'
if flags; then record 0 "the LONG spellings count (set -o errexit)" ""; else record 1 "the LONG spellings count (set -o errexit)" "no finding"; fi

scan_raw longoff.sh '#!/usr/bin/env bash\nset -o errexit\nset -o pipefail\nset +o pipefail\n  x="$(ls /tmp | head -1)"\n'
if spares; then record 0 "and the long form DISABLES too (set +o pipefail)" ""; else record 1 "and the long form DISABLES too" "$OUT"; fi

# A TRAILING COMMENT ON THE `set` LINE MUST NOT CHANGE THE OPTIONS. `apply_set`
# ran on the raw line, so splitting on whitespace turned comment tokens into
# real option updates: `# note: +e would be bad` cleared errexit and every
# hazard below went unreported. Fail-open and silent (Bugbot and Asad,
# independently, .github#300).
scan_raw cmtplus.sh '#!/usr/bin/env bash\nset -euo pipefail  # note: +e would be bad\n  x="$(ls /tmp | head -1)"\n'
if flags; then record 0 "a '+e' inside a set-line comment does not disarm errexit" ""; else record 1 "a '+e' inside a set-line comment does not disarm errexit" "no finding"; fi
scan_raw cmtpipe.sh '#!/usr/bin/env bash\nset -euo pipefail  # never +o pipefail here\n  x="$(ls /tmp | head -1)"\n'
if flags; then record 0 "nor does '+o pipefail' in a set-line comment" ""; else record 1 "nor does '+o pipefail' in a set-line comment" "no finding"; fi
# The discrimination: a REAL `set +e` after the line must still disarm, or the
# two cases above would pass under a scanner that simply ignores every `+`.
scan_raw realplus.sh '#!/usr/bin/env bash\nset -euo pipefail\nset +e\n  x="$(ls /tmp | head -1)"\n'
if spares; then record 0 "but a REAL 'set +e' still disarms it" ""; else record 1 "but a REAL 'set +e' still disarms it" "$OUT"; fi

echo
echo "== a `set` line carries code too =============================================="
# The dispatch `next`ed after apply_set, so the rest of the PHYSICAL line was
# never judged (Asad, .github#300). Third `next`-shaped miss on this file.
scan_raw setsame.sh '#!/usr/bin/env bash\nset -euo pipefail; producer | head -1\n'
if flags; then record 0 "a hazard on the SAME line as 'set -euo pipefail' is flagged" ""; else record 1 "a hazard on the SAME line as 'set -euo pipefail' is flagged" "no finding"; fi
# And the bigger version found while fixing it: `pipefail;` tokenised with the
# semicolon attached, so the `-o` handler never matched and p_on stayed 0 for
# the WHOLE FILE — every hazard in it skipped, not just the one on that line.
scan_raw setsemi.sh '#!/usr/bin/env bash\nset -euo pipefail; cd /tmp\necho a\nproducer | head -1\n'
if flags; then record 0 "'set -euo pipefail; cmd' still enables pipefail for the file" ""; else record 1 "'set -euo pipefail; cmd' still enables pipefail for the file" "no finding"; fi
# The discriminations: falling through must not lose the DISABLING forms, which
# is the whole reason the state machine is positional.
scan_raw setoff1.sh '#!/usr/bin/env bash\nset -euo pipefail\nset +e; producer | head -1\n'
if spares; then record 0 "...and 'set +e; cmd' on one line still disarms" ""; else record 1 "...and 'set +e; cmd' on one line still disarms" "$OUT"; fi
scan_raw setoff2.sh '#!/usr/bin/env bash\nset -euo pipefail\nset +o pipefail; producer | head -1\n'
if spares; then record 0 "...as does 'set +o pipefail; cmd'" ""; else record 1 "...as does 'set +o pipefail; cmd'" "$OUT"; fi

echo
echo "== one-line functions are scanned, not skipped ================================"
scan_raw oneline.sh '#!/usr/bin/env bash\nset -euo pipefail\nfirst() { producer | head -1; }\n'
if flags; then record 0 "a one-line function body is scanned" ""; else record 1 "a one-line function body is scanned" "no finding"; fi
scan_raw onelinec.sh '#!/usr/bin/env bash\nset -euo pipefail\nfirst() { producer | head -1; }   # and with a trailing comment\n'
if flags; then record 0 "...even with a trailing comment" ""; else record 1 "...even with a trailing comment" "no finding"; fi
# A MULTI-LINE opener can carry code on the opener line itself. That branch
# `next`ed after recording the scope, skipping its own hazard — the same defect
# as the one-liner case above, one shape over (Bugbot, .github#300).
scan_raw openerbody.sh '#!/usr/bin/env bash\nset -euo pipefail\nf() { producer | head -1\n  more_stuff\n}\n'
if flags; then record 0 "a hazard ON a multi-line function opener is flagged" ""; else record 1 "a hazard ON a multi-line function opener is flagged" "no finding"; fi
# The discrimination: falling through must not break function SCOPING, which is
# the reason that branch exists at all. `set -e` inside the function must still
# not leak past the closing brace.
scan_raw openerscope.sh '#!/usr/bin/env bash\nset -uo pipefail\nf() {\n  set -e\n  a | head -1\n}\nb | head -1\n'
if [ "$(printf '%s\n' "$OUT" | grep -c .)" = 1 ] && grep -q ':5:' <<<"$OUT"; then
  record 0 "...and function scoping still ends at the closing brace" ""
else
  record 1 "...and function scoping still ends at the closing brace" "$OUT"
fi

echo
echo "== the terminator class ======================================================="
case_flag subst.sh "head closed by ')' inside a command substitution" '  x="$(cmd | head)"'

echo
echo "== '||' IS NOT A PIPE (the three reviewer-found false positives) =============="
case_spare orgrep.sh  "|| grep -q is not a pipe"  '  helm template . >/dev/null || grep -q needle <<<"$out"'
case_spare orhead.sh  "|| head is not a pipe"     '  check_it || head -5 <<<"$captured" >&2'
case_spare orgrepm.sh "|| grep -m1 is not a pipe" '  probe || grep -m1 needle <<<"$out"'
case_spare span.sh    "a plain '| grep' does not borrow a later '|| grep -q'" \
  '  producer | grep needle && cmd || grep -q x <<<"$y"'
case_spare spanm.sh   "nor a later '|| grep -m1'" \
  '  producer | grep needle && cmd || grep -m1 x <<<"$y"'
# The same span WITHIN ONE SEGMENT — no `&&`, no `;`. Splitting the line on those
# subsumed the two cases above, so the \001 boundary in the grep classes stopped
# being exercised by them and their mutations came back UNCAUGHT. These are what
# pin it now.
case_spare span1seg.sh  "a plain '| grep' does not borrow a later '|| grep -q' in ONE segment" \
  '  producer | grep needle || grep -q x <<<"$y"'
case_spare span1segm.sh "nor a '|| grep -m1' in one segment" \
  '  producer | grep needle || grep -m1 x <<<"$y"'
# The discriminations that keep the five above from being satisfied by a scanner
# that simply never fires on a line containing `||`.
case_flag mixed.sh    "but a REAL pipe on a line that also contains '||' still fires" \
  '  x=$(producer | head -1) || warn "no first line"'
case_flag glued.sh    "and head glued to '||' still fires (producer | head||die)" \
  '  producer | head||die'
case_flag gluedamp.sh "the same for '|& head||die'" '  noisy |& head||die'
case_flag spanreal.sh "and a real '| grep -q' on the spanning shape still fires" \
  '  producer | grep -q needle && cmd || fallback'
case_spare gluedtrue.sh "while 'head||true' stays spared" '  producer | head||true'
case_spare gluedcolon.sh "and 'head|| :' likewise"       '  producer | head|| :'

# A DISCARDED STATUS COVERS ITS OWN PIPELINE ONLY. `|| true` used to spare the
# WHOLE line, so a live hazard sharing the line was skipped (Bugbot,
# .github#300). Segments are judged separately now.
case_flag segamp.sh  "'foo || true && producer | grep -q x' — the 2nd pipeline is live" \
  '  foo || true && producer | grep -q x'
case_flag segsemi.sh "'foo || true; producer | head -1' likewise" \
  '  foo || true; producer | head -1'
# The discriminations: the real `|| true` idiom must STILL be spared, in both
# the bare and the command-substitution form, or the two above would just mean
# "the spare stopped working".
case_spare segspare.sh "and a segment that DOES end in '|| true' is still spared" \
  '  producer | head -1 || true'
case_spare segsubst.sh "including the command-substitution form" \
  '  ver="$(tool --version | head -1 || true)"'
case_spare segboth.sh "both segments spared when both discard their status" \
  '  a | head -1 || true && b | grep -q x || :'
# THE SPARE IS ANCHORED TO THE END OF THE SEGMENT, and this is what pins that.
# A `|| true` buried mid-segment discards the status of the command it follows,
# not of the pipeline that comes after it: here the `grep -q` status is live.
# An unanchored spare would match the `|| true` inside the substitution and skip
# the line — which is what the old whole-line rule did.
case_flag segmid.sh "'|| true' inside a substitution does not spare a live pipe after it" \
  '  printf %s "$(get || true)" | grep -q needle'
# Asad's table (.github#300), verbatim: the whitespace before `;` used to decide
# the verdict, so two lines doing the same thing disagreed. All four spellings
# now read the same.
case_flag segsp1.sh "'|| true ;' with a space before the semicolon" \
  '  rm -f x || true ; producer | head -1'
case_flag segsp2.sh "'|| : ;' likewise" \
  '  rm -f x || : ; producer | head -1'
# ...and the FALSE POSITIVE the simpler end-anchored regex would have cost.
# Segment-wise evaluation is what avoids it, so it belongs in the suite as the
# reason that approach was chosen over the cheaper one.
case_spare segcost.sh "a spared pipeline followed by another command stays spared" \
  '  producer | head -1 || true; echo done'
case_spare segparen.sh "and the parenthesised form stays spared" \
  '  ( producer | head -1 || true )'

echo
echo "== trailing comments, quote-aware ============================================="
# The segment/hazard path ran on the RAW line, so a trailing comment broke the
# end-anchored spare and prose could read as a pipe (Bugbot, .github#300).
case_spare cmtspare.sh "a trailing comment does not break the '|| true' anchor" \
  '  producer | head -1 || true   # explains why'
case_spare cmtprose.sh "prose in a TRAILING comment is not a pipe" \
  '  do_thing   # NOT producer | head -1, see above'
# THE DISCRIMINATION, and the reason the strip is quote-aware rather than a
# regex: `sub(/[[:space:]]*#.*$/, ...)` would also cut a `#` living inside a
# string and lose the real hazard after it — trading two false positives for a
# false NEGATIVE, the wrong direction.
case_flag cmtinstr.sh "a '#' inside a double-quoted string does not hide a hazard" \
  '  x="a # b"; producer | head -1'
case_flag cmtinstr2.sh "nor inside a single-quoted one" \
  "  x='a # b'; producer | grep -q z"
case_spare cmtmarker.sh "and the allow marker still opts a line out" \
  '  producer | head -1   # pipefail-guard: allow'
# A `#` NOT preceded by whitespace is not a comment. Without that condition the
# strip would cut at the `#` in `x=a#b` and lose the hazard after it.
case_flag cmtnospace.sh "a '#' with no preceding space is not a comment" \
  '  x=a#b; producer | head -1'
# And a whole-line comment still yields nothing — via the strip now, not via a
# separate guard, which the mutation showed had become redundant.
case_spare cmtwhole.sh "a whole-line comment is still not the hazard" \
  '  # NOT producer | head -1'

echo
echo "== state does not leak between files =========================================="
scan_hazardous a-bad.sh '  x="$(ls /tmp | head -1)"'
printf '#!/usr/bin/env bash\nset -uo pipefail\n  x="$(ls /tmp | head -1)"\n' > "$WORK/b-good.sh"
OUT=$(awk -f "$SCANNER_ABS" "$WORK/a-bad.sh" "$WORK/b-good.sh")
if grep -q 'a-bad.sh' <<<"$OUT" && ! grep -q 'b-good.sh' <<<"$OUT"; then
  record 0 "a clean file after a dirty one stays clean" ""
else
  record 1 "a clean file after a dirty one stays clean" "$OUT"
fi

echo
echo "== INHERITANCE: a library runs under its sourcer's options ===================="
# The reason this gate needs a wrapper at all. A lib that sets nothing is not
# safe; it is whatever its caller is. Driven through the real entry point,
# because the awk alone cannot know.
mk_repo() {  # $1 = repo dir ; $2 = the sourcer's set-line
  local r="$1" setline="$2"
  mkdir -p "$r/scripts/lib"
  printf '#!/usr/bin/env bash\n%s\nsource "${LIB_DIR}/worker.sh"\n' "$setline" > "$r/scripts/caller.sh"
  printf 'work() {\n  producer | head -1\n}\n' > "$r/scripts/lib/worker.sh"
  ( cd "$r" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f )
}
mk_repo "$WORK/inh" "set -euo pipefail"
OUT=$(PIPEFAIL_ROOT="$WORK/inh" bash "$GATE_ABS"); RC=$?
if [ "$RC" = 1 ] && grep -q 'worker.sh' <<<"$OUT"; then
  record 0 "a lib sourced by a hazardous script IS flagged (inheritance)" ""
else
  record 1 "a lib sourced by a hazardous script IS flagged (inheritance)" "rc=$RC out=$OUT"
fi

# TRANSITIVE, which is why the closure is a FIXPOINT and not one pass. Found by
# mutation: replacing the loop's exit condition with a bare `break` — i.e. one
# iteration only — left the depth-1 case above green, so the loop itself was
# unpinned. Real installers are deeper than one level (`install.sh` sources
# `common.sh`, which sources `log.sh`), and depth is exactly what a single pass
# gets wrong.
mkdir -p "$WORK/deep/scripts/lib"
printf '#!/usr/bin/env bash\nset -euo pipefail\nsource "${LIB_DIR}/mid.sh"\n' > "$WORK/deep/scripts/caller.sh"
printf 'source "${LIB_DIR}/leaf.sh"\n' > "$WORK/deep/scripts/lib/mid.sh"
printf 'work() {\n  producer | head -1\n}\n' > "$WORK/deep/scripts/lib/leaf.sh"
( cd "$WORK/deep" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f )
OUT=$(PIPEFAIL_ROOT="$WORK/deep" bash "$GATE_ABS"); RC=$?
if [ "$RC" = 1 ] && grep -q 'leaf.sh' <<<"$OUT"; then
  record 0 "...and TRANSITIVELY, two levels down (the fixpoint, not one pass)" ""
else
  record 1 "...and TRANSITIVELY, two levels down (the fixpoint, not one pass)" "rc=$RC out=$OUT"
fi

# THE SOURCE LINE'S SPELLING MUST NOT MATTER EITHER. A basename-only quoted
# target, `source "worker.sh"`, kept its opening quote through the extractor --
# there is no slash for `s|.*/||` to strip, and the path form only worked
# because that substitution removed the quote by accident (Bugbot, .github#300).
mk_repo_src() {  # $1 = repo dir ; $2 = the literal source line
  local r="$1" srcline="$2"
  mkdir -p "$r/scripts/lib"
  printf '#!/usr/bin/env bash\nset -euo pipefail\n%s\n' "$srcline" > "$r/scripts/caller.sh"
  printf 'work() {\n  producer | head -1\n}\n' > "$r/scripts/lib/worker.sh"
  ( cd "$r" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f )
}
while IFS= read -r srcline; do
  [ -n "$srcline" ] || continue
  rm -rf "$WORK/srcspell"; mk_repo_src "$WORK/srcspell" "$srcline"
  OUT=$(PIPEFAIL_ROOT="$WORK/srcspell" bash "$GATE_ABS"); RC=$?
  if [ "$RC" = 1 ] && grep -q 'worker.sh' <<<"$OUT"; then
    record 0 "...inherited through: $srcline" ""
  else
    record 1 "...inherited through: $srcline" "rc=$RC out=$OUT"
  fi
done <<'SPELLINGS'
source "${LIB_DIR}/worker.sh"
source "worker.sh"
source scripts/lib/worker.sh
. "worker.sh"
source "$(dirname "$0")/lib/worker.sh"
source "${LIB_DIR}"/worker.sh
SPELLINGS

# A PATH WITH A SPACE must not silently drop out. `haz` was a space-separated
# string iterated unquoted, so such a file split into two nonexistent paths,
# failed `[ -f ]`, and was never marked hazardous — fail-open (Asad,
# .github#300). No repo has one today; that is not a property.
rm -rf "$WORK/spaced"; mkdir -p "$WORK/spaced/my scripts/lib"
printf '#!/usr/bin/env bash\nset -euo pipefail\nsource "${LIB_DIR}/worker.sh"\n' > "$WORK/spaced/my scripts/caller.sh"
printf 'work() {\n  producer | head -1\n}\n' > "$WORK/spaced/my scripts/lib/worker.sh"
( cd "$WORK/spaced" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f )
OUT=$(PIPEFAIL_ROOT="$WORK/spaced" bash "$GATE_ABS"); RC=$?
if [ "$RC" = 1 ] && grep -q 'worker.sh' <<<"$OUT"; then
  record 0 "a hazardous file under a path WITH A SPACE still propagates" ""
else
  record 1 "a hazardous file under a path WITH A SPACE still propagates" "rc=$RC out=$OUT"
fi

# A TRAILING COMMENT MUST NOT SEED. `.*` lets the option cluster sit anywhere on
# the line, which also let a comment mentioning the flag satisfy the seed. Fail-
# CLOSED, so never dangerous — fixed so the two halves of one rule agree, since
# the awk already strips comments (Asad, .github#300).
rm -rf "$WORK/cmt"; mkdir -p "$WORK/cmt/scripts/lib"
printf '#!/usr/bin/env bash\nset -e   # we deliberately do NOT use -o pipefail here\nsource "${LIB_DIR}/worker.sh"\n' > "$WORK/cmt/scripts/caller.sh"
printf 'work() {\n  producer | head -1\n}\n' > "$WORK/cmt/scripts/lib/worker.sh"
( cd "$WORK/cmt" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f )
OUT=$(PIPEFAIL_ROOT="$WORK/cmt" bash "$GATE_ABS"); RC=$?
if [ "$RC" = 0 ]; then
  record 0 "a comment naming the flag does not seed the file" ""
else
  record 1 "a comment naming the flag does not seed the file" "rc=$RC out=$OUT"
fi

# THE SOURCER'S SPELLING MUST NOT MATTER. `set -eu -o pipefail` is an ordinary
# way to write it, and the seed used to require the option to be the FIRST
# cluster after `set` — so a split-form script's libraries were never marked
# inherited (Bugbot, .github#300). The awk got the direct file right either
# way, which is exactly why this needs a wrapper-level case: the bug was
# invisible to a scanner-level test.
for spelling in 'set -eu -o pipefail' 'set -e -o pipefail' 'set -o errexit -o pipefail'; do
  rm -rf "$WORK/spell"; mk_repo "$WORK/spell" "$spelling"
  OUT=$(PIPEFAIL_ROOT="$WORK/spell" bash "$GATE_ABS"); RC=$?
  if [ "$RC" = 1 ] && grep -q 'worker.sh' <<<"$OUT"; then
    record 0 "...inherited through the split form: $spelling" ""
  else
    record 1 "...inherited through the split form: $spelling" "rc=$RC out=$OUT"
  fi
done

# The SIGN of the sourcer's options is what decides it — the mirror case, and
# the one that proves the tests above are not just "any lib is flagged".
mk_repo "$WORK/inhoff" "set -eu
set +o pipefail"
OUT=$(PIPEFAIL_ROOT="$WORK/inhoff" bash "$GATE_ABS"); RC=$?
if [ "$RC" = 0 ] && [ -z "$OUT" ]; then
  record 0 "...and a pipefail-OFF sourcer does not make its libs hazardous" ""
else
  record 1 "...and a pipefail-OFF sourcer does not make its libs hazardous" "rc=$RC out=$OUT"
fi

echo
echo "== the file list is DERIVED, and the gate fails closed ========================"
# A shebang-only file with no extension must be picked up: a private `find
# -name '*.sh'` missed exactly this shape in client (docker/k3s-cuda/build.sh).
mkdir -p "$WORK/derive"
printf '#!/bin/bash\nset -euo pipefail\nx=$(ls | head -1)\n' > "$WORK/derive/build"
chmod +x "$WORK/derive/build"
( cd "$WORK/derive" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f )
OUT=$(PIPEFAIL_ROOT="$WORK/derive" bash "$GATE_ABS"); RC=$?
if [ "$RC" = 1 ] && grep -q 'build' <<<"$OUT"; then
  record 0 "an extensionless file is classified by its shebang" ""
else
  record 1 "an extensionless file is classified by its shebang" "rc=$RC out=$OUT"
fi

# .bats is excluded here exactly as it is in the shellcheck job — a bats file is
# a harness, and its fixtures deliberately contain the hazard as DATA.
mkdir -p "$WORK/batsx"
printf '#!/usr/bin/env bats\nset -euo pipefail\n@test "x" {\n  producer | head -1\n}\n' > "$WORK/batsx/t.bats"
( cd "$WORK/batsx" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f )
OUT=$(PIPEFAIL_ROOT="$WORK/batsx" bash "$GATE_ABS"); RC=$?
if [ "$RC" = 0 ]; then
  record 0 ".bats is out of scope, matching the shellcheck job's classifier" ""
else
  record 1 ".bats is out of scope" "rc=$RC out=$OUT"
fi

OUT=$(PIPEFAIL_ROOT="$WORK" bash "$GATE_ABS" 2>&1); RC=$?
if [ "$RC" = 2 ]; then
  record 0 "an unreadable/non-git tree is 'cannot tell' (exit 2), never green" ""
else
  record 1 "an unreadable/non-git tree is 'cannot tell' (exit 2)" "rc=$RC out=$OUT"
fi

# A readable tree with no shell in it is a legitimate 0 — "nothing in scope" is
# not the same as "nothing checked", and conflating them would make the gate
# unadoptable in the Python and TS repos.
mkdir -p "$WORK/noshell"
printf 'print("hi")\n' > "$WORK/noshell/a.py"
( cd "$WORK/noshell" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f )
OUT=$(PIPEFAIL_ROOT="$WORK/noshell" bash "$GATE_ABS"); RC=$?
if [ "$RC" = 0 ]; then
  record 0 "a repo with no shell files is a clean 0, not a failure" ""
else
  record 1 "a repo with no shell files is a clean 0" "rc=$RC out=$OUT"
fi

# A CRASHING SCANNER IS NOT A CLEAN TREE. The wrapper runs without errexit, so
# a failing `awk` does not stop it; testing only "was the output empty" read a
# crash as clean and exited 0 (Bugbot, .github#300). This drives the real gate
# with a corrupted scanner via SCANNER-path override.
cp "$SCANNER_ABS" "$WORK/broken.awk"
printf 'this is not valid awk {{{\n' > "$WORK/broken.awk"
mkdir -p "$WORK/crash/scripts"
cp "$GATE_ABS" "$WORK/crash/scripts/pipefail-early-close.sh"
cp "$WORK/broken.awk" "$WORK/crash/scripts/pipefail-early-close.awk"
printf '#!/usr/bin/env bash\nset -euo pipefail\nx=$(ls | head -1)\n' > "$WORK/crash/run.sh"
( cd "$WORK/crash" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f )
OUT=$(PIPEFAIL_ROOT="$WORK/crash" bash "$WORK/crash/scripts/pipefail-early-close.sh" 2>&1); RC=$?
if [ "$RC" = 2 ]; then
  record 0 "a scanner that CRASHES is exit 2, never a clean tree" ""
else
  record 1 "a scanner that CRASHES is exit 2, never a clean tree" "rc=$RC out=$OUT"
fi

echo
echo "== YAML \`run:\` BLOCKS ARE IN SCOPE (backend#2967) ============================"
# THE HOLE THIS TICKET FOUND. The wrapper classifies a file as shell by
# extension, else by shebang; workflow YAML has neither, so every `run:` block
# in the fleet was out of scope and the gate reported SUCCESS on
# `e2e-test-agent@f4d6fec`. Handed that file explicitly, the scanner flagged the
# offending line correctly -- so the LINE GRAMMAR was never the hole. The
# ticket's suspicion (a matcher too narrow for `head -1`) is FALSE, and the four
# spellings are pinned in the requirement block above so the claim stays
# measured.
#
# These cases drive the REAL gate over a REAL git tree, because scope is the
# wrapper's job and the awk alone cannot know.
#
# FIXTURES ARE ONE-LINE printf FORMATS for the reason documented at the top of
# this file, with one addition: `%` must be written `%%`, since the fixture
# bodies contain `printf '%s\n'`. A literal fixture indented into YAML would put
# `set -euo pipefail` behind nothing but whitespace, and the scanner's dispatch
# is `^[[:space:]]*set` -- it would read this suite's own options off a fixture.
scan_yaml() {  # $1 = name ; $2 = printf FORMAT for the workflow file
  local d="$WORK/y-$1"
  rm -rf "$d"; mkdir -p "$d/.github/workflows"
  # shellcheck disable=SC2059  # $2 IS the format, by contract
  printf "$2" > "$d/.github/workflows/w.yml"
  ( cd "$d" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f ) >/dev/null
  # STDERR IS KEPT SEPARATE, never folded into OUT. Under `2>&1`, gawk's
  # long-standing warning about the `\"` in the `|| true` spare regex
  # (pipefail-early-close.awk:237 -- byte-identical on develop, so not from
  # this change) landed in OUT, and every "spares" assertion then saw non-empty
  # output and failed. macOS awk emits no such warning, which is why it passed
  # locally. An assertion about FINDINGS must read the findings stream only.
  OUT=$(PIPEFAIL_ROOT="$d" bash "$GATE_ABS" 2>"$WORK/yerr"); RC=$?
  ERRTXT=$(cat "$WORK/yerr" 2>/dev/null)
}
yaml_flag()  { # $1 name ; $2 desc ; $3 format
  scan_yaml "$1" "$3"
  if [ "$RC" = 1 ] && [ -n "$OUT" ]; then record 0 "$2" ""; else record 1 "$2" "rc=$RC out=$OUT err=$ERRTXT"; fi
}
yaml_spare() { # $1 name ; $2 desc ; $3 format
  scan_yaml "$1" "$3"
  if [ "$RC" = 0 ] && [ -z "$OUT" ]; then record 0 "$2" ""; else record 1 "$2" "rc=$RC out=$OUT err=$ERRTXT"; fi
}

# f4d6fec's LITERAL line, in f4d6fec's shape: a `shell: bash` step. This is the
# regression case the ticket asks for.
F4D='name: j\non:\n  push:\njobs:\n  journey:\n    runs-on: ubuntu-latest\n    steps:\n      - name: Record what the chart decided about telemetry\n        shell: bash\n        run: |\n          set -uo pipefail\n          CM=$(printf "%%s\\\\n" "$CM_RAW" | head -1)\n'
yaml_flag f4d6fec "f4d6fec's literal 'printf | head -1' in a 'shell: bash' step IS flagged" "$F4D"

# THE LINE NUMBER MUST POINT AT THE REAL FILE. A fragment offset reported as a
# source line sends the reviewer to the wrong place and the annotation lands on
# the wrong row -- which is how a finding gets dismissed as noise. Line 12 is
# the `CM=$(...)` line of the fixture above.
scan_yaml f4d6fec "$F4D"
if grep -q '^\.github/workflows/w\.yml:12: ' <<<"$OUT"; then
  record 0 "...and reported at the source line, not the fragment offset" ""
else
  record 1 "...and reported at the source line, not the fragment offset" "$OUT"
fi

# 720b952, the FIXED head: capture-then-slice, plus a COMMENT that still names
# the old hazard. The ticket requires this to stay green -- that is what makes
# the case above derived rather than a restatement of one line of text.
yaml_spare fixedhead "720b952's capture-then-slice fix is spared, comment and all" \
  'name: j\non:\n  push:\njobs:\n  journey:\n    runs-on: ubuntu-latest\n    steps:\n      - shell: bash\n        run: |\n          set -uo pipefail\n          # This was `printf "%%s\\\\n" "$CM_RAW" | head -1`, which SIGPIPEs.\n          CM="${CM_RAW%%%%$"\\\\n"*}"\n'

echo
echo "-- the effective shell is DERIVED from the YAML, not assumed ------------------"
# GitHub's contract, and the whole reason f4d6fec was hazardous while its
# neighbours were not: `shell: bash` is the only keyword that turns pipefail ON.
#   shell: bash   -> bash --noprofile --norc -eo pipefail {0}
#   shell: sh     -> sh -e {0}
#   (absent)      -> bash -e {0}, falling back to sh -e {0}
# Both halves are asserted. A scanner that armed every run block would pass the
# flagging cases and fail these; one that armed none would do the reverse.
HAZ_BODY='run: |\n          x=$(printf "%%s" "$Y" | head -1)\n'
yaml_flag shbash  "'shell: bash' arms errexit AND pipefail" \
  "name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - shell: bash\n        $HAZ_BODY"
yaml_spare shsh   "'shell: sh' is errexit-only -- no pipefail, so not the hazard" \
  "name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - shell: sh\n        $HAZ_BODY"
yaml_spare shnone "no 'shell:' at all is 'bash -e {0}' -- errexit-only, spared" \
  "name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - $HAZ_BODY"
# ...and the default shell's block can still arm pipefail ITSELF. This is the
# shape three of tracebloc/.github's own findings have, so it is not academic:
# the synthesised `set -e` must not stop the body's own `set -euo pipefail`
# from counting.
yaml_flag shnoneset "a default-shell block that sets '-euo pipefail' itself IS flagged" \
  'name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          set -euo pipefail\n          x=$(printf "%%s" "$Y" | head -1)\n'
# A CUSTOM COMMAND LINE IS USED VERBATIM, so GitHub adds no `-e`. Assuming
# every `bash …` means errexit would invent hazards; the flags have to be read.
yaml_spare shcustom "a custom 'bash -x {0}' gets NO implicit -e, so it is spared" \
  "name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - shell: bash -x {0}\n        $HAZ_BODY"
yaml_flag shcustomeo "...but a custom 'bash -eo pipefail {0}' IS armed" \
  "name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - shell: bash -eo pipefail {0}\n        $HAZ_BODY"
# THE FLAGS ARE READ, NOT TOPPED UP. `bash -x {0}` above is spared whether or
# not an `-e` is injected, because it has no pipefail either way -- so it
# cannot tell "flags parsed" from "flags parsed plus a helpful -e". This one
# can: pipefail is present and errexit is not, so ANY injected `-e` arms both
# options and the case reddens. Found by the mutation surviving.
yaml_spare shcustomo "a custom 'bash -o pipefail {0}' has pipefail but NO errexit" \
  "name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - shell: bash -o pipefail {0}\n        $HAZ_BODY"
yaml_spare shpython "'shell: python' is not a POSIX shell and carries no pipefail" \
  "name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - shell: python\n        $HAZ_BODY"

echo
echo "-- 'defaults.run.shell' applies, at both levels -------------------------------"
# A step with no `shell:` inherits the job's default, and failing over to the
# workflow's. Miss either layer and a whole repo's worth of blocks read as
# errexit-only.
yaml_flag defjob "a job-level 'defaults.run.shell: bash' arms its steps" \
  "name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    defaults:\n      run:\n        shell: bash\n    steps:\n      - $HAZ_BODY"
yaml_flag defwf "a workflow-level 'defaults.run.shell: bash' does too" \
  "name: j\non:\n  push:\ndefaults:\n  run:\n    shell: bash\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - $HAZ_BODY"
# The discrimination: without it, the two above are satisfied by a scanner that
# arms every block regardless of what any `shell:` says.
yaml_spare defsh "...and a 'defaults.run.shell: sh' does NOT arm them" \
  "name: j\non:\n  push:\ndefaults:\n  run:\n    shell: sh\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - $HAZ_BODY"

echo
echo "-- composite actions, and single-line 'run:' ----------------------------------"
# The ticket asks specifically about composite actions: their steps live under
# `runs.steps`, not `jobs.*.steps`, so a walker that only knows about workflows
# sees none of them.
scan_yaml_action() {  # $1 = name ; $2 = format
  local d="$WORK/ya-$1"
  rm -rf "$d"; mkdir -p "$d/.github/actions/thing"
  # shellcheck disable=SC2059
  printf "$2" > "$d/.github/actions/thing/action.yml"
  ( cd "$d" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f ) >/dev/null
  OUT=$(PIPEFAIL_ROOT="$d" bash "$GATE_ABS" 2>"$WORK/yerr"); RC=$?
  ERRTXT=$(cat "$WORK/yerr" 2>/dev/null)
}
scan_yaml_action composite \
  'name: t\ndescription: d\nruns:\n  using: composite\n  steps:\n    - shell: bash\n      run: |\n        x=$(printf "%%s" "$Y" | head -1)\n'
if [ "$RC" = 1 ] && grep -q 'action.yml' <<<"$OUT"; then
  record 0 "a composite action's 'runs.steps' run block is in scope" ""
else
  record 1 "a composite action's 'runs.steps' run block is in scope" "rc=$RC out=$OUT"
fi
# A SINGLE-LINE `run:` is not a block scalar, and taking `raw[1:]` of it yields
# nothing -- so the block would be silently skipped.
yaml_flag oneline "a single-line 'run:' (no block scalar) is scanned too" \
  'name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - shell: bash\n        run: x=$(printf "%%s" "$Y" | head -1)\n'
# And a YAML with no run blocks at all contributes nothing -- "nothing in scope"
# is not "nothing checked", or the gate would be unadoptable.
yaml_spare norun "a workflow with no 'run:' blocks is a clean 0" \
  'name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v5\n'

echo
echo "-- the YAML path FAILS CLOSED -------------------------------------------------"
# rule 3: "cannot tell" is a finding. Unparseable YAML is the case that matters,
# because the alternative -- skip what will not parse -- means any repo with one
# broken workflow silently loses coverage of ALL of them.
rm -rf "$WORK/ybad"; mkdir -p "$WORK/ybad/.github/workflows"
printf 'jobs:\n  j:\n  steps: [ unclosed\n   bad: : :\n' > "$WORK/ybad/.github/workflows/w.yml"
( cd "$WORK/ybad" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f ) >/dev/null
OUT=$(PIPEFAIL_ROOT="$WORK/ybad" bash "$GATE_ABS" 2>&1); RC=$?
if [ "$RC" = 2 ]; then
  record 0 "unparseable YAML is exit 2, never a clean tree" ""
else
  record 1 "unparseable YAML is exit 2" "rc=$RC out=$OUT"
fi

# A MISSING EXTRACTOR IS ALSO 'CANNOT TELL'. Driven through a copied tree so the
# real gate runs with the real sibling absent -- the shape that made the
# workflow's rc-whitelist necessary in the first place (127 read as clean).
rm -rf "$WORK/ynoext"; mkdir -p "$WORK/ynoext/scripts" "$WORK/ynoext/.github/workflows"
cp "$GATE_ABS" "$WORK/ynoext/scripts/pipefail-early-close.sh"
cp "$SCANNER_ABS" "$WORK/ynoext/scripts/pipefail-early-close.awk"
printf 'name: j\non:\n  push:\njobs:\n  j:\n    steps:\n      - shell: bash\n        run: echo hi\n' \
  > "$WORK/ynoext/.github/workflows/w.yml"
( cd "$WORK/ynoext" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f ) >/dev/null
OUT=$(PIPEFAIL_ROOT="$WORK/ynoext" bash "$WORK/ynoext/scripts/pipefail-early-close.sh" 2>&1); RC=$?
if [ "$RC" = 2 ]; then
  record 0 "a missing YAML extractor is exit 2, never a clean tree" ""
else
  record 1 "a missing YAML extractor is exit 2" "rc=$RC out=$OUT"
fi

# A NON-ZERO EXTRACTOR IS FATAL EVEN WHEN IT LEFT A MANIFEST BEHIND. The
# unparseable-YAML case above does not reach this: the extractor returns before
# writing anything, so the missing-manifest guard catches it and the exit-status
# check is never consulted -- which is exactly why its mutation survived. The
# shape that needs the status check is a crash PART WAY THROUGH: a manifest on
# disk, fewer fragments in it than the tree really has, and a clean-looking
# scan of the ones that made it. Driven with a stub extractor, because no real
# input produces that.
rm -rf "$WORK/ystub"; mkdir -p "$WORK/ystub/scripts" "$WORK/ystub/.github/workflows"
cp "$GATE_ABS" "$WORK/ystub/scripts/pipefail-early-close.sh"
cp "$SCANNER_ABS" "$WORK/ystub/scripts/pipefail-early-close.awk"
printf '#!/usr/bin/env python3\nimport os, sys\nd = sys.argv[sys.argv.index("--out") + 1]\nos.makedirs(d, exist_ok=True)\nopen(os.path.join(d, "manifest.tsv"), "w").close()\nsys.exit(1)\n' \
  > "$WORK/ystub/scripts/pipefail-early-close-yaml.py"
printf 'name: j\non:\n  push:\njobs:\n  j:\n    steps:\n      - shell: bash\n        run: echo hi\n' \
  > "$WORK/ystub/.github/workflows/w.yml"
( cd "$WORK/ystub" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f ) >/dev/null
OUT=$(PIPEFAIL_ROOT="$WORK/ystub" bash "$WORK/ystub/scripts/pipefail-early-close.sh" 2>&1); RC=$?
if [ "$RC" = 2 ]; then
  record 0 "an extractor that fails AFTER writing a manifest is exit 2" ""
else
  record 1 "an extractor that fails AFTER writing a manifest is exit 2" "rc=$RC out=$OUT"
fi

# An unrecognised PIPEFAIL_SCOPE must refuse rather than quietly narrow the run.
OUT=$(PIPEFAIL_SCOPE=shel PIPEFAIL_ROOT="$WORK/noshell" bash "$GATE_ABS" 2>&1); RC=$?
if [ "$RC" = 2 ]; then
  record 0 "an unknown PIPEFAIL_SCOPE is exit 2, not a narrowed scan" ""
else
  record 1 "an unknown PIPEFAIL_SCOPE is exit 2" "rc=$RC out=$OUT"
fi

# THE SCOPES MUST ACTUALLY DIFFER, or every YAML case above could be satisfied
# by a wrapper that ignores PIPEFAIL_SCOPE entirely.
rm -rf "$WORK/yboth"; mkdir -p "$WORK/yboth/.github/workflows"
printf '#!/usr/bin/env bash\nset -euo pipefail\nx=$(ls | head -1)\n' > "$WORK/yboth/s.sh"
printf 'name: j\non:\n  push:\njobs:\n  j:\n    steps:\n      - shell: bash\n        run: |\n          y=$(ls | head -1)\n' \
  > "$WORK/yboth/.github/workflows/w.yml"
( cd "$WORK/yboth" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f ) >/dev/null
A=$(PIPEFAIL_SCOPE=shell PIPEFAIL_ROOT="$WORK/yboth" bash "$GATE_ABS")
B=$(PIPEFAIL_SCOPE=yaml  PIPEFAIL_ROOT="$WORK/yboth" bash "$GATE_ABS")
C=$(PIPEFAIL_SCOPE=all   PIPEFAIL_ROOT="$WORK/yboth" bash "$GATE_ABS")
if grep -q 's\.sh:3:' <<<"$A" && ! grep -q 'w\.yml' <<<"$A" \
   && grep -q 'w\.yml:9:' <<<"$B" && ! grep -q 's\.sh' <<<"$B" \
   && grep -q 's\.sh:3:' <<<"$C" && grep -q 'w\.yml:9:' <<<"$C"; then
  record 0 "scope=shell, scope=yaml and scope=all each report their own half" ""
else
  record 1 "the three scopes must differ" "shell=[$A] yaml=[$B] all=[$C]"
fi

echo
echo "== the gate reports on THIS repo =============================================="
# SHELL SCOPE ONLY, and that is a statement about a BACKLOG, not a loophole.
# Bringing YAML into scope surfaced 8 pre-existing instances in this repo's own
# workflows (jq/`--version` producers, none of them reachable at today's data
# sizes -- the same profile as the 19 instances backend#2264 converted before
# arming). Converting them touches live board automation and belongs in its own
# PR, so the fleet-facing job reports YAML findings at WARNING level for now
# (`yaml-run-blocks-soft-fail`, the migration shape action-pins already uses).
# Arming a red gate is what trains people to skip the tier (rule 4).
OUT=$(PIPEFAIL_SCOPE=shell bash "$GATE_ABS"); RC=$?
if [ "$RC" = 0 ]; then
  record 0 "tracebloc/.github is itself clean under the SHELL rule" ""
else
  record 1 "tracebloc/.github is itself clean under the SHELL rule" "$OUT"
fi

# ...and the YAML scan must reach a VERDICT here, not an error. This is the
# honest claim while the backlog stands: rc 2 would mean the extractor cannot
# read this repo's own workflows, which is a different and much worse fact than
# "there are findings". Asserting `rc = 0` instead would be a restated
# expectation that goes stale the moment the backlog is cleared.
OUT=$(PIPEFAIL_SCOPE=yaml bash "$GATE_ABS" 2>&1); RC=$?
if [ "$RC" = 0 ] || [ "$RC" = 1 ]; then
  record 0 "the YAML scan reaches a verdict on this repo's own workflows (rc $RC)" ""
else
  record 1 "the YAML scan reaches a verdict on this repo's own workflows" "rc=$RC out=$OUT"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] || exit 1

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] || exit 1
