#!/usr/bin/env bash
#
# extract-advanced-prs selftest (backend#3365) — the attribution, exercised
# against real git repositories with `gh` stubbed on PATH.
#
# The defect this pins: attributing a card by grepping the commit SUBJECT reads a
# TICKET (or nothing) instead of the PR whenever the author put the ticket in the
# `(#N)` slot or edited the squash subject. The two fixtures below are the two real
# 2026-09-07 misses; each asserts the API-derived number wins and the subject
# number does NOT. Nothing here talks to GitHub — the stub returns canned
# `/pulls` JSON keyed by commit sha and applies the caller's `--jq` with real jq.
#
# Run: bash scripts/tests/extract-advanced-prs-selftest.sh
set -uo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/extract-advanced-prs.sh"
[ -f "$SCRIPT" ] || { echo "FATAL: $SCRIPT not found"; exit 1; }

pass=0
fail=0
ok() { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
no() { printf '  FAIL  %s\n     %s\n' "$1" "$2"; fail=$((fail + 1)); }
assert_out()     { if grep -qF -- "$2" <<<"$3"; then ok "$1"; else no "$1" "expected: $2 -- got: $3"; fi; }
assert_not_out() { if grep -qF -- "$2" <<<"$3"; then no "$1" "must NOT contain: $2 -- got: $3"; else ok "$1"; fi; }

# A `gh` stub: `gh api repos/<repo>/commits/<sha>/pulls --jq <expr>` -> apply
# <expr> with real jq over $STUB_DIR/<sha>.json (an empty array if none written).
# Anything else exits non-zero so an unexpected call is loud, not silently empty.
make_gh_stub() {
  local bin="$1"
  mkdir -p "$bin"
  cat >"$bin/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = api ]; then
  path="$2"; shift 2
  jqexpr='.'
  while [ $# -gt 0 ]; do case "$1" in --jq) jqexpr="$2"; shift 2;; *) shift;; esac; done
  sha="$(printf '%s' "$path" | sed -nE 's#.*/commits/([0-9a-f]+)/pulls#\1#p')"
  f="$STUB_DIR/${sha}.json"
  [ -f "$f" ] || f=/dev/stdin
  if [ "$f" = /dev/stdin ]; then echo '[]' | jq -r "$jqexpr"; else jq -r "$jqexpr" "$f"; fi
  exit 0
fi
echo "gh stub: unexpected args: $*" >&2
exit 1
STUB
  chmod +x "$bin/gh"
}

# make_repo <dir> -> a repo with a base commit on `staging`; echoes the base sha.
make_repo() {
  local work="$1"
  git init -q --initial-branch=staging "$work"
  git -C "$work" config user.email t@t.io
  git -C "$work" config user.name t
  echo base >"$work/f"; git -C "$work" add f; git -C "$work" commit -qm base
  git -C "$work" rev-parse HEAD
}

# commit <dir> <subject> -> echoes the new commit sha.
commit() {
  local work="$1" subject="$2"
  echo "$RANDOM" >>"$work/f"; git -C "$work" add f
  git -C "$work" commit -qm "$subject"
  git -C "$work" rev-parse HEAD
}

# run the extractor over BEFORE..SHA on `staging`, with the stub on PATH.
run_extract() {
  local work="$1" before="$2" sha="$3" bin="$4"
  ( cd "$work" && PATH="$bin:$PATH" STUB_DIR="$STUB_DIR" \
      BEFORE="$before" SHA="$sha" BRANCH=staging GITHUB_REPOSITORY=tracebloc/x \
      bash "$SCRIPT" 2>&1 )
}

# ---------------------------------------------------------------------------
# FIXTURE 1: client#985 — subject names ISSUE #979, the merged PR is #985.
# The old grep read 979; the API read must win with 985.
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
a="$(commit "$root" 'fix(tests): bound the k3d cleanup in all seven e2e EXIT traps (#979)')"
printf '[{"number":985,"merged_at":"2026-09-07T00:00:00Z","base":{"ref":"staging"}}]\n' >"$STUB_DIR/${a}.json"
out="$(run_extract "$root" "$base" "$a" "$bin")"
assert_out     "client#985: the API-derived PR wins"        "Found PRs: 985" "$out"
assert_not_out "client#985: the subject issue is NOT used"  "979"            "$out"

# ---------------------------------------------------------------------------
# FIXTURE 2: tracebloc-engine#914 — subject names ticket backend#3013 in the
# (#N) slot, the merged PR is #914. The old grep matched nothing.
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
b="$(commit "$root" 'sec(deps): torch 2.13.0 + torchvision 0.28.0 on the cu129 index (backend#3013)')"
printf '[{"number":914,"merged_at":"2026-09-07T00:00:00Z","base":{"ref":"staging"}}]\n' >"$STUB_DIR/${b}.json"
out="$(run_extract "$root" "$base" "$b" "$bin")"
assert_out     "engine#914: the API-derived PR wins"       "Found PRs: 914" "$out"
assert_not_out "engine#914: the subject ticket is NOT used" "3013"          "$out"

# ---------------------------------------------------------------------------
# FALLBACK: the API returns no PR, but the subject carries a real (#N) -> use it
# and WARN, so the attribution is visible rather than silent.
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
c="$(commit "$root" 'chore: tidy the makefile (#777)')"   # no stub file -> API returns []
out="$(run_extract "$root" "$base" "$c" "$bin")"
assert_out "fallback: the subject PR is used when the API is empty" "Found PRs: 777"          "$out"
assert_out "fallback: it warns that the attribution is a fallback"  "attributed from its SUBJECT" "$out"

# ---------------------------------------------------------------------------
# UNATTRIBUTABLE: API empty AND subject names nothing -> no PR, and a warning
# so the un-advanced card is visible.
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
d="$(commit "$root" 'docs: fix a typo')"
out="$(run_extract "$root" "$base" "$d" "$bin")"
assert_out "unattributable: no PR is attributed"           "Found PRs: "              "$out"
assert_out "unattributable: it warns the card was skipped" "no PR could be attributed" "$out"

# ---------------------------------------------------------------------------
# BASE FILTER: a merged PR whose base is DEVELOP (not this staging push) must be
# ignored, falling through to the subject. Proves the base.ref filter is load-bearing.
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
e="$(commit "$root" 'fix(x): a thing (#555)')"
printf '[{"number":222,"merged_at":"2026-09-07T00:00:00Z","base":{"ref":"develop"}}]\n' >"$STUB_DIR/${e}.json"
out="$(run_extract "$root" "$base" "$e" "$bin")"
assert_not_out "base filter: a develop-based PR is not attributed to a staging push" "222" "$out"
assert_out     "base filter: it falls through to the subject PR"                     "Found PRs: 555" "$out"

echo
echo "extract-advanced-prs selftest: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
