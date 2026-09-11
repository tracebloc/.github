#!/usr/bin/env bash
#
# post-release-bump selftest (backend#3681) -- the TAG DECISION, extracted from the
# workflow and exercised directly.
#
# WHAT THIS PINS. The reusable used to decide "is this a prerelease?" from an event
# FIELD (`github.event.release.prerelease`). backend#3681 moved callers onto
# `on: push: tags: ['v*']`, because the release event reaches exactly one of the six
# repos that declare a version_file -- so the field is gone and the decision is made
# from the TAG TEXT instead.
#
# That swap has one trap, and it is the reason this file exists. The shape test the
# workflow already had is a GLOB:
#
#     case "${TAG#v}" in [0-9]*.[0-9]*.[0-9]*) ;; *) error ;; esac
#
# and `1.2.3-rc.1` SATISFIES it -- `*` matches the `-rc.1` suffix too. So dropping
# the prerelease field without adding an explicit rc test would not have failed
# loudly; it would have bumped the version on every rc tag the train cuts, which is
# once per hop per repo. A silently-too-permissive glob is exactly the kind of thing
# a reader confirms by eye and gets wrong.
#
# THE LOGIC IS READ OUT OF THE WORKFLOW, NOT RETYPED HERE. A selftest that carries
# its own copy of the `case` statements passes forever while the workflow drifts --
# the inert-verification shape backend#1729 catalogued. The two `case` blocks are
# extracted from the YAML by anchor, so editing the workflow either changes what
# this runs or breaks the extraction; it cannot quietly diverge.
#
# Run: bash scripts/tests/post-release-bump-selftest.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WF="$ROOT/.github/workflows/post-release-bump.yml"
[ -f "$WF" ] || { echo "FATAL: $WF not found"; exit 1; }

pass=0
fail=0
ok() { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
no() { printf '  FAIL  %s\n     %s\n' "$1" "$2"; fail=$((fail + 1)); }

# --- extract the two decisions from the workflow -----------------------------
#
# Anchored on the literal `case` subjects rather than on line numbers, which move.
# Both extractions are REQUIRED to find something: a silent empty extraction would
# make every assertion below pass against an empty function.
rc_case="$(awk '/^ *case "\$TAG" in/{f=1} f{print} f&&/^ *esac/{exit}' "$WF")"
[ -n "$rc_case" ] || { echo "FATAL: could not extract the rc \`case \"\$TAG\"\` block from $WF"; exit 1; }

shape_case="$(awk '/^ *case "\$RELEASED" in/{f=1} f{print} f&&/^ *esac/{exit}' "$WF")"
[ -n "$shape_case" ] || { echo "FATAL: could not extract the \`case \"\$RELEASED\"\` shape block from $WF"; exit 1; }

# The workflow's own bodies call `echo ::notice`/`::error` and `exit`. Run them in a
# subshell and report the exit code, which IS the decision: 0 = skip or accept,
# 1 = refuse.
verdict() { # tag -> "skip" | "accept" | "refuse"
  local TAG="$1" out rc
  # VERSION_FILE is set because the workflow's REFUSAL MESSAGE interpolates it.
  # Without it `set -u` aborts the subshell with "unbound variable" -- which is a
  # non-zero exit, so every `refuse` assertion below passed for the wrong reason and
  # would have kept passing with the shape check deleted. Caught on this file's own
  # first run; it is the same false-green shape the header warns about, one layer in.
  out=$(
    set -uo pipefail
    TAG="$TAG"
    VERSION_FILE="package.json"
    BASE="develop"
    eval "$rc_case"
    RELEASED="${TAG#v}"
    eval "$shape_case"
    echo "__ACCEPTED__"
  ) ; rc=$?
  if [ $rc -ne 0 ]; then echo refuse
  elif grep -q '__ACCEPTED__' <<<"$out"; then echo accept
  else echo skip
  fi
}

is() { # desc, expected, tag
  local got; got="$(verdict "$3")"
  if [ "$got" = "$2" ]; then ok "$1"; else no "$1" "expected $2, got $got (tag: $3)"; fi
}

# --- a plain release consumes its version and must bump ----------------------
is "v1.2.3 is accepted"            accept v1.2.3
is "v0.1.0 is accepted (the design-system-v2 case that started this)" accept v0.1.0
is "v10.20.30 is accepted"         accept v10.20.30

# --- an rc consumes nothing: GREEN no-op, never an error ---------------------
#
# THE REGRESSION THIS FILE EXISTS FOR. Each of these satisfies the `[0-9]*.[0-9]*.
# [0-9]*` glob, so without the rc `case` every one of them would read as `accept`.
is "v1.2.3-rc.1 is skipped, not bumped"   skip v1.2.3-rc.1
is "v1.2.3-rc.12 is skipped"              skip v1.2.3-rc.12
is "v1.9.115-rc.1 is skipped (client's real rc shape)" skip v1.9.115-rc.1
is "v2.0.0-rc1 is skipped (no dot after rc)" skip v2.0.0-rc1

# --- anything that is not a version at all is REFUSED, loudly -----------------
#
# Unchanged behaviour, asserted so the rc addition above cannot quietly widen into
# "skip everything I don't recognise" -- which would turn a malformed tag into a
# green no-op and lose the bump with no signal.
is "a non-version tag is refused"      refuse vlatest
is "a two-part tag is refused"         refuse v1.2
is "an empty tag is refused"           refuse ""

printf '\npost-release-bump: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
