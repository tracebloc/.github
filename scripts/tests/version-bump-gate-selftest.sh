#!/usr/bin/env bash
#
# version-bump-gate.yml's decision table, asserted offline with a stubbed `gh`.
#
# WHY THIS EXISTS (tracebloc/backend#1563)
# This gate front-runs the release train's own version preflight
# (release-train scripts/promote-repo.sh:version_preflight). A guard that
# disagrees with the gate it front-runs is worse than no guard - it either
# blocks work the train would happily ship, or greenlights work the train will
# refuse at the prod hop, which is the exact failure backend#1561 cost a release
# leg to diagnose. The two things that can drift are the VERSION PARSE (five
# repos, five file formats, every one of them anchored for a reason) and the
# FAIL-CLOSED paths (where "could not evaluate" quietly becomes "nothing found").
# Both are asserted here, because both are invisible in a diff and obvious in a
# run.
#
# The gate's `run:` block is deliberately env-driven (no `${{ }}` inside), so it
# can be extracted and executed directly. That property is load-bearing for this
# test; if a future edit interpolates an expression into the script body,
# EXTRACTION FAILS LOUDLY here rather than silently testing less.
#
# WHAT A GREEN RUN MEANS
# Every case asserts BOTH the exit status and a distinguishing phrase, because
# several paths share a status while needing to say very different things: a
# docs-only PR and a PR riding somebody else's bump both exit 0, and a caller
# that forgot `publish-paths` and a PR that genuinely changed published files
# both exit 1 under `soft-fail: false`. A test that checked only rc would pass
# while the gate gave the wrong advice.
#
# Run:  bash scripts/tests/version-bump-gate-selftest.sh
set -uo pipefail

GATE=${GATE:-.github/workflows/version-bump-gate.yml}
WORK=$(mktemp -d) || exit 1
trap 'rm -rf "$WORK"' EXIT

# --- extract the run block -------------------------------------------------
if ! python3 - "$GATE" "$WORK/gate.sh" <<'PY'
import sys, re, yaml
gate, out = sys.argv[1], sys.argv[2]
doc = yaml.safe_load(open(gate))
try:
    run = doc['jobs']['version-check']['steps'][0]['run']
except (KeyError, IndexError) as e:
    sys.exit(f"cannot locate jobs.version-check.steps[0].run in {gate}: {e}")
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
GATE_SH="$WORK/gate.sh"

# --- stubs -----------------------------------------------------------------
# `gh` dispatches on the API path and honours --jq, because the gate pipes
# `--jq '.content'` straight into base64 -d. Each endpoint has its own failure
# switch: the whole point of the table below is that a 404, a 500 and a clean
# answer must reach three different messages.
cat > "$WORK/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
url=""; jqexpr=""
while [ $# -gt 0 ]; do
  case "$1" in
    --jq) shift; jqexpr="${1:-}" ;;
    --paginate|api) ;;
    -*) ;;
    *) [ -z "$url" ] && url="$1" ;;
  esac
  shift
done
emit() { if [ -n "$jqexpr" ]; then jq -r "$jqexpr"; else cat; fi; }
case "$url" in
  */compare/*)
    case "${STUB_COMPARE_STATUS:-ok}" in
      500) echo "gh: Internal Server Error (HTTP 500)" >&2; exit 1 ;;
    esac
    emit < "$STUB_COMPARE_FILE" ;;
  */contents/*)
    ref="${url##*ref=}"
    case "$ref" in
      headsha) status="${STUB_HEAD_STATUS:-ok}"; body="$STUB_HEAD_FILE" ;;
      *)       status="${STUB_BASE_STATUS:-ok}"; body="$STUB_BASE_FILE" ;;
    esac
    case "$status" in
      404) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
      500) echo "gh: Internal Server Error (HTTP 500)" >&2; exit 1 ;;
    esac
    emit < "$body" ;;
  */matching-refs/tags/v)
    case "${STUB_TAGS_STATUS:-ok}" in
      500) echo "gh: Internal Server Error (HTTP 500)" >&2; exit 1 ;;
    esac
    emit < "$STUB_TAGS_FILE" ;;
  *)
    echo "stub gh: unexpected call: $url" >&2; exit 1 ;;
esac
STUB
chmod +x "$WORK/gh"

