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
import re
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
             first_total=None, reread_total=None,
             arch_first: int = 0, arch_seen: int = 0):
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
    # seen.tsv is the FIRST read's per-item record; field 1 is `isArchived`. The
    # identity reads `arch_first` off it rather than being told, so this fixture
    # has to be the real file shape.
    with open(os.path.join(work, "seen.tsv"), "w") as fh:
        for i in range(arch_first):
            fh.write("true\tProd\n")
        fh.write("false\tReady\n")
    # `first_total` is READ from seen.total inside the block now, not injected --
    # the extraction was widened to cover the line that assigns it, because the
    # bare shrink check that lived just above it was invisible to this suite and
    # shipped red-on-success (backend#2820).
    ft = (declared + archived) if first_total is None else first_total
    with open(os.path.join(work, "seen.total"), "w") as fh:
        fh.write(str(ft))
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
        f"reread_total={declared if reread_total is None else reread_total}\n"
        f"arch_seen={arch_seen}\n"
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
# WHETHER ARCHIVING SHRINKS THE CONNECTION IS DERIVED (Bugbot High, .github#383),
# so both worlds are exercised. Measured on this board it OMITS archived items
# (run 33084151778 read `1626 (un-archived: 1626)` weeks after 83+ archives, and
# nothing calls deleteProjectV2Item) -- but the identity must not depend on that
# staying true, or a change to someone else's API turns it into a permanent red
# that fires on every productive run.
# `want_expected` is the number archiving accounts for, WRITTEN DOWN rather
# than recomputed from the shell's own formula -- iterating the formula to check
# the formula is the self-consistent shape rule 9's corollary warns about.
# (name, first_total, archived, reread_total, arch_first, arch_seen,
#  want_expected, must_refuse)
IDENTITY_CASES = [
    # --- the world this board is in: archived items leave the connection ---
    ("the view collapsed between this run's two reads", 93, 1, 30, 0, 0, 92, True),
    ("an ordinary run: 93 read, 63 archived, 30 left", 93, 63, 30, 0, 0, 30, False),
    ("nothing archived and nothing changed", 700, 0, 700, 0, 0, 700, False),
    # GROWTH IS NOT INCOHERENCE (backend#2833). The previous version of this
    # case asserted the opposite -- "a view that GREW mid-run is equally
    # incoherent" -- and it was wrong about the board it runs on.
    # `add-to-kanban` adds a card the instant anyone opens an issue or a PR, so
    # a card arriving between the two reads is the ordinary case, and refusing
    # it failed productive runs AND withheld the baseline, leaving the next
    # run's floor above the live board with no way to clear (rule 4).
    ("the view grew between the two reads", 93, 1, 150, 0, 0, 92, False),
    # The measured shape: a real archive with one card added underneath it.
    ("a card was added during a productive archive", 518, 49, 470, 0, 0, 469, False),
    # ...and the shrink direction keeps every card of its detection, including
    # the boundary. One below `expected` is still a refusal.
    ("one item short of the identity", 93, 1, 91, 0, 0, 92, True),
    # --- the other world: archived items STAY, so the size does not move ------
    # This is the case Bugbot argued we were already in. We are not, but if the
    # API ever changes the identity self-corrects instead of reddening forever.
    ("archived items are retained, so 93 stays 93", 93, 28, 93, 0, 28, 93, False),
    ("retained, with a board that already held archived items", 700, 41, 700, 12, 53, 700, False),
    # ...and it must still catch a genuine collapse in THAT world.
    ("retained, but the view collapsed anyway", 93, 28, 30, 0, 28, 93, True),
    # THE MEASURED PRODUCTIVE RUN (backend#2820). After #380 restored the
    # credential's sight, the daily cron read 749, archived 253 and re-read 496 --
    # and the run FAILED, because a bare `reread < first` check compared against
    # the PRE-archive count while both reads filter `isArchived == false`. The
    # guard fired on exactly the runs that did their job.
    #
    # This case is the anchor for that check staying gone. It is not hypothetical
    # arithmetic: these are the three numbers from the 13:37 UTC dispatch.
    ("the measured productive run: 749 read, 253 archived, 496 left",
     749, 253, 496, 0, 0, 496, False),
]


