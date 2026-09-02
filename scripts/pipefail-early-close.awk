# pipefail-early-close.awk — find pipelines that can SIGPIPE their own producer
# and abort the script (backend#1778).
#
# THE CLASS
# ---------
# Under `set -o pipefail` AND `set -e`, a diagnostic like `producer | head -n N`
# aborts its own caller once the producer's output outgrows the ~64KB pipe
# buffer: head closes the pipe, the producer takes SIGPIPE, the pipeline returns
# 141, and errexit kills the script — usually skipping the cleanup and the
# intended exit code that followed.
#
# It is SIZE-DEPENDENT, which is why instances survive review. Measured on the
# client#656 case: 50 matching lines exit 1 (fits the buffer, no signal), 20k
# exit 141.
#
# The house idiom is a here-string (or capture-then-slice), never a pipe into an
# early-closing reader:
#     head -25 <<<"$captured"          # no pipe, nothing to SIGPIPE
#     first="${out%%$'\n'*}"           # pure-bash slicing
#
# OPTIONS ARE POSITIONAL, AND THEY GET TURNED OFF TOO (Asad on #763)
# ------------------------------------------------------------------
# An earlier version asked only "does this file ENABLE both options" and so had
# no idea that `set +e` exists. `run_diagnose()` opens with `set +e` precisely so
# no step can abort the bundle, and every line in it was flagged. Best-effort
# regions are a normal idiom here, so that model would need a hand-placed marker
# on each one — which is the opposite of encoding the rule.
#
# So the state is tracked LINE BY LINE: `set -e` / `set +e` / `set -o pipefail` /
# `set +o pipefail` / the combined `set -euo pipefail` all move it, and a line is
# an offender only if BOTH options are live where it sits.
#
# Function scoping is an approximation, deliberately. bash does NOT scope shell
# options to functions — a `set +e` inside one leaks to the caller — but treating
# it as restored at the function's closing brace is the conservative direction
# for a linter: it keeps asking about later code instead of going quiet after the
# first best-effort helper.
#
# `hazardous` (from pipefail-early-close.sh) seeds files that inherit both
# options from a sourcing script; those start with the state already on.
#
# KNOWN LIMITATION -- a `set` line inside a MULTI-LINE QUOTED STRING is read as
# this file's options rather than as fixture data. tracebloc/.github's
# house-rules-selftest.sh passes whole scripts as quoted arguments:
#
#     expect "a bare curl fires both rules" \
#     '#!/bin/bash
#     set -euo pipefail
#     curl -fsSL "$url" -o out' curl-timeout,curl-tls
#
# so the scanner sees errexit enabled in a file whose real options are
# `set -uo pipefail`, and reports a line that carries no hazard. Counting quotes
# to find such regions was tried and REJECTED: apostrophes in prose ("the file's
# options") desynchronise the count within a few lines of the top of that very
# file, and a desynchronised count HIDES real offenders -- strictly worse than
# reporting a false one. Use `# pipefail-guard: allow` on the affected line until
# the scanner can lex shell properly (backend#2264). The behaviour is PINNED by a
# test, so changing it is deliberate rather than silent.
#
# Usage:  awk -v hazardous="<paths>" -f pipefail-early-close.awk FILE...
# Output: one `path:line: code` per offender.

# Strip a TRAILING comment, quote-aware.
#
# A regex cannot do this. `sub(/[[:space:]]*#.*$/, ...)` also cuts a `#` that
# lives inside a string, and `x="a # b"; producer | head -1` then loses its real
# hazard -- trading two false positives for a false negative, which is the wrong
# direction. So: walk the line, track single/double quote state, and cut only at
# a `#` that is OUTSIDE quotes and preceded by whitespace (or starts the line).
#
# Needed because the segment/hazard path ran on the RAW line, so
#   producer | head -1 || true   # explains why
# no longer looked end-anchored and was reported, and prose in a trailing
# comment could read as a pipe (Bugbot, .github#300). `apply_set` already
# stripped; this path did not -- the same asymmetry, third occurrence.
function strip_trailing_comment(s,   i, ch, inq, ind, n) {
  inq = 0; ind = 0
  n = length(s)
  for (i = 1; i <= n; i++) {
    ch = substr(s, i, 1)
    if (ch == "\"" && inq == 0)      { ind = 1 - ind }
    else if (ch == "'" && ind == 0)  { inq = 1 - inq }
    else if (ch == "#" && inq == 0 && ind == 0) {
      if (i == 1 || substr(s, i - 1, 1) ~ /[[:space:]]/) return substr(s, 1, i - 1)
    }
  }
  return s
}

