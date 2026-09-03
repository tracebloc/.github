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
# A SED SUBSTITUTION CONTAINING `q` IS NOT `sed q`. This is what forces the sed
# arm to match the script token exactly (`[0-9]*q`, optionally quoted) instead
# of the cheap `sed[^|]*q`: the loose form reports on ordinary substitutions,
# and a gate that reports on `sed` gets switched off. It cannot go in the
# measured table above, whose harness runs the consumer through word splitting.
case_spare sedsubst.sh "a sed SUBSTITUTION containing q reads to EOF and is spared" \
  "  producer | sed 's/a/q/'"


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
echo "== THE REQUIREMENT, MEASURED: which consumers actually SIGPIPE ================"
# backend#1729 rule 6: mutation-proof is not requirement-proof. Every case above
# compares the scanner against a shape someone typed. This block compares it
# against the RUNTIME, so the expected set is derived rather than restated --
# and it is the only thing here that could have told us `sed q` was missing.
#
# NEVER TEST A LIST AGAINST ITSELF (rule 9's corollary). The consumers below are
# written down independently of the matcher; nothing reads the awk's regexes.
# For each, the pipeline is RUN through `bash -eo pipefail` at both payload
# sizes and the detector is required to AGREE with what the shell did.
#
# BOTH SIZES ARE THE POINT. The class is size-dependent: under ~64KB the reader
# closes and the producer's write has already fit in the pipe buffer, so nothing
# is signalled and the construct looks correct in review. That is why instances
# survive, and why a gate tested only on a small payload proves nothing.
# Measured here, and the numbers are the ticket's: 200 lines (~2.6KB) vs 20000
# (~260KB).
# THE PAYLOADS ARE FILES, NOT ENVIRONMENT VARIABLES, and that is portability
# rather than style. Passed as `BIG=$BIG bash -c ...`, the 260KB payload
# exceeds Linux's MAX_ARG_STRLEN (128KB for a single string), `execve` refuses,
# and bash reports 126 -- so EVERY consumer measured "126 at both sizes", every
# member looked like a non-member, and the suite went 17 red. macOS has no
# per-string cap, so the first version passed locally and failed only in CI.
# See `measure` below for why the fix could not be as simple as piping the file.
awk 'BEGIN { for (i = 0; i < 20000; i++) printf "line%08d\n", i }' > "$WORK/payload-BIG"
awk 'BEGIN { for (i = 0; i < 200;   i++) printf "line%08d\n", i }' > "$WORK/payload-SMALL"

# THE PRODUCER IS `printf`, A BASH BUILTIN, AND SUBSTITUTING ONE CHANGES THE
# MEASUREMENT. An earlier version used `cat "$PAYLOAD" | $C` to dodge the
# environment limit above, which looked equivalent and was not: GNU coreutils
# `cat` reports the failed write as `write error: Broken pipe` and exits 1,
# where BSD `cat` dies on SIGPIPE and yields 141. So on Linux every member
# measured `small=0 big=1` -- neither a member nor a non-member -- and all ten
# reported MEASUREMENT UNCLEAR, while macOS still said 141. The harness has to
# reproduce the CONSTRUCT, not something that resembles it: the real instances
# are `printf ... | head -1`, and printf is a builtin, so the subshell itself
# takes the signal and the pipeline returns 141 on both platforms.
#
# The payload reaches printf through a variable read INSIDE the subshell, so it
# never crosses an execve boundary and MAX_ARG_STRLEN cannot apply.
measure() {  # $1 = consumer ; $2 = SMALL|BIG -> sets MRC
  C="$1" PAYLOAD="$WORK/payload-$2" bash -c \
    'set -eo pipefail; P=$(cat "$PAYLOAD"); printf "%s\n" "$P" | eval "$C" >/dev/null' 2>/dev/null
  MRC=$?
}
# Does the gate flag that same consumer, in a file where both options are live?
detects() {  # $1 = consumer -> sets DOUT
  printf '#!/usr/bin/env bash\nset -euo pipefail\nprintf "%%s" "$X" | %s\n' "$1" \
    > "$WORK/req.sh"
  DOUT=$(awk -f "$SCANNER_ABS" "$WORK/req.sh")
}

