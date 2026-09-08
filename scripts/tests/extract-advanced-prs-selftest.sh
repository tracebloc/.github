#!/usr/bin/env bash
#
# extract-advanced-prs selftest (backend#3365) — the attribution, exercised
# against real git repositories with `gh` stubbed on PATH.
#
# The defect this pins: attributing a card by grepping the commit SUBJECT reads a
# TICKET (or nothing) instead of the PR whenever the author put the ticket in the
# `(#N)` slot or edited the squash subject. The two fixtures below are the two real
# 2026-09-07 misses, WITH THEIR REAL BASES: both feature PRs merged to `develop`,
# and the miss showed up on the `staging` hop. A base filter would reject them
# there (that was the first cut's bug, @saadqbal on .github#438) -- so these carry
# `base: develop` and must still be attributed, which is exactly what a base-free
# read gives. Nothing here talks to GitHub: the stub returns canned `/pulls` JSON
# keyed by commit sha and applies the caller's `--jq` with real jq.
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

# A `gh` stub. `gh api repos/<repo>/commits/<sha>/pulls --jq <expr>`:
#   * $STUB_DIR/<sha>.fail present -> exit 1 (a FAILED read, e.g. a 403)
#   * else apply <expr> with real jq over $STUB_DIR/<sha>.json (empty array if none)
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
  [ -f "$STUB_DIR/${sha}.fail" ] && { echo "gh: HTTP 403 rate limit" >&2; exit 1; }
  f="$STUB_DIR/${sha}.json"
  if [ -f "$f" ]; then jq -r "$jqexpr" "$f"; else echo '[]' | jq -r "$jqexpr"; fi
  exit 0
fi
echo "gh stub: unexpected args: $*" >&2
exit 1
STUB
  chmod +x "$bin/gh"
}

# make_repo <dir> -> a repo with a base commit on `staging`; echoes the base sha.
# The pushed branch is `staging`: the real hop where the bug showed, and the one a
# base filter would have broken.
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

# run the extractor over BEFORE..SHA, with the stub on PATH. No BRANCH: the script
# is base-free, so the pushed branch never enters the attribution.
run_extract() {
  local work="$1" before="$2" sha="$3" bin="$4"
  ( cd "$work" && PATH="$bin:$PATH" STUB_DIR="$STUB_DIR" \
      BEFORE="$before" SHA="$sha" GITHUB_REPOSITORY=tracebloc/x \
      bash "$SCRIPT" 2>&1 )
}

# ---------------------------------------------------------------------------
# FIXTURE 1: client#985 — subject names ISSUE #979; the merged PR is #985, base
# develop, on a STAGING hop. The real base is what makes this the regression test:
# a base==staging filter would reject it and fall back to the #979 miss.
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
a="$(commit "$root" 'fix(tests): bound the k3d cleanup in all seven e2e EXIT traps (#979)')"
printf '[{"number":985,"merged_at":"2026-09-07T08:22:52Z","base":{"ref":"develop"},"head":{"ref":"fix/979-k3d-cleanup"}}]\n' >"$STUB_DIR/${a}.json"
out="$(run_extract "$root" "$base" "$a" "$bin")"
assert_out     "client#985: the develop-based feature PR is attributed on a staging hop" "Found PRs: 985" "$out"
assert_not_out "client#985: the subject issue is NOT used"                               "979"            "$out"

# ---------------------------------------------------------------------------
# FIXTURE 2: tracebloc-engine#914 — subject names ticket backend#3013 in the
# (#N) slot; merged PR #914, base develop. The old grep matched nothing.
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
b="$(commit "$root" 'sec(deps): torch 2.13.0 + torchvision 0.28.0 on the cu129 index (backend#3013)')"
printf '[{"number":914,"merged_at":"2026-09-07T00:00:00Z","base":{"ref":"develop"},"head":{"ref":"sec/3013-torch"}}]\n' >"$STUB_DIR/${b}.json"
out="$(run_extract "$root" "$base" "$b" "$bin")"
assert_out     "engine#914: the develop-based feature PR is attributed on a staging hop" "Found PRs: 914" "$out"
assert_not_out "engine#914: the subject ticket is NOT used"                              "3013"           "$out"

# ---------------------------------------------------------------------------
# PROMOTION PR EXCLUDED: a commit whose /pulls names a merged release-train/*
# promotion PR must NOT be attributed (those carry no card); fall to the subject.
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
p="$(commit "$root" 'chore(promote): develop -> staging (#900)')"
printf '[{"number":901,"merged_at":"2026-09-07T00:00:00Z","base":{"ref":"staging"},"head":{"ref":"release-train/develop-to-staging"}}]\n' >"$STUB_DIR/${p}.json"
out="$(run_extract "$root" "$base" "$p" "$bin")"
assert_not_out "promotion: the release-train PR is not attributed"       "901"            "$out"
assert_out     "promotion: it falls through to the subject"              "Found PRs: 900" "$out"

# ---------------------------------------------------------------------------
# EMPTY API -> subject fallback, with the "API names none" warning.
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
c="$(commit "$root" 'chore: tidy the makefile (#777)')"   # no stub file -> API returns []
out="$(run_extract "$root" "$base" "$c" "$bin")"
assert_out "empty API: the subject PR is used"          "Found PRs: 777"       "$out"
assert_out "empty API: it warns the API named none"     "the API names no merged PR" "$out"

# ---------------------------------------------------------------------------
# FAILED API read (403/5xx) -> distinct from empty: its own warning, NOT the
# "API names none" wording (@saadqbal on .github#438).
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
q="$(commit "$root" 'fix(x): a thing (#654)')"
touch "$STUB_DIR/${q}.fail"                                # stub exits 1
out="$(run_extract "$root" "$base" "$q" "$bin")"
assert_out     "failed read: falls back to the subject as a last resort" "Found PRs: 654"           "$out"
assert_out     "failed read: warns the read FAILED, distinctly"          "API read FAILED"          "$out"
assert_not_out "failed read: does NOT claim the API named none"          "the API names no merged"  "$out"

# ---------------------------------------------------------------------------
# UNATTRIBUTABLE: empty API AND subject names nothing -> no PR, warning only.
# ---------------------------------------------------------------------------
root="$(mktemp -d)"; STUB_DIR="$(mktemp -d)"; bin="$(mktemp -d)"
make_gh_stub "$bin"
base="$(make_repo "$root")"
d="$(commit "$root" 'docs: fix a typo')"
out="$(run_extract "$root" "$base" "$d" "$bin")"
assert_out "unattributable: no PR is attributed"           "Found PRs: "               "$out"
assert_out "unattributable: it warns the card was skipped" "no PR could be attributed" "$out"

echo
echo "extract-advanced-prs selftest: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
