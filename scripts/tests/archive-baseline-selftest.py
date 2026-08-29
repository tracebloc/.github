#!/usr/bin/env python3
"""The archive's CROSS-RUN floor is read out of the workflow and exercised.

WHY THIS EXISTS (backend#2802)

`kanban-archive.yml` carried three completeness checks and every one of them
compared two numbers produced by the SAME credential in the SAME run --
`reread_total` against the server's `totalCount`, and the re-read against the
first read. Numbers from one view shrink together, so all three agree while the
view itself is partial. Measured 2026-08-28, two runs two minutes apart:

    07:38Z  "paginated 93 item(s), server totalCount=93 (agree)"
    07:40Z  "paginated 30 item(s), server totalCount=30 (agree)"   <- 1 archived

while a PAT reading the same board saw 742. Each run was internally consistent,
both were wrong, and nothing inside either could say so: the disagreement is
BETWEEN runs and every number was inside one. That is CLAUDE.md rule 3 rendering
as agreement -- "cannot tell" printed as a pass -- and rule 1, a check holding
its own copy of the answer.

The workflow now carries yesterday's `totalCount` forward as an artifact.
Archiving is the only way a card leaves this board, so

    floor = previous_total - archived_this_run

is a hard lower bound on today's size, and a total beneath it is a shrunken
view rather than a smaller board.

WHY IT IS EXTRACTED RATHER THAN COPIED

Rule 9. A copy of the comparison here would let the workflow drift while this
file stayed green -- the same defect class the check itself exists to close. So
the shell under test is pulled out of the YAML by anchor and run verbatim; if
someone reshapes or renames it, the anchors miss and this fails loudly instead
of proving a stale duplicate.

WHY THE NO-BASELINE CASE IS A WARNING AND STILL ASSERTED

A first run, and the run after any retention lapse, has nothing to compare
against. Failing there would be a red nobody could clear, which is the "never
land a red gate" half of rule 4. But silence would make the one shape this
check exists to catch indistinguishable from a pass, so the run must SAY it
could not tell -- and this suite asserts that it does.

Exit 0 when every case behaves as specified.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = os.path.join(HERE, os.pardir, os.pardir, ".github", "workflows", "kanban-archive.yml")

# The region under test, by its own comment banner and its trailing write. Both
# anchors must resolve or the extraction is stale -- see the module docstring.
START = "# selftest:xrun-begin"
END = "# selftest:xrun-end"


def extract() -> str:
    doc = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    steps = doc["jobs"]["archive"]["steps"]
    named = [s for s in steps if s.get("name") == "Assert the board is clean"]
    if len(named) != 1:
        sys.exit(
            f"expected exactly one 'Assert the board is clean' step, found {len(named)}. "
            "The step was renamed or duplicated; this suite is pointing at nothing."
        )
    run = named[0]["run"]
    for anchor in (START, END):
        if run.count(anchor) != 1:
            sys.exit(
                f"anchor {anchor!r} matched {run.count(anchor)} time(s) in the assert step, "
                "so the cross-run comparison could not be extracted. Re-point this suite "
                "rather than deleting the case -- an unextractable guard is untested."
            )
    return run[run.index(START):run.index(END)]


BLOCK = extract()


def run_case(declared: int, previous, archived: int, unreadable: str = "",
             other_archiver: str = "", view_bad: int = 0,
             first_total=None, reread_total=None):
    """Run the REAL block with the files it reads, and report what it decided.

    `unreadable` writes the `prev.error` marker the recall step leaves when the
    baseline LOOKUP broke, as opposed to there being no baseline. Both leave
    `prev.total` empty, which is exactly why the block has to be handed the
    difference rather than inferring it.
    """
    work = tempfile.mkdtemp()
    if previous is not None:
        with open(os.path.join(work, "prev.total"), "w") as fh:
            fh.write(str(previous))
    if unreadable:
        with open(os.path.join(work, "prev.error"), "w") as fh:
            fh.write(unreadable)
    if other_archiver:
        with open(os.path.join(work, "prev.otherarchiver"), "w") as fh:
            fh.write(other_archiver)
    with open(os.path.join(work, "archived.count"), "w") as fh:
        fh.write(str(archived))
    # `set -euo pipefail` exactly as the step runs it: the absent-baseline case
    # must survive `set -e`, which a short-circuit `[ -s f ] && v=$(cat f)` would
    # not -- so running under weaker flags here would hide a real failure.
    script = (
        "set -euo pipefail\n"
        f"cd {work}\n"
        "fail=0\n"
        f"view_bad={view_bad}\n"
        # The within-run identity brackets THIS job's own archiving: `first_total`
        # is the PRE-archive read and `declared`/`reread_total` the post-archive
        # one, so a consistent default is `declared + archived`. Cases that target
        # the identity supply their own pair.
        f"first_total={declared + archived if first_total is None else first_total}\n"
        f"reread_total={declared if reread_total is None else reread_total}\n"
        f"declared_total={declared}\n" + BLOCK
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    recorded = os.path.join(work, "board.total")
    wrote = open(recorded).read().strip() if os.path.exists(recorded) else None
    return proc.returncode, proc.stdout + proc.stderr, wrote


# (name, declared, previous, archived, must_refuse, unreadable, other_archiver,
#  view_bad, must_record)
CASES = [
    # THE MEASURED ONE. 93 -> 30 with a single item archived is the incident
    # this check was filed for, and the shape every same-run check called clean.
    ("the measured 93 -> 30 with one archive", 30, 93, 1, True, "", "", 0, False),
    # The same arithmetic when the archive REALLY did remove them: not a finding.
    # Without this case the check could be "always refuse a smaller board", which
    # would redden every productive run and be switched off within a week.
    ("a legitimate shrink: 63 archived", 30, 93, 63, False, "", "", 0, True),
    # Growth is normal -- cards are added freely and only leave by archiving.
    ("the board grew", 742, 93, 0, False, "", "", 0, True),
    # First run / retention lapse: cannot tell, must not refuse, must SAY SO.
    ("no baseline available", 30, None, 1, False, "", "", 0, True),
    # The boundary, both sides. An off-by-one here silently widens or narrows the
    # floor by one card, which no coarser case can see.
    ("exactly on the floor", 92, 93, 1, False, "", "", 0, True),
    ("one card below the floor", 91, 93, 1, True, "", "", 0, False),
    # THE LOOKUP BROKE, which is NOT an empty history (Bugbot, .github#383).
    # Both states leave `prev.total` empty, so without this case the block can
    # report the benign first-run warning and pass on a run that compared
    # nothing -- this ticket's own defect, one layer inside its fix.
    ("the baseline lookup failed", 30, None, 1, True, "the artifact listing failed: HTTP 403", "", 0, True),
    # And it must refuse EVEN WHEN the numbers would otherwise have been fine,
    # or the refusal is really just the floor check wearing a different message.
    ("the lookup failed on an unremarkable board", 742, None, 0, True, "HTTP 500", "", 0, True),
    # ANOTHER ARCHIVER RAN SINCE (Bugbot High, .github#383). kanban-reconcile
    # calls archiveProjectV2Item too, every Monday at 04:00, an hour before this
    # job -- so on Mondays the board is legitimately smaller by a number no `ok`
    # of ours counted, and a floor of `previous - archived_now` would sit above
    # the real board. A red the calendar produces is the fastest way to get a
    # tier switched off, so this run says it could not compare instead.
    ("reconcile archived since the baseline", 30, 93, 1, False,
     "", "2026-08-31T04:12:00Z", 0, True),
    # THE RECORDING GATE. A run whose own read was incoherent must not enshrine
    # its count as tomorrow's floor -- that ratchets the floor down to meet the
    # defect until the check can never fire again.
    ("an incoherent read records no baseline", 30, 93, 63, False, "", "", 1, False),
]

# THE WITHIN-RUN IDENTITY, which needs no baseline and no other workflow: two
# reads bracket this job's own archiving, so `first_total - archived` is what the
# re-read MUST return. This is what catches the measured incident's actual
# transition -- 93 read, one archived, 30 re-read -- with no cross-run state at
# all, and it is the one check a steady-state blind credential cannot satisfy by
# being consistently wrong.
# (name, first_total, archived, reread_total, must_refuse)
IDENTITY_CASES = [
    ("the view collapsed between this run's two reads", 93, 1, 30, True),
    ("an ordinary run: 93 read, 63 archived, 30 left", 93, 63, 30, False),
    ("nothing archived and nothing changed", 700, 0, 700, False),
    # A view that GREW mid-run is equally incoherent -- a bound would wave it
    # through, and it means the two counts describe different boards just as much.
    ("the view grew between the two reads", 93, 1, 150, True),
    ("one item short of the identity", 93, 1, 91, True),
]


def wiring_failures() -> list:
    """The comparison being right is half the claim; the baseline surviving to
    the next run is the other half, and it lives in YAML the shell never reads.

    Without these, the artifact could stop being written -- or start being
    written by a FAILED run, ratcheting the floor down to meet the defect --
    and every future run would fall to the cannot-tell warning with nothing red
    to say the check had quietly stopped existing.
    """
    doc = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    steps = doc["jobs"]["archive"]["steps"]
    bad = []

    recall = [s for s in steps if s.get("name") == "Recall the previous run's board size"]
    if len(recall) != 1:
        bad.append("no single step recalls the previous run's board size, so the "
                   "comparison can never have a baseline to use")
    else:
        body = recall[0].get("run", "")
        if "board-baseline" not in body:
            bad.append("the recall step no longer names the `board-baseline` artifact")
        # THE PRODUCER OF THE DISTINCTION, checked against its consumer
        # (Bugbot, .github#383). "no baseline yet" and "the lookup broke" both
        # leave `prev.total` empty, so the assert step can only tell them apart
        # if the recall step MARKS the second -- on every path it can take. Drop
        # one marker and that path silently rejoins the benign warning, which is
        # this ticket's defect reproduced inside its own fix. Two failure paths
        # exist: the listing failing, and a live artifact that will not download.
        # DERIVED, NOT RESTATED -- and this is the second time in one change that
        # mattered. The first version asserted `writes >= 2` against a
        # hand-counted two failure paths; a third path was added the same day and
        # the check went on passing while a mutation deleted one of them. A count
        # written down beside the thing it counts is the defect rule 1 names.
        #
        # So the workflow marks each failure branch `# selftest:unreadable-path`
        # and the two counts must AGREE. Add a path and the check keeps working;
        # write the branch but forget the mark, and the counts diverge.
        # The `: > prev.error` initialiser is excluded -- counting it once let a
        # truncation stand in for a real write.
        writes = sum(
            1 for line in body.splitlines()
            if "> prev.error" in line and not line.strip().startswith(":")
        )
        marked = body.count("# selftest:unreadable-path")
        if writes != marked or writes == 0:
            bad.append(
                f"the recall step has {marked} branch(es) marked as an unreadable-baseline "
                f"path but writes `prev.error` on {writes}. An unmarked or unwritten path "
                "falls through to the benign first-run warning and passes, having compared "
                "nothing -- this ticket's own defect"
            )

    upload = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))]
    if len(upload) != 1:
        bad.append(f"expected exactly one upload-artifact step, found {len(upload)}; "
                   "nothing would carry this run's count forward")
        return bad

    step = upload[0]
    with_ = step.get("with") or {}
    if with_.get("name") != "board-baseline":
        bad.append(f"the baseline artifact is named {with_.get('name')!r}, but the "
                   "recall step downloads `board-baseline` -- the pair is broken")
    if with_.get("path") != "board.total":
        bad.append(f"the baseline uploads {with_.get('path')!r}, not the "
                   "`board.total` the assert step writes")
    # THE ANTI-RATCHET GUARD MOVED INTO THE SHELL (`view_bad`), so this step must
    # do the opposite of what an earlier revision asserted: it has to run even
    # when the assert step fails, or a run that archived legitimately and then
    # failed on something else would never record, and the next run would meet a
    # floor it can never get under. What the STEP still owes is that a
    # deliberately-unrecorded run is not an error.
    cond = str(step.get("if", ""))
    if "success()" in cond or ("cancelled" not in cond and "always" not in cond):
        bad.append(
            f"the baseline step's condition is {cond!r}, so it is skipped when the "
            "assert step fails. The decision not to record lives in that step now; "
            "gating the upload as well makes a single failure permanent -- the next "
            "run meets a stale floor, refuses, and never records either"
        )
    if "DRY_RUN" not in cond:
        bad.append(f"the baseline step's condition is {cond!r}; a dry run archives "
                   "nothing and must not set the floor")
    if (with_.get("if-no-files-found") or "").lower() != "ignore":
        bad.append(
            "the baseline step errors when board.total is absent, but an incoherent "
            "run writes no board.total ON PURPOSE. Erroring turns the anti-ratchet "
            "mechanism into a second failure and hides the real one"
        )
    return bad


def main() -> int:
    # THE MARKER THE MUTATION RUNNER READS. It tells "the suite ran and judged"
    # apart from "the mutation broke the harness", which are opposite results
    # that a non-zero exit alone cannot distinguish -- a traceback is not
    # coverage and must never be logged as a catch.
    print("archive-baseline-selftest: %d case(s)" % (len(CASES) + len(IDENTITY_CASES)))
    failures = 0
    for (name, declared, previous, archived, must_refuse, unreadable,
         other_archiver, view_bad, must_record) in CASES:
        rc, out, wrote = run_case(declared, previous, archived, unreadable,
                                  other_archiver, view_bad)
        # THE BLOCK MUST NEVER EXIT ON ITS OWN. It records its verdict in
        # `fail`; the step decides at the end. A non-zero exit here means the
        # shell died mid-block -- which under `set -e` is what an absent
        # baseline does to a `[ -s f ] && v=$(cat f)` short-circuit, turning
        # "nothing to compare against yet" into a failed job. Without this
        # assertion that regression reads as a clean pass, because a dead shell
        # prints no `::error::` either.
        if rc != 0:
            failures += 1
            print(f"FAIL {name}: the block exited {rc} instead of recording a verdict")
            print("     " + out.strip().replace("\n", "\n     "))
            continue
        refused = "::error::" in out
        if refused != must_refuse:
            failures += 1
            verb = "refused" if refused else "passed"
            want = "refuse" if must_refuse else "pass"
            print(f"FAIL {name}: the block {verb}, expected it to {want}")
            print("     " + out.strip().replace("\n", "\n     "))
            continue
        if (wrote is not None) != must_record:
            failures += 1
            did = "recorded" if wrote is not None else "recorded nothing"
            want = "record" if must_record else "record nothing"
            print(f"FAIL {name}: the block {did}, expected it to {want}")
            print("     " + out.strip().replace("\n", "\n     "))
            continue
        if other_archiver and "::warning::" not in out:
            failures += 1
            print(f"FAIL {name}: another archiver ran since the baseline and the run "
                  "said nothing. Passing in silence is the defect this check removes.")
            continue
        if previous is None and not unreadable and "::warning::" not in out:
            failures += 1
            print(
                f"FAIL {name}: no baseline and no warning. The run would report clean "
                "having never made the comparison -- 'cannot tell' has to be a finding."
            )
            continue
        print(f"ok   {name}")

    for name, first, archived, reread, must_refuse in IDENTITY_CASES:
        rc, out, _ = run_case(reread, None, archived,
                              first_total=first, reread_total=reread)
        if rc != 0:
            failures += 1
            print(f"FAIL identity/{name}: the block exited {rc}")
            continue
        refused = "must leave exactly" in out
        if refused != must_refuse:
            failures += 1
            verb = "refused" if refused else "passed"
            want = "refuse" if must_refuse else "pass"
            print(f"FAIL identity/{name}: the identity {verb}, expected it to {want}")
            print("     " + out.strip().replace("\n", "\n     "))
            continue
        print(f"ok   identity: {name}")

    wiring = wiring_failures()
    for why in wiring:
        failures += 1
        print(f"FAIL wiring: {why}")
    if not wiring:
        print("ok   the baseline survives a failing assert, and a dry run sets no floor")

    print()
    if failures:
        print(f"{failures} check(s) failed.")
        return 1
    print(f"{len(CASES)} cross-run + {len(IDENTITY_CASES)} identity cases "
          "+ the artifact wiring passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