function apply_set(line,   n, a, i, tok, sign, flags) {
  # STRIP THE TRAILING COMMENT FIRST. This ran on the RAW line, so whitespace
  # splitting turned comment tokens into real option updates:
  #     set -euo pipefail  # note: +e would be bad
  # cleared errexit, and every hazard below that line was then skipped.
  # Fail-OPEN on the positional state machine, and silent (Bugbot and Asad,
  # independently, .github#300).
  #
  # It lives HERE rather than at the dispatch because this is the function with
  # the hole; the `^[[:space:]]*#` skip further down only handles comments that
  # are the WHOLE line, and the `bare` strip above is for function-body
  # detection only. Unconditional is safe: the only legitimate `#` on a `set`
  # line is inside a positional argument (`set -- "a#b"`), and the loop below
  # ignores any token not starting with `-` or `+`.
  line = strip_trailing_comment(line)
  # SPLIT ON `;` AS WELL AS WHITESPACE. `set -euo pipefail; cd /tmp` tokenised
  # as `pipefail;`, which never equals `pipefail`, so the `-o` handler missed it
  # and the file ran with p_on=0 -- every hazard in it skipped, not just the one
  # on that line. Found while fixing the fall-through above (Asad, .github#300).
  n = split(line, a, /[[:space:];]+/)
  for (i = 1; i <= n; i++) {
    if (a[i] == "set") break
  }
  for (i = i + 1; i <= n; i++) {
    tok = a[i]
    if (tok !~ /^[-+]/) continue
    sign = substr(tok, 1, 1)
    flags = substr(tok, 2)
    # `-o <name>` / `+o <name>` — the option name is the NEXT token. BOTH long
    # forms are handled: errexit used to fall through entirely, because only
    # `pipefail` was matched here and `-o` carries no `e` for the short-flag
    # branch below. A file doing `set -o pipefail; set -o errexit` therefore had
    # p_on=1, e_on=0 and every hazard in it was skipped — the matcher was
    # spelling-sensitive, and asymmetric with the pipefail handling right beside
    # it (Saqlain on #763).
    if (flags ~ /o$/ && (i + 1) <= n) {
      if (a[i + 1] == "pipefail")     p_on = (sign == "-") ? 1 : 0
      else if (a[i + 1] == "errexit") e_on = (sign == "-") ? 1 : 0
    }
    # `e` anywhere in a combined short flag (-e, -eu, -euo, +e …).
    if (flags ~ /e/) e_on = (sign == "-") ? 1 : 0
  }
}

function inherits(f) {
  return hazardous != "" && index(" " hazardous " ", " " f " ") > 0
}

FNR == 1 {
  curfile = FILENAME
  # A file that inherits both options from its sourcer starts with them live.
  e_on = inherits(curfile) ? 1 : 0
  p_on = inherits(curfile) ? 1 : 0
  in_fn = 0; save_e = 0; save_p = 0
}