# The gate retries an unreadable version file 3x with `sleep 3` between tries -
# correct against the Contents API's replication lag, and 6 wasted seconds per
# fail-closed case here. Stubbed to a no-op so the table stays fast; the retry
# LOOP is still exercised, only its patience is not.
printf '#!/usr/bin/env bash\nexit 0\n' > "$WORK/sleep"
chmod +x "$WORK/sleep"
export PATH="$WORK:$PATH"

pass=0; fail=0

# --- fixture builders ------------------------------------------------------
# mkcompare <path>[:<previous_path>] ...   - one arg per file in the delta
mkcompare() {
  python3 - "$WORK/compare.json" "$@" <<'PY'
import json, sys
out, args = sys.argv[1], sys.argv[2:]
files = []
for a in args:
    if ":" in a:
        new, prev = a.split(":", 1)
        files.append({"filename": new, "previous_filename": prev})
    else:
        files.append({"filename": a})
json.dump({"merge_base_commit": {"sha": "basesha"}, "files": files}, open(out, "w"))
PY
  export STUB_COMPARE_FILE="$WORK/compare.json"
}

# mkcompare_raw <json>  - for the shapes a builder cannot express (no merge
# base, no file list, a delta sitting exactly on the API's 300-file page cap)
mkcompare_raw() {
  printf '%s' "$1" > "$WORK/compare.json"
  export STUB_COMPARE_FILE="$WORK/compare.json"
}

mkcompare_at_cap() {
  python3 - "$WORK/compare.json" <<'PY'
import json, sys
files = [{"filename": f"docs/page-{i}.md"} for i in range(300)]
json.dump({"merge_base_commit": {"sha": "basesha"}, "files": files}, open(sys.argv[1], "w"))
PY
  export STUB_COMPARE_FILE="$WORK/compare.json"
}

# mkversion <head|base> <file contents>
mkversion() {
  python3 - "$WORK/$1.json" "$2" <<'PY'
import base64, json, sys
out, text = sys.argv[1], sys.argv[2]
json.dump({"content": base64.b64encode(text.encode()).decode()}, open(out, "w"))
PY
  case "$1" in
    head) export STUB_HEAD_FILE="$WORK/head.json" ;;
    base) export STUB_BASE_FILE="$WORK/base.json" ;;
  esac
}

# mktags <version> ...   (bare versions; the 'v' prefix is added here)
mktags() {
  python3 - "$WORK/tags.json" "$@" <<'PY'
import json, sys
out, vers = sys.argv[1], sys.argv[2:]
json.dump([{"ref": f"refs/tags/v{v}"} for v in vers], open(out, "w"))
PY
  export STUB_TAGS_FILE="$WORK/tags.json"
}

# check <name> <expected_rc> <expected_substring>
check() {
  local name=$1 want_rc=$2 want_txt=$3 out rc ok=1
  out=$(cd "${CASE_DIR:-$WORK}" && bash "$GATE_SH" 2>&1); rc=$?
  [ "$rc" = "$want_rc" ] || ok=0
  grep -qF -- "$want_txt" <<<"$out" || ok=0
  if [ "$ok" = 1 ]; then
    printf '  ok   %s\n' "$name"; pass=$((pass + 1))
  else
    printf '  FAIL %s\n' "$name"
    printf '         rc=%s (want %s)\n' "$rc" "$want_rc"
    printf '         want text: %s\n' "$want_txt"
    printf '         got: %s\n' "$(head -4 <<<"$out" | tr '\n' ' ')"
    fail=$((fail + 1))
  fi
}

# The py-package shape from backend#1561, which is the case this gate exists for:
# pyproject.toml pinned at 0.17.0 on both sides, v0.17.0 already released, and a
# delta squarely inside `tracebloc/*`.
base() {
  export REPO_FULL=tracebloc/tracebloc-py-package
  export VERSION_FILE=pyproject.toml
  export PUBLISH_PATHS='tracebloc/* pyproject.toml'
  # Reset like the rest: an exclusion leaking from a previous case would make a
  # later one pass for the wrong reason, which is the failure mode this whole
  # file exists to catch.
  export EXCLUDE_PATHS=''
  export SOFT_FAIL=false
  export PR_BASE_SHA=basesha PR_HEAD_SHA=headsha PR_HEAD_REF=feat/some-work
  export PR_BASE_REF=develop IS_FORK=false
  export LABELS='[]'
  export GITHUB_STEP_SUMMARY="$WORK/summary.md"
  : > "$WORK/summary.md"
  # Reset to "ok" rather than `unset`: a case below overrides one of these with a
  # bare `STUB_TAGS_STATUS=500`, and a bare assignment to an UNSET name is not
  # exported, so the stub would never see it and the case would silently assert
  # the happy path instead. Keeping the names exported keeps the overrides real.
  export STUB_COMPARE_STATUS=ok STUB_HEAD_STATUS=ok STUB_BASE_STATUS=ok STUB_TAGS_STATUS=ok
  CASE_DIR="$WORK"
  mkcompare "tracebloc/_linking.py" "docs/readme.md"
  mkversion head '[project]
name = "tracebloc"
version = "0.17.0"
'
  mkversion base '[project]
name = "tracebloc"
version = "0.17.0"
'
  mktags 0.16.0 0.17.0
}