# The needle matches the FIRST line on purpose: `grep -q` cannot close early
# until it has matched, so a non-matching needle measures nothing at all. That
# is a real trap -- with `grep -q nomatch` every size returns 1 and the arm
# looks dead.
while IFS= read -r consumer; do
  [ -n "$consumer" ] || continue
  measure "$consumer" SMALL; small_rc=$MRC
  measure "$consumer" BIG;   big_rc=$MRC
  detects "$consumer"

  # A MEMBER OF THE CLASS is defined by behaviour, not by a list: the pipeline
  # SUCCEEDS on the small payload and FAILS on the large one. The flip is the
  # requirement -- under errexit+pipefail any non-zero status aborts the script,
  # which is the damage -- and it is deliberately NOT pinned to 141.
  #
  # 141 IS NOT PORTABLE, and assuming it cost a CI round. The producer here is
  # bash's `printf` builtin: macOS bash 3.2 is killed by SIGPIPE and the
  # pipeline yields 141 with nothing on stderr, while GNU bash 5 reports the
  # EPIPE itself (`printf: write error: Broken pipe`) and yields 1. Same defect,
  # same abort, different number -- so a test that demanded 141 called every
  # member "MEASUREMENT UNCLEAR" on Linux while passing on macOS. The observed
  # code is printed in the case name so the platform difference stays visible
  # instead of being smoothed over.
  if [ "$small_rc" = 0 ] && [ "$big_rc" != 0 ]; then
    hazard=yes
  elif [ "$small_rc" = 0 ] && [ "$big_rc" = 0 ]; then
    hazard=no
  else
    record 1 "MEASUREMENT UNCLEAR for '$consumer'" \
      "small=$small_rc big=$big_rc -- the small payload must succeed; it did not, so the fixture is wrong"
    continue
  fi

  # A MEASURED MEMBER THE SCANNER CANNOT SEE IS REGISTERED, NOT HIDDEN. This is
  # an acknowledged-gap register, and the assertion runs BOTH ways: the row must
  # still be a measured member, and it must still go unflagged. If the scanner
  # ever grows the ability to see it, this case REDDENS and tells you to delete
  # the row -- so the register cannot quietly rot into a lie, which is the usual
  # fate of a documented limitation.
  #
  # `sed '1d;q'` is here because the scanner splits a line on `;` with no quote
  # awareness (deliberately -- that split is what stops a `|| true` on one
  # pipeline sparing the next), so a `;` INSIDE a quoted sed script cuts the
  # script in half and `q` is never seen. Widening the sed arm cannot reach it;
  # only a quote-aware segmenter could, and that is a change to the most
  # heavily pinned function in the scanner. Not attempted here, and not claimed.
  case "$consumer" in
    "sed '1d;q'")
      if [ "$hazard" = yes ] && [ -z "$DOUT" ]; then
        record 0 "'$consumer' is a KNOWN-UNSEEN member (rc $big_rc): ';' inside quotes splits the segment" ""
      else
        record 1 "'$consumer' known-gap register is out of date" \
          "hazard=$hazard detector=$DOUT -- if the scanner now sees it, delete this row"
      fi
      continue ;;

    # THE ACCEPTED FALSE POSITIVES, REGISTERED THE SAME WAY AND FOR THE SAME
    # REASON. The sed arm discriminates on what FOLLOWS the `q`, and inside a
    # sed script that is arbitrary text -- so a `q` in REPLACEMENT text that
    # happens to be followed by a space or a `;` flags, while `'s/a/q/'` and
    # `'y/ab/qz/'` are spared only because their `q` is followed by `/` and `z`.
    # Those two spare rows above therefore agree with the shell by coincidence
    # of the delimiter their examples chose, and nothing pinned the difference:
    # the arm's behaviour on this shape was "an accident of two well-chosen
    # examples" (Shujaat, .github#403).
    #
    # The trade is accepted deliberately -- `/addr/q` is a real member and no
    # regex separates `q`-the-command from `q`-the-replacement-text without
    # knowing where the script's commands end, which needs the quote-aware
    # segmenter the row above also rules out. So these are registered instead of
    # fixed, and the assertion runs BOTH ways like the known-gap row: each must
    # still be measurably SAFE and must still be FLAGGED. If the arm ever learns
    # to spare them, the row REDDENS and tells you to delete it -- which is the
    # only version of an accepted false positive that cannot rot into a lie.
    "sed 's/^/q /'"|'sed "s/x/q y/"'|"sed 's/a/q;b/'")
      if [ "$hazard" = no ] && [ -n "$DOUT" ]; then
        record 0 "'$consumer' is an ACCEPTED FALSE POSITIVE: reads to EOF (rc 0), flagged by the loose sed arm" ""
      else
        record 1 "'$consumer' false-positive register is out of date" \
          "hazard=$hazard detector=$DOUT -- if the arm now spares it, delete this row"
      fi
      continue ;;
  esac

  if [ "$hazard" = yes ] && [ -n "$DOUT" ]; then
    record 0 "'$consumer' fails at 260KB (rc $big_rc), succeeds at 2.6KB -- and is flagged" ""
  elif [ "$hazard" = no ] && [ -z "$DOUT" ]; then
    record 0 "'$consumer' reads to EOF at both sizes (rc 0) -- and is spared" ""
  elif [ "$hazard" = yes ]; then
    record 1 "'$consumer' is a MEASURED hazard the gate does not flag" \
      "small=$small_rc big=$big_rc (the abort is real at 260KB), detector said nothing"
  else
    record 1 "'$consumer' is measurably safe but the gate flags it" \
      "small=$small_rc big=$big_rc, detector said: $DOUT"
  fi
