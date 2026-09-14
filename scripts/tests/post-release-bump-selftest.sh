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
# workflow used to have was a GLOB:
#
#     case "${TAG#v}" in [0-9]*.[0-9]*.[0-9]*) ;; *) error ;; esac
#
# and `1.2.3-rc.1` SATISFIES it -- `*` matches the `-rc.1` suffix too. So dropping
# the prerelease field without adding an explicit prerelease test would not have
# failed loudly; it would have bumped the version on every rc tag the train cuts,
# which is once per hop per repo. A silently-too-permissive glob is exactly the kind
# of thing a reader confirms by eye and gets wrong.
#
# The glob was equally permissive in the other direction: `1.2.3.4`, `1.2.3rc1` and
# `1.2.3-hotfix` all satisfied it too and reached `version_file.py cmp`, which
# refused them naming the VERSION FILE rather than the tag. The workflow now uses
# the org's existing stable rule -- ^v[0-9]+\.[0-9]+\.[0-9]+$, as spelled by cli's
# release.yml and the train's promote-repo.sh -- with `*-*` as the prerelease arm,
# so the prerelease decision and the shape decision are ONE `case`. This file
# asserts all three of its outcomes (review of PR .github#474).
#
# THE LOGIC IS READ OUT OF THE WORKFLOW, NOT RETYPED HERE. A selftest that carries
# its own copy of the `case` statement passes forever while the workflow drifts --
# the inert-verification shape backend#1729 catalogued. The block is extracted from
# the YAML by anchor, so editing the workflow either changes what this runs or
# breaks the extraction; it cannot quietly diverge.
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

# --- extract THE decision from the workflow ----------------------------------
#
# Anchored on the literal `case` subject rather than on a line number, which moves.
# The extraction is REQUIRED to find something: a silent empty extraction would make
# every assertion below pass against an empty function.
tag_case="$(awk '/^ *case "\$TAG" in/{f=1} f{print} f&&/^ *esac/{exit}' "$WF")"
[ -n "$tag_case" ] || { echo "FATAL: could not extract the \`case \"\$TAG\"\` block from $WF"; exit 1; }

# AND IT IS REQUIRED TO STILL CONTAIN ALL THREE OUTCOMES. Extraction succeeding is
# not the same as extracting the right thing: a rewrite that kept the `case` subject
# but lost an arm would extract cleanly and then be scored against whatever was
# left. Each arm is asserted by the text the assertions below actually depend on.
for _needle in '*-*)' '::notice::' '::error::release tag' 'exit 0' 'exit 1'; do
  grep -qF -- "$_needle" <<<"$tag_case" || {
    echo "FATAL: the extracted block no longer contains '$_needle'. Either an arm was"
    echo "       removed or the refusal text changed; verdict() below keys on it."
    exit 1
  }
done

# --- every variable the block interpolates must be BOUND, and the list is DERIVED --
#
# THE FALSE GREEN THIS CLOSES (review of PR .github#474). `verdict()` used to call
# any non-zero exit a `refuse`. `set -u` aborting the subshell on an unbound
# variable is also non-zero, so adding a `$REPO_FULL` to the refusal message made
# three assertions pass on an "unbound variable" abort instead of on the shape test
# -- and they would have kept passing with the shape test deleted. Binding one more
# variable by hand closed one instance; this closes the CLASS, by reading the
# references out of the extracted block and refusing any that nothing here binds.
BOUND="TAG VERSION_FILE BASE BUMP REPO_FULL"
_unbound=""
for _v in $(grep -oE '\$\{?[A-Za-z_][A-Za-z_0-9]*' <<<"$tag_case" | tr -d '${' | sort -u); do
  case " $BOUND " in
    *" $_v "*) ;;
    *) _unbound="$_unbound $_v" ;;
  esac
done
[ -z "$_unbound" ] || {
  echo "FATAL: the extracted block interpolates variable(s) this suite does not bind:$_unbound"
  echo "       Under \`set -u\` those abort the subshell, which is non-zero and would be"
  echo "       misread as a refusal. Add them to BOUND (with a realistic value) or stop"
  echo "       interpolating them in the workflow."
  exit 1
}

# The workflow's own arms call `echo ::notice`/`::error` and `exit`. Run them in a
# subshell and read BOTH the exit code and the output: the code alone cannot tell a
# refusal from a broken block, which is the whole point of the paragraph above.
verdict() { # tag -> "skip" | "accept" | "refuse" | "broken"
  local TAG="$1" out rc
  out=$(
    # THE SAME FLAGS THE WORKFLOW'S `run:` SETS. `-e` matters: without it a failing
    # command inside an arm would abort in production and sail past here.
    set -euo pipefail
    TAG="$TAG"
    # Read by `eval "$tag_case"` below, which shellcheck cannot follow. The BOUND
    # list above is what keeps this set honest -- it is derived from the block, so
    # a binding that stops being needed is dead weight, not a silent hazard.
    # shellcheck disable=SC2034
    { VERSION_FILE="package.json"; BASE="develop"; BUMP="patch"; REPO_FULL="tracebloc/design-system"; }
    eval "$tag_case"
    echo "__ACCEPTED__"
  ) 2>&1 ; rc=$?
  if [ "$rc" -ne 0 ]; then
    # A REFUSAL IS THE SPECIFIC MESSAGE, NOT MERELY A NON-ZERO EXIT.
    if grep -qF '::error::release tag' <<<"$out"; then echo refuse
    else echo broken
    fi
  elif grep -qF '__ACCEPTED__' <<<"$out"; then echo accept
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

# --- a prerelease consumes nothing: GREEN no-op, never an error ---------------
#
# THE REGRESSION THIS FILE EXISTS FOR. Each of these satisfied the old
# `[0-9]*.[0-9]*.[0-9]*` glob, so without the prerelease arm every one of them would
# read as `accept` and bump the version on a tag that consumed none.
is "v1.2.3-rc.1 is skipped, not bumped"   skip v1.2.3-rc.1
is "v1.2.3-rc.12 is skipped"              skip v1.2.3-rc.12
is "v1.9.115-rc.1 is skipped (client's real rc shape)" skip v1.9.115-rc.1
is "v2.0.0-rc1 is skipped (no dot after rc)" skip v2.0.0-rc1
is "v1.2.3-beta.1 is skipped (any suffix, not just rc)" skip v1.2.3-beta.1
is "v1.2.3-hotfix is skipped"             skip v1.2.3-hotfix

# --- anything that is not a version at all is REFUSED, loudly -----------------
#
# Asserted so the prerelease arm above cannot quietly widen into "skip everything I
# don't recognise" -- which would turn a malformed tag into a green no-op and lose
# the bump with no signal. The last three are the ones the old glob let through to
# `version_file.py`, where the refusal named the version file instead of the tag.
is "a non-version tag is refused"      refuse vlatest
is "a two-part tag is refused"         refuse v1.2
is "an empty tag is refused"           refuse ""
is "an unprefixed 1.2.3 is refused"    refuse 1.2.3
is "a four-part v1.2.3.4 is refused"   refuse v1.2.3.4
is "v1.2.3rc1 is refused, not skipped (no separator to read)" refuse v1.2.3rc1

printf '\npost-release-bump: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