echo "the backend#1561 case - published files changed under a released version"
base
check "an unbumped published change is refused, and names the file to edit" 1 "Bump pyproject.toml in this PR"
base
check "the refusal names the published file that matched" 1 "tracebloc/_linking.py (matches 'tracebloc/*')"
base; SOFT_FAIL=true
check "soft-fail reports the same finding and exits 0" 0 "soft-fail is on"
base; SOFT_FAIL=true
check "soft-fail still says what is wrong" 0 "ALREADY RELEASED"

echo
echo "the legitimate passes - a gate that is wrong on these gets removed, not obeyed"
base; mkcompare "docs/readme.md" ".github/workflows/ci.yml" "tests/test_x.py"
check "a docs/CI-only PR passes with the version stale AND released" 0 "No publishable delta"
base; mkcompare "docs/readme.md"
check "the docs-only pass says what it scanned, not just that it passed" 0 "none matching [tracebloc/* pyproject.toml]"
base; mkversion head '[project]
version = "0.18.0"
'
check "a PR that bumps to an unreleased version passes" 0 "bumped 0.17.0 -> 0.18.0"
base; mktags 0.16.0
check "an unchanged version that is NOT released passes - no bump is owed" 0 "no bump is owed"
base; PR_HEAD_REF=release-train/to-staging; PR_BASE_REF=staging
check "a release-train promotion PR is green with a stated reason" 0 "Release-train promotion PR"
base; PR_HEAD_REF=release-train/to-master; PR_BASE_REF=master
check "a promotion into a prod branch is green too" 0 "Release-train promotion PR"
base; PR_HEAD_REF=hotfix-backmerge/prod-fix; PR_BASE_REF=main
check "a hotfix backmerge is green for the same reason" 0 "Release-train promotion PR"
base; STUB_BASE_STATUS=404; mkversion head '[project]
version = "0.19.0"
'
check "a PR that INTRODUCES the version file passes" 0 "introduces pyproject.toml"
base; STUB_BASE_STATUS=404
check "...but introducing it at an ALREADY-RELEASED version is still refused" 1 "has already shipped"

echo
echo "versions that are wrong in a different way"
base; mkversion head '[project]
version = "0.16.0"
'
check "bumping DOWN to another released version is refused as already shipped" 1 "has already shipped"
base; mkversion head '[project]
version = "0.16.5"
'; mktags 0.16.0 0.17.0
check "an untagged version BELOW the current release is refused" 1 "sit BELOW the current release"
base; mkversion head '[project]
version = "0.16.5"
'; mktags 0.16.0 0.17.0
check "the regression refusal names the version to beat" 1 "Bump pyproject.toml above 0.17.0"

echo
echo "renames - a file moved OUT of a published tree still left the package"
base; mkcompare "vendor/_linking.py:tracebloc/_linking.py"
check "the PRE-rename path counts as a published change" 1 "renamed to vendor/_linking.py"

echo
echo "the promotion exemption is not mintable - a branch name is not a credential"
base; PR_HEAD_REF=release-train/looks-official; PR_BASE_REF=develop
check "borrowing the name to reach develop does NOT exempt" 1 "Bump pyproject.toml"
base; PR_HEAD_REF=release-train/looks-official; PR_BASE_REF=develop
check "...and the mismatch is warned about, not silently absent" 1 "does not promote there"
base; PR_HEAD_REF=release-train/to-staging; PR_BASE_REF=staging; IS_FORK=true
check "a FORK cannot claim the exemption even with the right base" 1 "comes from a FORK"
base; PR_HEAD_REF=release-train/to-staging; PR_BASE_REF=staging; IS_FORK=true
check "...and is then evaluated like any other PR" 1 "Bump pyproject.toml"
base; PR_HEAD_REF=release-train/to-staging; PR_BASE_REF=staging; IS_FORK=""
check "an UNREADABLE head.repo does not buy an exemption either" 1 "could not be confirmed"

