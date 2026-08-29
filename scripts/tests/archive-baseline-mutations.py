#!/usr/bin/env python3
"""Mutation harness for the archive's cross-run floor (backend#2802).

`archive-baseline-selftest.py` asserts the guard's behaviour; this asserts the
SELFTEST. Break the floor in `kanban-archive.yml`, watch the suite redden,
restore. Six green cases cannot tell you which of them are load-bearing, and
this repo has shipped that exact illusion before.

THE MUTATION EDITS THE WORKFLOW, NOT A COPY (CLAUDE.md rule 9). The suite
extracts its shell out of `kanban-archive.yml` by anchor at run time, so
mutating that file is mutating the code under test. The alternative shape --
re-implementing the comparison in here and mutating the re-implementation --
looks identical in a log and has bitten this org twice.

WHY THE UPLOAD STEP IS A TARGET TOO. The comparison being right and the baseline
ACTUALLY BEING CARRIED FORWARD are separate claims, and the second lives in
YAML the shell never touches. If the artifact stops being written -- or starts
being written on a failed run -- every future comparison silently falls back to
the no-baseline warning and this check stops existing, with nothing red to say
so. So the harness mutates that wiring too, and the suite has to notice.

EVERY ANCHOR MUST MATCH EXACTLY ONCE. An anchor matching twice mutates an
arbitrary one; an anchor matching zero times is stale and fails the run exactly
like an uncaught mutation. That is the assertion that the mutation ACTUALLY
APPLIED -- inert mutations and good coverage are indistinguishable otherwise.
`--dry` resolves every anchor without running the suite, for the fast tier.

  archive-baseline-mutations.py          run them all
  archive-baseline-mutations.py --dry    resolve anchors only
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "kanban-archive.yml"
SUITE = ROOT / "scripts" / "tests" / "archive-baseline-selftest.py"

# dont_write_bytecode BEFORE the import: `selftests-cover` rejects anything under
# scripts/tests/ that is not a suite or a runner, and a `__pycache__/` is exactly
# that.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402


# (label, old, new)
MUTATIONS = [
    # --- (A) the comparison itself -----------------------------------------
    #
    # The direction of the test is the whole guard. Flipped, it refuses every
    # growing board and waves through every shrinking one -- the precise
    # inversion of what it is for, and green under any case set that only ever
    # feeds it shrinkage.
    (
        "the floor comparison is inverted, so a shrunken view passes and growth refuses",
        'if [ "$declared_total" -lt "$floor" ]; then',
        'if [ "$declared_total" -gt "$floor" ]; then',
    ),
    # The floor without the archive term refuses every productive run: archive
    # 63 cards and the board legitimately drops 63. That is the version that
    # gets the check switched off in a week, and only the legitimate-shrink case
    # can see it.
    (
        "the floor ignores what this run archived, so every productive run refuses",
        "floor=$((prev_total - archived_now))",
        "floor=$prev_total",
    ),
    # Off by one, in the direction that lets one card vanish per run. Slow
    # enough to look like nothing; only the two boundary cases can see it.
    (
        "the floor is one card loose, so a single card may vanish per run",
        "floor=$((prev_total - archived_now))",
        "floor=$((prev_total - archived_now - 1))",
    ),
    # --- (B) the cannot-tell branch ----------------------------------------
    #
    # The failure this whole ticket is about is a check that reports clean
    # having compared nothing. Silence here recreates it exactly: no baseline,
    # no comparison, no warning, green.
    (
        "a run with no baseline says nothing, so 'never compared' reads as 'clean'",
        "::warning::no previous board size",
        "no previous board size",
    ),
    # DELIBERATELY NOT MUTATED: swapping the `if` back to a
    # `[ -s prev.total ] && prev_total=$(cat prev.total)` short-circuit. It was
    # written as a mutation, went UNCAUGHT, and the measurement is why -- `set
    # -e` exempts every command of an AND-OR list but the last, so mid-step the
    # two forms behave identically and NO case can tell them apart. Rule 5's
    # answer to an undetectable mutation is to strengthen the suite or admit the
    # requirement is not there; here it is not there, and the `if` is a
    # position-independence habit rather than a behaviour. Kept as a note so the
    # next reader does not re-add the case and re-discover this the same way.
    # --- (C) the wiring that makes tomorrow's comparison possible ----------
    #
    # `if: always()` here would write a SHRUNKEN total forward as the new
    # baseline, ratcheting the floor down to meet the defect until the check
    # can never fire again. The guard would still be present, still green, and
    # permanently blind.
    (
        "the baseline is recorded even on a failed run, ratcheting the floor down "
        "to meet the defect",
        """      - name: Record this run's board size for the next one
        if: env.DRY_RUN != 'true'""",
        """      - name: Record this run's board size for the next one
        if: always()""",
    ),
    # The producer half of the pair. No upload, no baseline, ever -- every
    # future run takes the cannot-tell branch and the comparison is dead code.
    (
        "the baseline artifact is never written, so no future run can compare",
        "          name: board-baseline\n",
        "          name: board-baseline-disabled\n",
    ),
]


def apply_one(src, old, new):
    n = src.count(old)
    if n != 1:
        raise LookupError("anchor matched %d times, expected exactly 1: %r"
                          % (n, old[:90]))
    out = src.replace(old, new, 1)
    return None if out == src else out


def main():
    dry = "--dry" in sys.argv

    # Refuse rather than measure against a baseline nothing vouches for
    # (backend#2441). Only the writing path: `--dry` writes nothing and runs in
    # the pre-push tier, where refusing on an uncommitted edit would block
    # whoever is editing the workflow.
    if not dry:
        rc = mutation_baseline.guard(ROOT, [WORKFLOW])
        if rc:
            return rc

    pristine = WORKFLOW.read_text(encoding="utf-8")
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
        WORKFLOW.write_text(mutated, encoding="utf-8")
        try:
            run = subprocess.run(
                [sys.executable, "-B", str(SUITE)],
                capture_output=True, text=True, cwd=str(ROOT),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        finally:
            # ALWAYS restore, including on a crash. A mutation left on disk makes
            # every later run measure the wrong file, and the tell is a suite that
            # reddens for reasons nobody typed.
            WORKFLOW.write_text(pristine, encoding="utf-8")
        # A crash counts as caught ONLY if the suite actually ran and judged. A
        # bare traceback means the mutation broke the harness rather than being
        # detected by a case -- not coverage, and never logged as if it were.
        # The extraction anchors are deliberately INSIDE this: a mutation that
        # makes the block unextractable is caught by the suite's own refusal,
        # which prints the marker first.
        reported = "archive-baseline-selftest:" in run.stdout
        caught = [ln.strip() for ln in run.stdout.splitlines()
                  if ln.strip().startswith("FAIL ")]
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (label, "; ".join(caught)[:150]))
        elif not reported:
            # The suite exits before its marker only when extraction failed,
            # which IS a detection -- of a differently-shaped break. Distinguish
            # it so the log never reads as though a case did the work.
            detail = (run.stdout + run.stderr).strip().splitlines()
            hint = detail[-1][:140] if detail else "no output"
            if run.returncode != 0:
                print("  caught     %s\n             by: extraction refused -- %s" % (label, hint))
            else:
                uncaught.append((label, "the suite did not report and did not fail"))
                print("  UNCAUGHT   %s (harness broke silently)" % label)
        else:
            uncaught.append((label, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % label)

    if WORKFLOW.read_text(encoding="utf-8") != pristine:
        print("::error::%s was left mutated - restore it from git before "
              "trusting any later run" % WORKFLOW)
        return 2

    if stale:
        print("\n::error::%d anchor(s) did not apply. A mutation that does not land "
              "is inert, and an inert mutation is indistinguishable from good "
              "coverage in this log (CLAUDE.md rule 5):" % len(stale))
        for label, why in stale:
            print("  - %s\n      %s" % (label, why))
    if uncaught:
        print("\n::error::%d mutation(s) went UNCAUGHT. The suite passed with the "
              "rule broken, so those assertions are vacuous - strengthen them "
              "rather than deleting the case:" % len(uncaught))
        for label, why in uncaught:
            print("  - %s\n      %s" % (label, why))
    if stale or uncaught:
        return 1

    verb = "resolved" if dry else "caught"
    print("\n%d/%d mutations %s." % (len(MUTATIONS), len(MUTATIONS), verb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