# THE DETECTORS BELOW KEY ON PRODUCTION TEXT, so they rot silently when a
# message is reworded: every case then reads as "passed" because the refusal
# string is simply never found. `anchor_failures` is what makes that red instead
# -- the same defect class as a mutation check that matches nothing (rule 9).
IDENT_REFUSAL = "that archiving accounts for"
OMISSION_REFUSAL = "Items are being omitted from the read"
LAG_WARNING = "has not finished catching up"
EXPECTED_LINE = "archiving accounts for"
ANCHORS = (IDENT_REFUSAL, OMISSION_REFUSAL, LAG_WARNING, EXPECTED_LINE)


def anchor_failures(block: str) -> list:
    return [
        f"the detector string {a!r} appears nowhere in the extracted block, so "
        "every case keyed on it silently reports a pass"
        for a in ANCHORS
        if a not in block
    ]


# The server's `totalCount` against what pagination actually returned. These
# need `declared_total != reread_total`, which IDENTITY_CASES cannot express
# (its runner passes declared = reread), so they get their own list.
#
# `first_total` is derived as `reread + archived` in the runner so the within-run
# identity agrees exactly and cannot confound the verdict -- a case where two
# guards can both fire tells you nothing about either.
# (name, declared_total, reread_total, archived, must_refuse, must_warn,
#  want_baseline)
#
# `want_baseline` IS WRITTEN DOWN PER CASE, not computed (backend#2833). It used
# to be `str(declared)` in the runner -- the implementation's own rule, restated
# in the place that was supposed to check it, so the two agreed by construction
# and the lag-path defect was invisible. Each value below is argued instead.
COMPLETENESS_CASES = [
    # THE MEASURED RUN (backend#2831, run 33301924089). First read 518, archived
    # 49, re-read paginated exactly 469 -- so the read was COMPLETE, 518-49=469
    # -- while the server still reported totalCount=475. The equality this
    # replaces failed the run for having archived.
    #
    # BASELINE 469, NOT 475: the run walked 469 cards. 475 is `totalCount` still
    # counting the 49 we just archived, which the warning on this very case says
    # out loud -- recording it hands tomorrow a floor above the real board, and
    # when the counter catches up the floor check blocks the corrected record
    # for ever (backend#2833).
    ("the measured run: 469 paginated, 475 counted, 49 archived",
     475, 469, 49, False, True, 469),
    ("counts agree", 100, 100, 10, False, False, 100),
    # More returned than counted is the other lag direction (a card added that
    # totalCount has not picked up). Nothing can be hidden by it: every item is
    # in hand.
    #
    # BASELINE 100, UNCHANGED. Here `totalCount` is the SMALLER of the two, and
    # the smaller is what gets recorded -- a floor that is too low
    # under-detects for one cycle and self-corrects, while one that is too high
    # cannot be cleared at all. This case is what stops the fix above being
    # "always trust the pagination".
    ("more returned than counted", 100, 103, 0, False, False, 100),
    # THE CEILING, both sides of it. The lag can only be still-counting the
    # cards this job archived, so the gap is bounded by `archived_now`.
    #
    # BASELINE 90 for the same reason as the measured run: 100 is the lagging
    # counter, 90 is the board.
    ("the gap is exactly the archive count", 100, 90, 10, False, True, 90),
    ("the gap is one wider than the archive count", 100, 89, 10, True, False, None),
    # ...and with nothing archived there is no lag to explain any gap at all.
    # This is the credential-blind omission the check exists for, undiminished.
    ("nothing archived, so a one-card gap is an omission",
     100, 99, 0, True, False, None),
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
    # THE WHOLE PRODUCER CHAIN MUST SURVIVE A FAILED ARCHIVE (Bugbot High, #383).
    # `board.total` is written by the assert step and read by the recall step, and
    # BOTH were gated on the implicit `success()` while only the upload carried
    # `!cancelled()`. So a run whose archive failed after archiving `ok` cards
    # recorded nothing, and the next run met a floor above the real board — a red
    # that clears only by accident. Asserting the upload alone left that half
    # untested, which is how it shipped: the mechanism is three steps, not one.
    for name in ("Recall the previous run's board size", "Assert the board is clean"):
        matches = [s for s in steps if s.get("name") == name]
        if len(matches) != 1:
            bad.append(f"expected exactly one {name!r} step, found {len(matches)}")
            continue
        cond = str(matches[0].get("if", ""))
        if "cancelled" not in cond and "always" not in cond:
            bad.append(
                f"{name!r} is gated on {cond!r}, which is an implicit success() — a "
                "failed archive skips it, so the cards it DID archive never reach "
                "board.total and the next run's floor sits above the real board"
            )
        if "DRY_RUN" not in cond:
            bad.append(f"{name!r} is gated on {cond!r}; a dry run must not set the floor")

    # A RECONCILE THAT ARCHIVED AND THEN FAILED STILL ARCHIVED (Bugbot, #383).
    # Its apply loop counts each archive and fails closed at the end, so filtering
    # the probe to `status=success` hid exactly the runs whose cards are missing
    # from the board — and the floor then read them as a shrunken view.
    recall_body = ""
    r = [s for s in steps if s.get("name") == "Recall the previous run's board size"]
    if r:
        recall_body = r[0].get("run", "")
    if "kanban-reconcile.yml/runs" in recall_body and "status=completed" not in recall_body:
        bad.append(
            "the other-archiver probe does not ask for `status=completed`; a "
            "reconcile run that archived cards and then failed is invisible to it, "
            "and its cards read as a shrunken view"
        )

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
    print("archive-baseline-selftest: %d case(s)"
          % (len(CASES) + len(IDENTITY_CASES) + len(COMPLETENESS_CASES)))
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

    for (name, first, archived, reread, arch_first, arch_seen,
         want_expected, must_refuse) in IDENTITY_CASES:
        rc, out, wrote = run_case(reread, None, archived,
                                  first_total=first, reread_total=reread,
                                  arch_first=arch_first, arch_seen=arch_seen)
        if rc != 0:
            failures += 1
            print(f"FAIL identity/{name}: the block exited {rc}")
            continue
        got = re.search(r"archiving accounts for (\d+) item", out)
        if not got or int(got.group(1)) != want_expected:
            failures += 1
            shown = got.group(1) if got else "nothing"
            print(f"FAIL identity/{name}: archiving accounts for {shown}, "
                  f"expected {want_expected} -- the identity's arithmetic is wrong, "
                  "which a lower bound alone cannot see")
            continue
        refused = IDENT_REFUSAL in out
        if refused != must_refuse:
            failures += 1
            verb = "refused" if refused else "passed"
            want = "refuse" if must_refuse else "pass"
            print(f"FAIL identity/{name}: the identity {verb}, expected it to {want}")
            print("     " + out.strip().replace("\n", "\n     "))
            continue
        # THE HALF #2833 WAS ACTUALLY ABOUT. Refusing a growing board was only
        # the visible symptom; the damage was `view_bad` withholding the
        # baseline, so the next run compared against a pre-growth floor it could
        # never get under. A passing case MUST leave a baseline behind.
        want_record = None if must_refuse else str(reread)
        if wrote != want_record:
            failures += 1
            print(f"FAIL identity/{name}: recorded baseline {wrote!r}, "
                  f"expected {want_record!r} -- the anti-ratchet is miswired")
            continue
        print(f"ok   identity: {name}")

    for (name, declared, reread, archived,
         must_refuse, must_warn, want_baseline) in COMPLETENESS_CASES:
        rc, out, wrote = run_case(declared, None, archived,
                                  first_total=reread + archived,
                                  reread_total=reread)
        if rc != 0:
            failures += 1
            print(f"FAIL completeness/{name}: the block exited {rc}")
            continue
        refused = OMISSION_REFUSAL in out
        warned = LAG_WARNING in out
        if refused != must_refuse or warned != must_warn:
            failures += 1
            print(f"FAIL completeness/{name}: refused={refused} warned={warned}, "
                  f"expected refused={must_refuse} warned={must_warn}")
            print("     " + out.strip().replace("\n", "\n     "))
            continue
        # A LAG WARNING IS NOT A BAD VIEW. If the warning path also withheld the
        # baseline, every productive run would starve the next one's floor --
        # which is #2833's mechanism arriving by a second door.
        want_record = None if want_baseline is None else str(want_baseline)
        if wrote != want_record:
            failures += 1
            print(f"FAIL completeness/{name}: recorded baseline {wrote!r}, "
                  f"expected {want_record!r} -- a baseline above the real board "
                  "cannot be cleared by any later run (backend#2833)")
            continue
        print(f"ok   completeness: {name}")

    stale = anchor_failures(BLOCK)
    for why in stale:
        failures += 1
        print(f"FAIL anchor: {why}")
    if not stale:
        print("ok   every detector string still appears in the workflow")

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
    print(f"{len(CASES)} cross-run + {len(IDENTITY_CASES)} identity + "
          f"{len(COMPLETENESS_CASES)} completeness cases "
          "+ the detector anchors + the artifact wiring passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