echo
echo "fail-closed paths - 'could not evaluate' must never read as 'nothing found'"
base; VERSION_FILE=""
check "no version-file input refuses" 1 "supplied no 'version-file'"
base; PUBLISH_PATHS=""
check "no publish-paths input refuses (absent is not permission)" 1 "supplied no 'publish-paths'"
base; PR_BASE_SHA=""
check "a missing base SHA refuses" 1 "base/head SHA missing"
base; STUB_COMPARE_STATUS=500
check "an unreadable compare refuses" 1 "Could not read the compare"
base; mkcompare_raw '{"files":[{"filename":"tracebloc/x.py"}]}'
check "a compare with no merge base refuses" 1 "carried no merge base"
base; mkcompare_raw '{"merge_base_commit":{"sha":"basesha"},"files":[]}'
check "ZERO files scanned is a malfunction, not a pass" 1 "zero files scanned is a malfunction"
base; mkcompare_at_cap
check "300 files with no match refuses - the page cap has no truncation flag" 1 "page cap"
base; STUB_HEAD_STATUS=500
check "an unreadable version file on head refuses" 1 "unreadable or EMPTY at headsha on all 3 attempts"
base; STUB_HEAD_STATUS=404
check "a version file missing on head refuses, and says so distinctly" 1 "does not exist on this PR's head"
base; mkversion head 'name = "tracebloc"
'
check "a head version file with no version in it refuses" 1 "found no version in it"
base; mkversion base 'name = "tracebloc"
'
check "an unparseable version at the MERGE BASE refuses" 1 "at the merge base"
base; STUB_BASE_STATUS=500
check "an unreadable version file at the merge base refuses" 1 "at the merge base"
base; STUB_TAGS_STATUS=500
check "an unreadable tag listing refuses rather than assuming untagged" 1 "Could not list this repo's v* tags"

echo
echo "soft-fail is about the verdict, never about the machinery"
base; SOFT_FAIL=true; STUB_TAGS_STATUS=500
check "soft-fail does NOT rescue an unreadable tag listing" 1 "failing closed"
base; SOFT_FAIL=true; PUBLISH_PATHS=""
check "soft-fail does NOT rescue a missing publish-paths" 1 "supplied no 'publish-paths'"
base; SOFT_FAIL=true; mkcompare_raw '{"merge_base_commit":{"sha":"basesha"},"files":[]}'
check "soft-fail does NOT rescue zero files scanned" 1 "malfunction, not a pass"

echo
echo "the override, which must sit ahead of every refusal"
base; LABELS='["skip-version-gate"]'
check "the label bypasses a genuine finding" 0 "bypassed via the 'skip-version-gate' label"
base; LABELS='["skip-version-gate"]'; PUBLISH_PATHS=""
check "the label reaches a PR the config refusal would have exited first" 0 "bypassed via the"
base; LABELS='["skip-version-gate"]'
check "the bypass says it is not a licence to publish" 0 "buys a merge, not a release"
base; LABELS='["skip-version-gate-please"]'
check "a label merely CONTAINING the override name does not disarm the gate" 1 "Bump pyproject.toml"
base; LABELS='not json'
check "an unparseable label payload does not disarm the gate" 1 "Bump pyproject.toml"

echo
echo "glob safety - publish_paths are patterns, not paths in \$PWD"
# Without `set -f`, `for pat in $PUBLISH_PATHS` expands `tracebloc/*` against the
# working directory: run from a checkout that happens to contain a matching tree
# and the loop compares against real filenames instead of the pattern, so a
# published change stops matching. promote-repo.sh survives this only because it
# runs where no publish path exists.
mkdir -p "$WORK/globtrap/tracebloc"
: > "$WORK/globtrap/tracebloc/unrelated.py"
base; CASE_DIR="$WORK/globtrap"
check "a publish path is still a glob when \$PWD contains a matching tree" 1 "tracebloc/_linking.py (matches 'tracebloc/*')"

echo
echo "the version parse, per file type - all five version_file repos in repos.yml"
# Each case proves the parse by making the gate report the version it read: the
# refusal below quotes it verbatim. The wrong-line traps are the ones
# promote-repo.sh's own comments name.
base; REPO_FULL=tracebloc/cli; VERSION_FILE=VERSION; PUBLISH_PATHS='cmd/* internal/* go.mod go.sum VERSION'
mkcompare "cmd/root.go"
mkversion head '0.10.3
'
mkversion base '0.10.3
'
mktags 0.10.3
check "cli: a bare VERSION file" 1 "VERSION still says 0.10.3"