{
  line = $0

  # Function boundaries, so a best-effort region ends with its function.
  #
  # A ONE-LINE function (`first() { cmd | head -1; }`) must NOT be skipped: an
  # unconditional `next` here meant its body was never checked at all, and this
  # repo writes helpers that way (Bugbot #763). The one-liner opens and closes on
  # the same line, so the surrounding state is unchanged — fall through and let
  # the hazard check read the body.
  if (line ~ /^[a-zA-Z_][a-zA-Z0-9_:.-]*[[:space:]]*\(\)[[:space:]]*\{/) {
    # Test self-closing against the line WITHOUT a trailing comment: a compact
    # helper written `f() { cmd | head -1; }   # why` is still a one-liner, and
    # requiring `}` to be the last character read it as a multi-line opener,
    # `next`ed, and skipped the body — the shape the gate was just taught to
    # catch (Bugbot #763).
    bare = line
    sub(/[[:space:]]*#.*$/, "", bare)
    if (bare !~ /\}[[:space:]]*$/) {
      # A MULTI-LINE OPENER CAN CARRY CODE, so record the scope and FALL THROUGH
      # rather than `next`. `f() { producer | head -1` with the `}` further down
      # matched here, failed the self-closing test, and skipped its own hazard --
      # fail-open (Bugbot, .github#300). This is the SAME defect as the one-liner
      # case documented above, one shape over: that fix removed an unconditional
      # `next` and left a conditional one on the branch beside it. Changing half
      # of a paired construct, again.
      save_e = e_on; save_p = p_on; in_fn = 1
    }
  }
  if (line ~ /^\}/ && in_fn) { e_on = save_e; p_on = save_p; in_fn = 0; next }

  # APPLY THE OPTIONS AND FALL THROUGH -- do not `next`. An unconditional `next`
  # here meant the rest of the PHYSICAL LINE was never judged:
  #     set -euo pipefail; producer | head -1
  # enabled both options and then skipped its own hazard. Fail-open on the
  # gate's core dispatch (Asad, .github#300), and the third `next`-shaped miss
  # on this file after the one-liner and multi-line function openers.
  #
  # Falling through is safe *because* of the segmentation below: `set -euo
  # pipefail` becomes its own segment, carries no pipe, and cannot produce a
  # false positive. And `set +e; producer | head -1` still spares correctly,
  # because e_on is already 0 by the time the second segment is judged.
  if (line ~ /^[[:space:]]*set[[:space:]]/) apply_set(line)

  if (line ~ /#[[:space:]]*pipefail-guard:[[:space:]]*allow/) next
  # (No separate full-line-comment skip. `strip_trailing_comment` reduces a
  # whole-line comment to the empty string, so the segment scan below finds
  # nothing -- the old `^[[:space:]]*#` guard became redundant the moment that
  # helper was introduced, and its mutation SURVIVED, which is how dead code
  # announces itself. Removed rather than annotated.)

  if (!(e_on && p_on)) next

  # `||` IS NOT A PIPE, and must be neutralised before the hazard test below.
  # Otherwise the SECOND bar of a `||` reads as a pipeline, and the scanner
  # flags the very form this file recommends:
  #     out=$(producer) || true
  #     grep -q needle <<<"$out"        # fine
  #     cmd || grep -q needle <<<"$out" # was reported as an offender
  # Found while converting the fleet for backend#2264 -- the gate rejected the
  # output of its own remediation, which is how a gate teaches people to
  # disable it. \001 cannot occur in a shell source line, so it is a safe
  # stand-in; the ORIGINAL line is still what gets printed.
  #
  # THE STAND-IN IS ALSO A BOUNDARY, and the `[^|\001]*` class below is what
  # makes it one. The `|` characters of a `||` are not just noise -- they are
  # what STOPS `grep[^|]*` from spanning further down the line. Replacing them
  # with a character the class permitted let a plain `| grep` reach the `-q` of
  # a LATER `|| grep -q`:
  #     producer | grep needle && cmd || grep -q x <<<"$y"
  # flagged again -- the same false positive, one level deeper (Bugbot,
  # client#777). Neutralising the pipe is only half the job; the boundary has to
  # survive. Adding \001 to the class is the fix, and it is the ONLY fix here:
  # shrinking the stand-in to one character was tried, and its mutation survived,
  # so it changed nothing and was reverted.
  # A DISCARDED STATUS NEUTRALISES ITS OWN PIPELINE, NOT THE WHOLE LINE. The
  # `|| true` spare used to match anywhere on the line and `next` out of it, so
  #     foo || true && producer | grep -q x
  # was skipped entirely even though the second pipeline's status is very much
  # live (Bugbot, .github#300). The line is therefore split on `;` and `&&` and
  # each segment judged on its own: spared only if IT ends in `|| true` / `|| :`,
  # flagged if IT contains the hazard. `;` happened to be caught already --
  # the old terminator class did not admit it -- so the live half of this was
  # the `&&` form.
  code = strip_trailing_comment(line)
  nseg = split(code, seg, /;|&&/)
  for (si = 1; si <= nseg; si++) {
    segtext = seg[si]

    # Spared: this segment's own status is discarded. The trailing class admits
    # the command-substitution form, `x="$(cmd | head -1 || true)"`.
    if (segtext ~ /\|\|[[:space:]]*(true|:)[[:space:])\"']*[[:space:]]*$/) continue

  probe = segtext
  gsub(/\|\|/, "\001\001", probe)

  # The hazard: a pipe into a reader that closes early.
  #   `| head`        — closes after N lines
  #   `| grep -q`     — closes on the first match
  #   `| grep -m N`   — closes after N matches
  # `|&` is bash's pipe-both-streams and is a pipe like any other, so the bar is
  # allowed one optional `&`. It is NOT the `|&` of the terminator class below,
  # which is about what may FOLLOW `head`.
  #
  # \001 IS IN THE HEAD TERMINATOR CLASS for the same reason it is in the grep
  # arms' `[^|\001]*`: the stand-in has to read as a BOUNDARY, not as ordinary
  # text. `producer | head||die` leaves `head` followed by \001, and without it
  # in the class the pipe is missed -- a FALSE NEGATIVE this fix introduced and
  # `develop` did not have (Arturo, client#777). The neutralisation and the
  # boundary are one change; doing half of it moves the bug rather than fixing
  # it, which is exactly what happened here twice.
  # The terminator class matters: `head` can be followed by a CLOSING delimiter,
  # not just whitespace or end-of-line. `x="$(cmd | head)"` ends at `)` and then
  # `"`, and requiring space-or-EOL missed exactly that shape (Bugbot #763).
  #
  # `sed q` AND `| read` ARE MEMBERS TOO, and both were missed (backend#2967).
  # Measured, 20k lines vs 200 through `bash -eo pipefail`:
  #     head -1 / head -n1 / head -n 1 / head   0  -> 141   (already caught)
  #     grep -q / grep -m 1  (matching early)   0  -> 141   (already caught)
  #     sed q / sed 1q                          0  -> 141   NOT caught
  #     read -r l                               0  -> 141   NOT caught
  #     grep -c / sed -n 1p / tail -1 / sort     0  ->   0   correctly spared
  # The last row is the discrimination and is asserted: a reader that runs to
  # EOF is not this class, and an arm that flagged it would be reporting on
  # `| sort`.
  #
  # THE SED SCRIPT TOKEN IS MATCHED EXACTLY, `[0-9]*q` optionally quoted, not
  # `sed[^|]*q`. The loose form flags `sed 's/a/q/'` -- a substitution that
  # reads to EOF -- and a gate that reports on ordinary `sed` gets switched off.
  # `sed -n 1p` has no `q` and stays spared, which is what pins the difference.
  #
  # `read` MUST SIT DIRECTLY AFTER THE BAR. `producer | while read -r l` is the
  # opposite of this class: the loop reads to EOF, so nothing SIGPIPEs. Only a
  # bare `| read` closes early, and requiring `read` to be the first token is
  # what separates them.
  if (probe ~ /\|&?[[:space:]]*head([[:space:]]|$|[)"'\''`;|&\001])/ \
      || probe ~ /\|&?[[:space:]]*grep[^|]*[[:space:]]-[a-zA-Z]*q/ \
      || probe ~ /\|&?[[:space:]]*grep[^|\001]*[[:space:]]-[a-zA-Z]*m[[:space:]]*[0-9]/ \
      || probe ~ /\|&?[[:space:]]*sed[[:space:]]+(-[a-zA-Z]+[[:space:]]+)*'?[0-9]*q'?([[:space:]]|$|[)"'\''`;|&\001])/ \
      || probe ~ /\|&?[[:space:]]*read([[:space:]]|$|[)"'\''`;|&\001])/) {
      sub(/^[[:space:]]+/, "", line)
      print curfile ":" FNR ": " line
      break
    }
  }
}
