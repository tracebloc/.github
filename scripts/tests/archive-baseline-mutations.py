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
        'if [ "$board_size" -lt "$floor" ]; then',
        'if [ "$board_size" -gt "$floor" ]; then',
    ),
    # backend#2833 introduced `board_size` as min(declared_total, reread_total)
    # and the whole fix rests on it being the SMALLER. Taking the larger puts the
    # lag path back exactly where it was: `totalCount` still counting cards this
    # job archived becomes tomorrow's baseline, tomorrow's floor sits above the
    # real board, and no later run can ever write a corrected one.
    (
        "board_size takes the LARGER count, restoring the unclearable baseline",
        'if [ "$reread_total" -lt "$board_size" ]; then board_size="$reread_total"; fi',
        'if [ "$reread_total" -gt "$board_size" ]; then board_size="$reread_total"; fi',
    ),
    # The narrowing dropped entirely -- board_size stays `declared_total`, which
    # is the pre-#2833 behaviour and the defect itself.
    (
        "the narrowing is removed, so board_size is just the server's counter",
        'if [ "$reread_total" -lt "$board_size" ]; then board_size="$reread_total"; fi',
        ":",
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
    # --- (C) unreadable is not absent (Bugbot, .github#383) ----------------
    #
    # The finding that came back on the first push, and the one worth the most:
    # a failed lookup and an empty history both leave `prev.total` empty, so
    # collapsing them lets an API failure render as the benign first-run
    # warning -- a run that compared nothing, reporting clean. That is this
    # ticket's own defect, one layer inside its fix.
    (
        "an unreadable baseline falls through to the benign first-run warning",
        'if [ -s prev.error ]; then',
        'if [ -n "${NEVER_SET:-}" ]; then',
    ),
    # The producer half. Marking only the listing failure leaves the
    # download-failed path rejoining the warning, which is the same bug with a
    # smaller blast radius and no way to notice it.
    (
        "one of the lookup failure paths stops marking itself unreadable",
        # RE-ANCHORED for backend#2802. The old anchor was the single-line
        # `printf ... > prev.error`; that branch now captures `gh run download`'s
        # stderr first and APPENDS the summary to it, so the literal no longer
        # exists. The harness caught this itself -- "anchor matched 0 times" --
        # rather than reporting 0 uncaught about a premise nobody typed, which is
        # the behaviour that made the re-anchor necessary instead of optional.
        "                printf 'a live board-baseline artifact exists (run %s) but could not be downloaded. gh said: %s\\n' \\\n"
        "                  \"$rid\" \"${reason:-<no stderr captured>}\" > prev.error",
        '                echo "could not download the baseline"',
    ),
    # The marker/write pairing that lets the suite DERIVE how many failure paths
    # exist. Drop a marker and the counts diverge; that is the whole mechanism.
    (
        "a failure path is written but not marked, so the derived count goes stale",
        "            # selftest:unreadable-path\n            printf 'the artifact listing failed:",
        "            printf 'the artifact listing failed:",
    ),
    # --- (D) the wiring that makes tomorrow's comparison possible ----------
    #
    # `if: always()` here would write a SHRUNKEN total forward as the new
    # baseline, ratcheting the floor down to meet the defect until the check
    # can never fire again. The guard would still be present, still green, and
    # permanently blind.
    # --- (F) the producer chain, and the other archiver's failed runs (#383) --
    # REPLACES the condition rather than inserting a second `if:` -- the first
    # attempt did the latter, which gives the step DUPLICATE KEYS, and PyYAML
    # keeps the last one. The step was therefore unchanged, the suite passed, and
    # the mutation reported UNCAUGHT for a reason about the mutation rather than
    # about the coverage. An invalid mutation is not a finding.
    (
        "the assert step goes back to implicit success(), so a failed archive records nothing",
        "        if: ${{ !cancelled() && env.DRY_RUN != 'true' }}\n"
        "        env:\n"
        "          GH_TOKEN: ${{ steps.app-token.outputs.token }}\n"
        "        run: |\n"
        "          set -euo pipefail\n"
        "          # ASSERT THE BOARD IS CLEAN",
        "        if: env.DRY_RUN != 'true'\n"
        "        env:\n"
        "          GH_TOKEN: ${{ steps.app-token.outputs.token }}\n"
        "        run: |\n"
        "          set -euo pipefail\n"
        "          # ASSERT THE BOARD IS CLEAN",
    ),
    (
        "the other-archiver probe filters to successful reconcile runs again",
        "runs?status=completed&per_page=1",
        "runs?status=success&per_page=1",
    ),
    (
        "the upload is gated on the assert step succeeding, making one failure permanent",
        # ANCHORED ON THE STEP, not on the condition alone: all three steps in
        # the producer chain now carry the same `!cancelled()` guard, so the bare
        # condition matches three times and the mutation would land on an
        # arbitrary one -- reporting a result about a step nobody chose.
        "        if: ${{ !cancelled() && env.DRY_RUN != 'true' }}\n"
        "        uses: actions/upload-artifact@",
        "        if: ${{ success() && env.DRY_RUN != 'true' }}\n"
        "        uses: actions/upload-artifact@",
    ),
    (
        "a deliberately-unrecorded run is reported as a missing-file error",
        "          if-no-files-found: ignore",
        "          if-no-files-found: error",
    ),
    # --- (E) the two Highs from the first review ---------------------------
    #
    # The recording gate. Enshrining an incoherent count ratchets the floor down
    # to meet the defect until the check can never fire again -- present, green,
    # permanently blind.
    (
        "an incoherent read still records its count as tomorrow's floor",
        '          if [ "$view_bad" -eq 0 ]; then',
        "          if true; then",
    ),
    # The other-archiver branch. kanban-reconcile archives too, so without this
    # the check goes red every Monday for a reason that is not a defect.
    (
        "a Monday reconcile is treated as a shrunken view, reddening on the calendar",
        "          elif [ -s prev.otherarchiver ]; then",
        '          elif [ -n "${NEVER_SET:-}" ]; then',
    ),
    # The exact within-run identity, which needs no baseline at all and is what
    # catches a view collapsing BETWEEN this job's own two reads.
    # The premise-free correction. Dropping it re-hardcodes "archiving always
    # shrinks the connection" -- true on this board today, and a permanent red
    # firing on every productive run if that ever changes (Bugbot High, #383).
    (
        "the identity re-assumes archived items always leave the connection",
        "          expected=$((first_total - archived_now + arch_seen - arch_first))",
        "          expected=$((first_total - archived_now))",
    ),
    # The identity is DIRECTIONAL now (backend#2833) -- growth is ordinary on a
    # board `add-to-kanban` writes to continuously, shrinkage is the signal --
    # so the two ways to break it are to point it the wrong way and to make it
    # exact again. Both are the shape it was mutated for before; the anchor
    # simply moved from `-ne` to `-lt`, and this list went red until it did,
    # which is the anchor assertion doing its job.
    (
        "the within-run identity points the wrong way, so a collapsed view passes "
        "and a growing board refuses",
        '          if [ "$reread_total" -lt "$expected" ]; then',
        '          if [ "$reread_total" -gt "$expected" ]; then',
    ),
    (
        "the within-run identity is exact again, so every card added mid-run "
        "refuses the run and withholds the baseline",
        '          if [ "$reread_total" -lt "$expected" ]; then',
        '          if [ "$reread_total" -ne "$expected" ]; then',
    ),
    # --- (E) the totalCount ceiling (backend#2831) --------------------------
    #
    # `totalCount` lags behind a bulk archive, so the gap is tolerated only up
    # to what this job archived. Unbound it and the omission check is gone;
    # remove the tolerance and every productive run refuses. Only cases on both
    # sides of the ceiling can tell those apart.
    (
        "the totalCount gap is tolerated without limit, so a credential-blind "
        "omission passes",
        '          elif [ "$((declared_total - reread_total))" -le "$archived_now" ]; then',
        '          elif [ "$((declared_total - reread_total))" -ge 0 ]; then',
    ),
    (
        "the totalCount gap is never tolerated, so archive lag refuses every "
        "productive run",
        '          elif [ "$((declared_total - reread_total))" -le "$archived_now" ]; then',
        '          elif [ "$((declared_total - reread_total))" -lt 0 ]; then',
    ),
    # The producer half of the pair. No upload, no baseline, ever -- every
    # future run takes the cannot-tell branch and the comparison is dead code.
    (
        "the baseline artifact is never written, so no future run can compare",
        "          name: board-baseline\n",
        "          name: board-baseline-disabled\n",
    ),
    # --- (F) the paginated listing (backend#2903, reviewer .github#393) ------
    #
    # Drop `--paginate` and the listing reads only the first page again -- the live
    # baseline can sit on a later page and a shrunken baseline is enshrined against
    # no real one. This is the bug this fix exists to close; restoring it must redden
    # the pagination requirement assertion, or that assertion is decoration.
    (
        "the artifact listing reads only the first page (no --paginate)",
        # RE-ANCHORED for backend#3068: the listing is now wrapped in `retry_read`
        # and the trailing `2>&1` moved inside the helper, so the old literal is
        # gone. Dropping `--paginate` (keeping the retry + `--jq`) is still the bug
        # the wiring `--paginate` assertion catches.
        'if ! arts=$(retry_read gh api --paginate "repos/$GITHUB_REPOSITORY/actions/artifacts?name=board-baseline&per_page=100" --jq \'.artifacts[]\'); then',
        'if ! arts=$(retry_read gh api "repos/$GITHUB_REPOSITORY/actions/artifacts?name=board-baseline&per_page=100" --jq \'.artifacts[]\'); then',
    ),
    # AND THE SLURP. The paginated stream is one artifact per line; reading it with
    # a plain `jq '.artifacts[]'` (the single-page idiom) selects nothing, so live=0
    # and the run reads as a first run -- the same enshrine-a-shrunken-baseline bug
    # by a different route. Reverting the slurp must redden.
    (
        "the paginated artifact stream is read without -s, so it selects nothing",
        "live=$(printf '%s' \"$arts\" | jq -s '[.[] | select(.expired == false)] | length')",
        "live=$(printf '%s' \"$arts\" | jq '[.artifacts[] | select(.expired == false)] | length')",
    ),
    # --- (G) the baseline read retries a transient blip, then fails closed -----
    # (backend#3068). A single attempt failed ~60% of this cron's runs, and each
    # failure refuses the whole cross-run comparison -- a red run that hid a green
    # archive. Retrying fixes that, but an EXHAUSTED read must STILL fail closed:
    # an unreadable baseline that reads as "nothing to archive" is this ticket's
    # own defect one layer in. The four mutations below break each half of each
    # loop; the recall-read cases in the selftest are what catch them.
    #
    # The DOWNLOAD stops retrying: `-ge 1` gives up after the first attempt, so a
    # transient blip within budget refuses instead of recovering. The
    # "download recovers after two transient failures" case reddens.
    (
        "the baseline download gives up after the first attempt (no retry)",
        '                if [ "$dl_attempt" -ge "${BASELINE_READ_RETRIES:-4}" ]; then',
        '                if [ "$dl_attempt" -ge 1 ]; then',
    ),
    # The DOWNLOAD stops failing closed: exhausting the retries reports success,
    # so a download that never lands reads as a clean baseline. The
    # "download fails every attempt, so the read fails closed" case reddens.
    (
        "an exhausted baseline download reports success, so an unreadable read reads clean",
        "                if [ \"$dl_attempt\" -ge \"${BASELINE_READ_RETRIES:-4}\" ]; then\n"
        "                  break\n"
        "                fi",
        "                if [ \"$dl_attempt\" -ge \"${BASELINE_READ_RETRIES:-4}\" ]; then\n"
        "                  dl_ok=1\n"
        "                  break\n"
        "                fi",
    ),
    # `retry_read` (the listing / reconcile reader) stops retrying: `-ge 1` gives
    # up after the first attempt. The "listing recovers after one transient
    # failure" case reddens.
    (
        "retry_read gives up after the first attempt, so a transient listing blip refuses",
        '              if [ "$attempt" -ge "$max" ]; then',
        '              if [ "$attempt" -ge 1 ]; then',
    ),
    # `retry_read` stops failing closed: it returns 0 on exhaustion, so a listing
    # that never lands is treated as a successful (error-text) read and the run
    # falls through to a clean first-run baseline. The "listing fails every
    # attempt, so the read fails closed" case reddens.
    (
        "retry_read returns success on exhaustion, so an unreadable listing reads clean",
        '                echo "::warning::a board-baseline read failed after $attempt attempt(s); failing closed." >&2\n'
        '                return "$rc"',
        '                echo "::warning::a board-baseline read failed after $attempt attempt(s); failing closed." >&2\n'
        '                return 0',
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
