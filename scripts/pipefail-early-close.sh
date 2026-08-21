#!/usr/bin/env bash
# =============================================================================
#  pipefail-early-close.sh — the org-wide early-close gate (backend#2264).
#
#  Resolves WHICH files run under errexit+pipefail, then hands them to
#  pipefail-early-close.awk, which decides which LINES are offenders.
#
#  THE CLASS. Under `set -o pipefail` AND `set -e`, `producer | head -n N` (or
#  `| grep -q`, `| grep -m N`) aborts its own caller once the producer outgrows
#  the ~64KB pipe buffer: the reader closes, the producer takes SIGPIPE, the
#  pipeline returns 141, errexit kills the script. It is SIZE-dependent, which
#  is why instances survive review — measured, 50 lines exit 0 and 20k exit 141.
#  Two incidents in `client` (client#656, client#678) before it was encoded.
#
#  HISTORY, because it bears on how much to trust this file. `client` carried
#  the only copy for a month. Arming it fleet-wide (backend#2264) required
#  converting 19 real instances across six repos first — and the measurement
#  that mattered was that NONE of them was reachable today: a single-line
#  producer cannot SIGPIPE a `grep -q` at all, because grep must read to
#  end-of-line before it can report a match. This gate earns its keep by
#  stopping the NEXT instance, not by the backlog it cleared.
#
#  WHY A WRAPPER AND NOT JUST THE AWK
#  ----------------------------------
#  1. INHERITED OPTIONS. A library that sets neither option still RUNS under
#     both when its sourcer sets them. An awk that only asks "does this file
#     contain both `set` lines" reads every `lib/*.sh` as safe — which in
#     `client` was most of the installer. Inheritance is closed over `source`
#     / `.` to a FIXPOINT, matched on BASENAME because the source line is
#     usually `source "${LIB_DIR}/common.sh"` and only the basename is
#     statically known. Basename matching is the fail-closed direction: it can
#     mark a same-named file that is never actually sourced (a spurious finding
#     someone can silence with the marker), never the reverse.
#
#     `s|^"||` is not redundant with `s|.*/||`. A BASENAME-ONLY quoted target,
#     `source "worker.sh"`, has no slash for the first substitution to strip, so
#     the opening quote survived into the comparison and the lib was never
#     marked inherited. The path form only worked because `s|.*/||` removed the
#     quote BY ACCIDENT along with the directory (Bugbot, .github#300). Four
#     spellings are covered and asserted in the selftest: quoted-with-path,
#     quoted-basename, bare, and `.` in place of `source`.
#
#  2. THE FILE LIST MUST BE DERIVED. A hand-kept list drifts; a private `find`
#     missed `docker/k3s-cuda/build.sh` — which sets `set -euo pipefail` — and
#     every `.bash` file. The classifier here (extension, else shebang) is
#     deliberately the SAME rule the `shellcheck` job in code-quality.yml
#     applies, so the two jobs cannot disagree about what "a shell file" is.
#
#  FAILS CLOSED (backend#1729 rule 3). An unreadable tree exits 2, not 0 —
#  "cannot tell" is a finding, never a pass. A tree that is readable and simply
#  contains no shell files is a legitimate 0: nothing in scope is not the same
#  as nothing checked.
#
#  Usage:  pipefail-early-close.sh [file...]      (default: the whole repo)
#          PIPEFAIL_ROOT=<dir>                    (default: $PWD)
#  Output: one `path:line: code` per offender. Exit 0 clean / 1 offenders /
#          2 cannot tell. Reporting and judging are separate on purpose: the
#          workflow decides whether findings block, via `soft-fail`.
# =============================================================================
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
AWK_PROG="$HERE/pipefail-early-close.awk"
ROOT="${PIPEFAIL_ROOT:-$PWD}"

[ -r "$AWK_PROG" ] || { echo "pipefail-early-close: cannot read $AWK_PROG" >&2; exit 2; }
cd "$ROOT" || { echo "pipefail-early-close: cannot enter $ROOT" >&2; exit 2; }

files=()
if [ "$#" -gt 0 ]; then
  files=("$@")
else
  # Enumerate TRACKED files only. An untracked build artefact is not this
  # repo's shell, and `git ls-files` is what every other whole-tree gate here
  # uses, so the scopes agree.
  cand=$(git ls-files 2>/dev/null) || {
    echo "pipefail-early-close: 'git ls-files' failed in $ROOT — refusing to report clean" >&2
    exit 2
  }
  [ -n "$cand" ] || {
    echo "pipefail-early-close: no tracked files in $ROOT — refusing to report clean" >&2
    exit 2
  }
  # Extension, else shebang — the same classifier the shellcheck job applies.
  # `.bats`/`.ps1`/`.psm1`/`.zsh` are excluded there and excluded here: a bats
  # file is a test harness, not a script this rule governs, and PowerShell has
  # no pipefail.
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    case "$f" in
      *.sh|*.bash|*.ksh) files+=("$f") ;;
      *.bats|*.ps1|*.psm1|*.zsh) ;;
      *) head -n 1 "$f" 2>/dev/null \
           | grep -Eq '^#![[:space:]]*[^[:space:]]*(/|[[:space:]])(ba|da|k)?sh([[:space:]]|$)' \
           && files+=("$f") ;;
    esac
  done <<EOF