base; REPO_FULL=tracebloc/cli; VERSION_FILE=VERSION; PUBLISH_PATHS='cmd/* VERSION'
mkcompare "cmd/root.go"
mkversion head '# bumped from 9.9.9 by hand
0.10.3
'
mkversion base '# bumped from 9.9.9 by hand
0.10.3
'
mktags 0.10.3 9.9.9
check "cli: a version in a LEADING COMMENT does not win (backend#1427)" 1 "VERSION still says 0.10.3"

base; REPO_FULL=tracebloc/design-system; VERSION_FILE=package.json; PUBLISH_PATHS='src/*'
mkcompare "src/index.ts"
mkversion head '{"name":"@tracebloc/design-system","dependencies":{"react":"18.3.1"},"version":"2.4.0"}'
mkversion base '{"name":"@tracebloc/design-system","dependencies":{"react":"18.3.1"},"version":"2.4.0"}'
mktags 2.4.0
check "design-system: package.json reads .version, not a dependency pin" 1 "package.json still says 2.4.0"

base; REPO_FULL=tracebloc/client; VERSION_FILE=client/Chart.yaml; PUBLISH_PATHS='client/*'
mkcompare "client/templates/deployment.yaml"
mkversion head 'apiVersion: v2
appVersion: "1.16.0"
version: 0.4.2
'
mkversion base 'apiVersion: v2
appVersion: "1.16.0"
version: 0.4.2
'
mktags 0.4.2 1.16.0
check "client: Chart.yaml reads version:, never appVersion:" 1 "client/Chart.yaml still says 0.4.2"

base; REPO_FULL=tracebloc/data-ingestors; VERSION_FILE=tracebloc_ingestor/__init__.py; PUBLISH_PATHS='tracebloc_ingestor/*'
mkcompare "tracebloc_ingestor/reader.py"
mkversion head 'SCHEMA_VERSION = "3.2.1"
__version__ = "0.8.0"
'
mkversion base 'SCHEMA_VERSION = "3.2.1"
__version__ = "0.8.0"
'
mktags 0.8.0 3.2.1
check "data-ingestors: __init__.py reads __version__, not a schema constant" 1 "__init__.py still says 0.8.0"

base; REPO_FULL=tracebloc/data-ingestors; VERSION_FILE=tracebloc_ingestor/__init__.py; PUBLISH_PATHS='tracebloc_ingestor/*'
mkcompare "tracebloc_ingestor/reader.py"
mkversion head "__version__ = '0.8.0'
"
mkversion base "__version__ = '0.8.0'
"
mktags 0.8.0
check "data-ingestors: single quotes parse the same as double" 1 "__init__.py still says 0.8.0"

base; mkversion head '[project]
version = "1.0.0-rc.1"
'
mkversion base '[project]
version = "1.0.0-rc.1"
'
mktags 1.0.0-rc.1
check "a prerelease suffix survives the parse (backend#1427)" 1 "still says 1.0.0-rc.1"

echo
echo
echo "exclude-paths - files inside a published tree that do not ship (backend#2758)"

# THE CASE THAT SENT design-system#272 THROUGH skip-version-gate. A stories-only
# PR under `src/*`, at a version that is already released. Without an exclusion
# the gate refuses; the only way past it was the audited override, which then
# left the version stale and refused the NEXT promotion at the hop.
base; REPO_FULL=tracebloc/design-system; VERSION_FILE=package.json
PUBLISH_PATHS='src/*'; EXCLUDE_PATHS='src/**/*.stories.tsx src/**/*.test.tsx src/**/*.test.ts'
mkcompare "src/components/organisms/Menubar/Menubar.stories.tsx"
mkversion head '{"version":"1.4.0"}'
mkversion base '{"version":"1.4.0"}'
mktags 1.4.0
check "a stories-only PR does not need a bump" 0 "were treated as not shipping"

# THE SAME PR WITH NO EXCLUSION, and this is the control: it proves the previous
# case passes BECAUSE of the exclusion and not because the fixture was harmless.
base; REPO_FULL=tracebloc/design-system; VERSION_FILE=package.json
PUBLISH_PATHS='src/*'; EXCLUDE_PATHS=''
mkcompare "src/components/organisms/Menubar/Menubar.stories.tsx"
mkversion head '{"version":"1.4.0"}'
mkversion base '{"version":"1.4.0"}'
mktags 1.4.0
check "the same PR still refuses with no exclusion (control)" 1 "package.json still says 1.4.0"

