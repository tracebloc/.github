#!/usr/bin/env python3
"""Mutation harness for scripts/fr-gate-walk.sh (backend#3323, RFC-0075 D6).

`fr-gate-walk-selftest.sh` asserts the behaviour; this asserts the SELFTEST. Each
row below reintroduces one defect the walk was written to prevent -- in the gate
half (the inline blocks it replaced) or the frontier half -- and requires the
suite to go red. A row that stays green is a case that has decayed into
decoration, and the run fails.

THE MUTATION EDITS THE REAL FILE (CLAUDE.md rule 9): it rewrites the script on
disk and re-runs the real suite. There is no second copy of the rule in here, and
every anchor must match EXACTLY ONCE -- twice mutates an arbitrary one, zero is
stale; both fail the run so an inert mutation cannot read as coverage.

  fr-gate-walk-mutations.py          run them all (~15 s per row)
  fr-gate-walk-mutations.py --dry    resolve anchors only (the fast tier)
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "fr-gate-walk.sh"
SUITE = ROOT / "scripts" / "tests" / "fr-gate-walk-selftest.sh"

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402

# (label, old, new)
MUTATIONS = [
    # ---- the gate half: the inline blocks' own history of holes -------------
    ("Bugbot #73: a sync merge vouches -- the merge_commit_sha match is dropped",
     "        '[.[] | select(.merged_at != null and .merge_commit_sha == $s\n"
     "                 and (((.head.ref // \"\") | startswith(\"release-train/\")) | not))",
     "        '[.[] | select(.merged_at != null\n"
     "                 and (((.head.ref // \"\") | startswith(\"release-train/\")) | not))"),

    ("Bugbot #72: a base push is laundered once a feature branch merged the base in",
     '         && ! git merge-base --is-ancestor "$csha" "$p1" 2>/dev/null; then',
     '         && true; then'),

    ("the train's own promotion PR becomes a gated item (release-train/* filter dropped, pass 1)",
     "        '[.[] | select(.merged_at != null and .merge_commit_sha == $s\n"
     "                 and (((.head.ref // \"\") | startswith(\"release-train/\")) | not))",
     "        '[.[] | select(.merged_at != null and .merge_commit_sha == $s)"),

    ("the train's own promotion PR is attributed to every inner commit (filter dropped, pass 2)",
     "        '[.[] | select(.merged_at != null\n"
     "                 and (((.head.ref // \"\") | startswith(\"release-train/\")) | not))",
     "        '[.[] | select(.merged_at != null)"),

    ("RFC-1405 D8 / backend#1411: a Done card blocks every prod promotion again",
     '    "Done")              echo 11 ;;',
     '    "Done")              echo "" ;;'),

    ("a PR with no card is no longer reported as missing (verify)",
     '    if [ "$rc" -eq 2 ] || [ -z "$STATUS" ]; then',
     '    if false; then'),

    ("Bugbot .github#109: ancestry-only passes with an unverified commit in range",
     "       && [ -z \"$(echo \"${UNATTRIB}\" | tr -d ' ')\" ]; then",
     "       ; then"),

    ("an uncounted range is treated as empty (compare API down -> 0 files)",
     "      DIFF_FILES=-1",
     "      DIFF_FILES=0"),

    ("verify: a blocked item no longer fails the gate",
     '  if [ -n "$BLOCKED" ] || [ -n "$MISSING" ] || [ -n "$UNREADABLE" ] || [ -n "${UNATTRIB// /}" ]; then',
     '  if [ -n "$MISSING" ] || [ -n "$UNREADABLE" ] || [ -n "${UNATTRIB// /}" ]; then'),

    ("an unanswered commits/pulls read is silently attributed to nothing (gate half)",
     '      UNATTRIB="$UNATTRIB ${csha}"\n      : > "$WALK_DIR/apifail/$csha"',
     '      : > "$WALK_DIR/apifail/$csha"'),

    # ---- the frontier half ---------------------------------------------------
    ("frontier: the walk goes first-parent -- one card pins its whole hop",
     '  RANGE=$(git rev-list --topo-order --reverse "$TARGET_SHA..$SOURCE_SHA")',
     '  RANGE=$(git rev-list --first-parent --topo-order --reverse "$TARGET_SHA..$SOURCE_SHA")'),

    ("frontier: a commit inside a PR becomes a cut candidate (partial payload)",
     '    [ -e "$WALK_DIR/covered/$sha" ] && continue          # inside a PR: never a cut (point 4)',
     '    true'),

    ("frontier: a direct push is not a blocker",
     '        reason="${sha}(no merged PR)"',
     '        reason=""'),

    ("frontier: an item below the required rank is not a blocker",
     '        reason="${reason:+$reason|}#${num}(${pr_status})"',
     '        true'),

    ("frontier: a PR with no card passes",
     '      STATUS="not on the kanban board"',
     '      STATUS="$REQUIRED"'),

    ("frontier: an unreadable board yields a frontier instead of UNKNOWN",
     '    if [ "$src" -eq 1 ]; then',
     '    if false; then'),

    ("frontier: an unanswered commits/pulls read yields a frontier instead of UNKNOWN",
     '    unknown "commits/{sha}/pulls never answered for:',
     '    say "commits/{sha}/pulls never answered for:'),

    ("frontier: the at-target fact is printed as empty-range",
     '    echo "frontier_state=at-target"',
     '    echo "frontier_state=empty-range"'),

    ("frontier: a shallow checkout is walked anyway",
     '  if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then',
     '  if false; then'),

    ("frontier: an unrankable REQUIRED is not refused",
     '  [ -n "$RR" ] || unknown "REQUIRED=\'$REQUIRED\' is not a Status this walk can rank."',
     '  RR="${RR:-9}"'),
]


def apply_one(src, old, new):
    n = src.count(old)
    if n != 1:
        raise LookupError("anchor matched %d times, expected exactly 1: %r" % (n, old[:80]))
    out = src.replace(old, new, 1)
    return None if out == src else out


def main():
    dry = "--dry" in sys.argv

    if not dry:
        rc = mutation_baseline.guard(ROOT, [SCRIPT])
        if rc:
            return rc

    pristine = SCRIPT.read_text(encoding="utf-8")
    stale, uncaught = [], []

    for label, old, new in MUTATIONS:
        try:
            mutated = apply_one(pristine, old, new)
        except LookupError as exc:
            stale.append((label, str(exc)))
            continue
        if mutated is None:
            stale.append((label, "NO-OP: the mutation changed nothing"))
            continue
        if dry:
            print("  anchor ok  %s" % label)
            continue
        SCRIPT.write_text(mutated, encoding="utf-8")
        try:
            run = subprocess.run(
                ["bash", str(SUITE)], capture_output=True, text=True, cwd=str(ROOT),
            )
        finally:
            SCRIPT.write_text(pristine, encoding="utf-8")
        caught = [
            line.strip()[6:].strip()
            for line in run.stdout.splitlines()
            if line.strip().startswith("FAIL:")
        ]
        # A crash counts as caught ONLY if the suite actually ran and reported;
        # a bare traceback means the mutation broke the harness, not that a case
        # detected it.
        reported = "fr-gate-walk-selftest:" in run.stdout
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (label, ", ".join(caught)[:140]))
        elif not reported:
            uncaught.append((label, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % label)
        else:
            uncaught.append((label, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % label)

    if SCRIPT.read_text(encoding="utf-8") != pristine:
        sys.stderr.write("::error::%s was left mutated. Restore it from git.\n" % SCRIPT.name)
        return 2

    print("\n%d mutation(s): %d stale, %d uncaught" % (len(MUTATIONS), len(stale), len(uncaught)))
    for label, why in stale:
        sys.stderr.write("::error::STALE mutation `%s`: %s\n" % (label, why))
    for label, why in uncaught:
        sys.stderr.write(
            "::error::UNCAUGHT `%s`: %s. Add a case that fails under it, or delete "
            "the mutation and say why it is not worth pinning.\n" % (label, why)
        )
    return 1 if (stale or uncaught) else 0


if __name__ == "__main__":
    raise SystemExit(main())
