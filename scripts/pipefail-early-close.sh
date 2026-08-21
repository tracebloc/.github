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
haz=""
for f in "${files[@]}"; do
  [ -f "$f" ] || continue
  grep -qE '^[[:space:]]*set[[:space:]]+(-[a-zA-Z]*e[a-zA-Z]*([[:space:]]|$)|-o[[:space:]]+errexit)' "$f" 2>/dev/null || continue
  # The SIGN matters: `^set .*pipefail` also matches `set +o pipefail`, so a
  # file that explicitly turns pipefail OFF would be seeded as hazardous.
  # Mirror the sign check the errexit seed above does.
  grep -qE '^[[:space:]]*set[[:space:]]+-[a-zA-Z]*o?[[:space:]]+pipefail|^[[:space:]]*set[[:space:]]+-[a-zA-Z]*o[[:space:]]*$' "$f" 2>/dev/null \
    || grep -qE '^[[:space:]]*set[[:space:]]+-[a-zA-Z]*o[[:space:]]+pipefail' "$f" 2>/dev/null \
    || continue
  haz="$haz $f"
done

# --- close over `source` / `.` to a fixpoint --------------------------------
# A file sourced BY a hazardous file runs under the caller's options.
while :; do
  added=0
  for f in $haz; do
    [ -f "$f" ] || continue
    while IFS= read -r base; do
      [ -n "$base" ] || continue
      for cand_f in "${files[@]}"; do
        [ "$(basename "$cand_f")" = "$base" ] || continue
        case " $haz " in *" $cand_f "*) continue ;; esac
        haz="$haz $cand_f"; added=1
      done
    done <<EOF
$(grep -hoE '(^|[[:space:]])(source|\.)[[:space:]]+"?[^"[:space:];|&]+\.(sh|bash)' "$f" 2>/dev/null \
    | sed -E 's|.*/||; s|^.*[[:space:]]||' | sort -u)
EOF
  done
  [ "$added" -eq 0 ] && break
done

out=$(awk -v hazardous="$haz " -f "$AWK_PROG" "${files[@]}")
if [ -n "$out" ]; then
  printf '%s\n' "$out"
  exit 1
fi
exit 0