# EXCLUSION MUST NOT OVER-APPLY. Real component source in the same tree, with the
# same exclusion list, still refuses. An exclusion that swallowed this would be a
# gate that never fires.
base; REPO_FULL=tracebloc/design-system; VERSION_FILE=package.json
PUBLISH_PATHS='src/*'; EXCLUDE_PATHS='src/**/*.stories.tsx src/**/*.test.tsx src/**/*.test.ts'
mkcompare "src/components/atoms/Badge/Badge.tsx"
mkversion head '{"version":"1.4.0"}'
mkversion base '{"version":"1.4.0"}'
mktags 1.4.0
check "real component source still refuses despite the exclusions" 1 "package.json still says 1.4.0"

# A MIXED PR IS PUBLISHABLE. One story and one component: the component decides.
base; REPO_FULL=tracebloc/design-system; VERSION_FILE=package.json
PUBLISH_PATHS='src/*'; EXCLUDE_PATHS='src/**/*.stories.tsx'
mkcompare "src/components/atoms/Badge/Badge.stories.tsx" "src/components/atoms/Badge/Badge.tsx"
mkversion head '{"version":"1.4.0"}'
mkversion base '{"version":"1.4.0"}'
mktags 1.4.0
check "a story plus a component is still publishable" 1 "package.json still says 1.4.0"

# RENAMED OUT OF THE PACKAGE STILL COUNTS. A shipping file moved to an excluded
# path left the package, which is a published change even though its NEW path is
# excluded. This is the half a naive per-file exclusion gets wrong.
base; REPO_FULL=tracebloc/design-system; VERSION_FILE=package.json
PUBLISH_PATHS='src/*'; EXCLUDE_PATHS='src/**/*.stories.tsx'
mkcompare "src/components/atoms/Badge/Badge.stories.tsx:src/components/atoms/Badge/Badge.tsx"
mkversion head '{"version":"1.4.0"}'
mkversion base '{"version":"1.4.0"}'
mktags 1.4.0
check "a shipping file renamed INTO an excluded path still counts" 1 "package.json still says 1.4.0"

# ...AND THE MIRROR CASE, which the first version got wrong (Bugbot, backend#2758).
# An EXCLUDED file renamed OUT of the published tree entirely: a story becoming a
# docs page. Nothing that ships changed, so no bump is owed.
#
# The bug was in the shape of the test, not just the code: the exclusion was asked
# as `is_excluded "$f" && is_excluded "$prev"`, then `$prev` was matched against
# $PUBLISH_PATHS with no exclusion check at all. Here `$f` (docs/) is not excluded,
# so the conjunction short-circuited, `$f` did not match `src/*`, and the excluded
# `$prev` then matched and tripped the gate the exclusion exists to silence. The
# five cases above could not see it: not one of them renames a file OUT of the tree,
# so the whole `$prev`-is-excluded half of the input domain went untested (rule 6).
base; REPO_FULL=tracebloc/design-system; VERSION_FILE=package.json
PUBLISH_PATHS='src/*'; EXCLUDE_PATHS='src/**/*.stories.tsx'
mkcompare "docs/Badge.mdx:src/components/atoms/Badge/Badge.stories.tsx"
mkversion head '{"version":"1.4.0"}'
mkversion base '{"version":"1.4.0"}'
mktags 1.4.0
check "an EXCLUDED file renamed OUT of the tree does not need a bump" 0 "were treated as not shipping"

# THE CONTROL FOR IT. Same rename shape, but the pre-rename path is real component
# source rather than a story: that DID leave the package, so it must still refuse.
# Without this, the case above could be satisfied by an exclusion that swallowed
# every rename-out, which is the opposite defect.
base; REPO_FULL=tracebloc/design-system; VERSION_FILE=package.json
PUBLISH_PATHS='src/*'; EXCLUDE_PATHS='src/**/*.stories.tsx'
mkcompare "docs/Badge.mdx:src/components/atoms/Badge/Badge.tsx"
mkversion head '{"version":"1.4.0"}'
mkversion base '{"version":"1.4.0"}'
mktags 1.4.0
check "a SHIPPING file renamed OUT of the tree still refuses (control)" 1 "package.json still says 1.4.0"

printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