$cand
EOF
  if [ "${#files[@]}" -eq 0 ]; then
    echo "pipefail-early-close: no shell files in scope."
    exit 0
  fi
fi

# --- seed: files that enable BOTH options themselves ------------------------
# COMMENTS ARE STRIPPED FIRST. `.*` lets the option cluster sit anywhere on the
# line (so the split `set -eu -o pipefail` seeds), but it also let a TRAILING
# COMMENT satisfy the test:
#     set -e   # then -o pipefail elsewhere
# That direction is fail-CLOSED -- a file gets scanned that need not be -- so it
# was never dangerous. It is fixed anyway so that the two halves of one rule
# agree (Asad, .github#300).
#
# AN EARLIER VERSION OF THIS COMMENT SAID "the awk already strips comments
# before deciding". THAT WAS FALSE, and it was quoted back approvingly in
# review before anyone checked it. The awk stripped comments only for
# FUNCTION-BODY detection; `apply_set` ran on the raw line, so a `# … +e …`
# comment disarmed errexit and silently skipped every hazard below it. Fixed in
# the awk, in `apply_set`. Recorded because a comment asserting what ANOTHER
# file does is a claim to verify, not to quote -- which is the whole subject of
# this rule family.
#
# The mirror of this in e2e-test-agent#184 ran the same way: a trailing comment
# DISARMING a flag check.
#
# `haz` IS AN ARRAY. As a space-separated string iterated unquoted, a path
# containing a space split into two nonexistent paths, both failed `[ -f ]`, and
# the file was silently never marked hazardous -- fail-open, in the half this
# wrapper exists for. No repo has such a path today; that is not a property.
#
# EXPANDED AS `${haz[@]+"${haz[@]}"}`, NOT `"${haz[@]}"`. bash 3.2 -- still the
# /bin/bash on every macOS -- treats an EMPTY array's `[@]` as unbound under
# `set -u` and aborts. The runners are bash 5, where it is fine, so this would
# have been green in CI and broken for every developer running the suite
# locally. Caught only because the suite was run on macOS.
haz=()
for f in "${files[@]}"; do
  [ -f "$f" ] || continue
  setlines=$(sed -E 's/#.*$//' "$f" 2>/dev/null | grep -E '^[[:space:]]*set[[:space:]]') || continue
  grep -qE '(-[a-zA-Z]*e[a-zA-Z]*([[:space:]]|$)|-o[[:space:]]+errexit)' <<<"$setlines" || continue
  # The SIGN is load-bearing: the `-` is required, so `set +o pipefail` cannot
  # satisfy it.
  grep -qE '\-[a-zA-Z]*o[[:space:]]+pipefail' <<<"$setlines" || continue
  haz+=("$f")
done

# Basenames of every file this one sources.
#
# QUOTES ARE STRIPPED BEFORE EXTRACTION rather than tolerated inside the
# pattern. The previous single regex handled `source "${LIB_DIR}/x.sh"` and
# missed both `source "$(dirname "$0")/lib.sh"` and `source "${DIR}"/file.sh`,
# because an embedded quote ended the match early -- the fail-OPEN direction, in
# the half this wrapper exists for (Asad, .github#300). Strip the quotes, then
# take any path-like token ending in .sh/.bash; `(` and `)` are excluded from
# the token so a `$(dirname …)` prefix ends the token rather than swallowing it.
sourced_basenames() {  # $1 = file
  sed -E 's/#.*$//; s/["'"'"']//g' "$1" 2>/dev/null \
    | grep -E '(^|[[:space:]])(source|\.)[[:space:]]' \
    | grep -oE '[^[:space:];|&()]+\.(sh|bash)' \
    | sed -E 's|.*/||' \
    | sort -u
}

# --- close over `source` / `.` to a fixpoint --------------------------------
# A file sourced BY a hazardous file runs under the caller's options.
while :; do
  added=0
  for f in ${haz[@]+"${haz[@]}"}; do
    [ -f "$f" ] || continue
    while IFS= read -r base; do
      [ -n "$base" ] || continue
      for cand_f in "${files[@]}"; do
        [ "$(basename "$cand_f")" = "$base" ] || continue
        seen=0
        for h in ${haz[@]+"${haz[@]}"}; do [ "$h" = "$cand_f" ] && { seen=1; break; }; done
        [ "$seen" = 1 ] && continue
        haz+=("$cand_f"); added=1
      done
    done < <(sourced_basenames "$f")
  done
  [ "$added" -eq 0 ] && break
done

out=$(awk -v hazardous="${haz[*]+${haz[*]}} " -f "$AWK_PROG" "${files[@]}")
awk_rc=$?
if [ "$awk_rc" -ne 0 ]; then
  echo "pipefail-early-close: the scanner exited $awk_rc — refusing to report clean" >&2
  exit 2
fi
if [ -n "$out" ]; then
  printf '%s\n' "$out"
  exit 1
fi
exit 0