done <<'CONSUMERS'
head -1
head -n1
head -n 1
head
sed q
sed 1q
sed -n 2q
sed -e q
sed '1d;q'
sed 's/a/q/'
sed 'y/ab/qz/'
sed 's/^/q /'
sed "s/x/q y/"
sed 's/a/q;b/'
grep -q line00000000
grep -m 1 line00000000
read -r line
IFS= read -r line
grep -c line00000000
sed -n 1p
tail -1
sort
CONSUMERS

# `| while read` IS THE OPPOSITE SHAPE and cannot go through `measure`, whose
# harness runs the consumer as a simple command. The loop reads to EOF, so
# nothing SIGPIPEs -- and an arm matching `read` anywhere after the bar would
# report on it. Measured inline, then asserted against the detector.
# BOTH PRODUCER BEHAVIOURS ARE PINNED HERE, so the platform difference is
# covered wherever this runs instead of only where the platform happens to
# behave one way -- the same lesson as the gawk case further down. Ignoring
# SIGPIPE makes bash report the EPIPE itself rather than die from the signal,
# which is exactly what GNU bash 5 does on the runners: rc 1 with `printf:
# write error: Broken pipe`, where macOS bash 3.2 is killed and yields 141.
# Both must read as a member, because both abort the caller under errexit.
EPIPE_SMALL=0
PAYLOAD="$WORK/payload-SMALL" bash -c \
  'set -eo pipefail; trap "" PIPE; P=$(cat "$PAYLOAD"); printf "%s\n" "$P" | head -1 >/dev/null' \
  2>/dev/null || EPIPE_SMALL=$?
EPIPE_BIG=0
PAYLOAD="$WORK/payload-BIG" bash -c \
  'set -eo pipefail; trap "" PIPE; P=$(cat "$PAYLOAD"); printf "%s\n" "$P" | head -1 >/dev/null' \
  2>"$WORK/epipe-err" || EPIPE_BIG=$?
if [ "$EPIPE_SMALL" = 0 ] && [ "$EPIPE_BIG" != 0 ] \
   && grep -qi "broken pipe" "$WORK/epipe-err"; then
  record 0 "the EPIPE form (GNU bash) also flips: rc $EPIPE_BIG at 260KB, 0 at 2.6KB" ""
else
  record 1 "the EPIPE form (GNU bash) must also flip" \
    "small=$EPIPE_SMALL big=$EPIPE_BIG err=$(cat "$WORK/epipe-err" 2>/dev/null)"
fi

WHILE_RC=0
PAYLOAD="$WORK/payload-BIG" bash -c \
  'set -eo pipefail; P=$(cat "$PAYLOAD"); printf "%s\n" "$P" | while read -r l; do :; done' \
  2>/dev/null || WHILE_RC=$?
detects 'while read -r l; do :; done'
if [ "$WHILE_RC" = 0 ] && [ -z "$DOUT" ]; then
  record 0 "'| while read' reads to EOF at 260KB (rc 0, no flip) -- and is spared" ""
else
  record 1 "'| while read' must be spared" "rc=$WHILE_RC detector=$DOUT"
fi

# AND THE PREFIXED LOOP FORM, which is what makes the widened `read` arm safe
# rather than merely wider. The arm now admits a run of `VAR=value` assignments
# before `read` so that `| IFS= read -r line` is caught (a measured member, in
# CONSUMERS above), and `IFS= read -r` is ALSO the canonical way to write the
# loop -- so this is the exact shape that would break if the prefix were allowed
# to swallow a loop keyword too. `while` is not of the form `NAME=`, so it
# cannot match the prefix; this asserts that rather than trusting it.
WHILE_IFS_RC=0
PAYLOAD="$WORK/payload-BIG" bash -c \
  'set -eo pipefail; P=$(cat "$PAYLOAD"); printf "%s\n" "$P" | while IFS= read -r l; do :; done' \
  2>/dev/null || WHILE_IFS_RC=$?
detects 'while IFS= read -r l; do :; done'
if [ "$WHILE_IFS_RC" = 0 ] && [ -z "$DOUT" ]; then
  record 0 "'| while IFS= read' reads to EOF at 260KB (rc 0, no flip) -- and is spared" ""
else
  record 1 "'| while IFS= read' must be spared" "rc=$WHILE_IFS_RC detector=$DOUT"
fi

echo
echo "== A CLEAN RUN IS SILENT ON STDERR ==========================================="
# THE DEFECT THIS PINS, and it is the same shape as the bug this whole file is
# about: output that is not what the assertion thinks it is.
#
# `pipefail-early-close.awk:237` carried `\"` inside a bracket expression. BSD
# awk (every macOS) accepts it silently; gawk (every ubuntu-latest runner)
# emitted `warning: regexp escape sequence` on EVERY invocation. A helper here
# folded the gate's stderr into its captured output with `2>&1`, so five
# unrelated "is spared" assertions saw non-empty output and failed -- in CI
# only, invisible locally. Two separate fixes came out of it: the helpers now
# keep the streams apart, and THIS asserts the invariant that made the
# confusion possible. A gate with nothing to report reports nothing, on both
# streams; any future warning reddens here, with its text, instead of surfacing
# as a handful of baffling failures somewhere else.
rm -rf "$WORK/quiet"; mkdir -p "$WORK/quiet/.github/workflows"
printf '#!/usr/bin/env bash\nset -euo pipefail\nhead -1 <<<"$x"\n' > "$WORK/quiet/ok.sh"
printf 'name: j\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - shell: bash\n        run: |\n          head -1 <<<"$x"\n' \
  > "$WORK/quiet/.github/workflows/w.yml"
( cd "$WORK/quiet" && git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm f ) >/dev/null

QOUT=$(PIPEFAIL_ROOT="$WORK/quiet" bash "$GATE_ABS" 2>"$WORK/qerr"); QRC=$?
QERR=$(cat "$WORK/qerr" 2>/dev/null)
if [ "$QRC" = 0 ] && [ -z "$QOUT" ] && [ -z "$QERR" ]; then
  record 0 "a clean tree produces no findings AND no stderr ($(awk --version 2>/dev/null | head -1 || awk -Wversion 2>&1 | head -1))" ""
else
  record 1 "a clean tree produces no findings AND no stderr" "rc=$QRC out=$QOUT err=$QERR"
fi

# AND UNDER gawk EXPLICITLY WHERE IT EXISTS, because the whole failure was an
# implementation difference: asserting only against the local awk is what let
# this reach CI. ubuntu-latest has gawk, so CI always takes this branch.
if command -v gawk >/dev/null 2>&1; then
  GQERR=$(PATH="$(dirname "$(command -v gawk)"):$PATH" \
    env AWKPATH= sh -c 'gawk -f "$1" "$2" 2>&1 >/dev/null' _ "$SCANNER_ABS" "$WORK/quiet/ok.sh")
  if [ -z "$GQERR" ]; then
    record 0 "...and gawk parses the scanner without a single warning" ""
  else
    record 1 "...and gawk parses the scanner without a single warning" "$GQERR"
  fi
else
  record 1 "gawk is not installed, so the CI awk cannot be checked here" \
    "install gawk (brew install gawk) -- ubuntu-latest runs gawk, and an awk-specific warning is invisible to BSD awk"
fi

echo
echo "== the gate reports on THIS repo =============================================="
OUT=$(bash "$GATE_ABS"); RC=$?
if [ "$RC" = 0 ]; then
  record 0 "tracebloc/.github is itself clean under the rule" ""
else
  record 1 "tracebloc/.github is itself clean under the rule" "$OUT"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] || exit 1
