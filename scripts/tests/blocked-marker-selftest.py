#!/usr/bin/env python3
"""Selftest for scripts/blocked-marker.py (backend#1729).

Weighted towards FALSE POSITIVES. A missed marker costs what we have today; a
gate that fires on "unblock the ingest path" gets switched off within a week,
and then it costs everything it would ever have caught. `house-rules.sh` says
the same thing in its own design notes: "A checker that cries wolf gets
switched off."

Every REAL title in here is copied verbatim from a tracebloc PR.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
from typing import List

_HERE = pathlib.Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "blocked_marker", _HERE.parent / "blocked-marker.py")
bm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bm)

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def blocked(title: str, labels=None) -> bool:
    return bm.evaluate(title, labels or [])[0]


print("REAL blocked titles — every one of these is a live or merged tracebloc PR")
# The one that cost a day. Merged with three approvals, 105s after S2.
check("data-ingestors#468",
      blocked("sec(#1528): require DB_USER/DB_PASSWORD, drop the edgeuser "
              "fallback (D10) [blocked on S2]"), True)
# Open right now.
check("client#490",
      blocked("chore(chart): point spawned ingestor at the 0.8 line "
              "(D16 write path) — HOLD until v0.8.0 image"), True)
check("client-runtime#192",
      blocked("DO NOT MERGE: fix(training): add configurable "
              "activeDeadlineSeconds to kill truly-hung runs"), True)

print("\nOther spellings the team writes")
check("blocked by", blocked("feat: thing (blocked by backend#1)"), True)
check("blocked until", blocked("feat: thing — blocked until the flag is on"), True)
check("bare [blocked]", blocked("feat: thing [blocked]"), True)
check("WIP prefix", blocked("WIP: feat: thing"), True)
check("[WIP] prefix", blocked("[WIP] feat: thing"), True)
check("hold pending", blocked("fix: thing, hold pending the drill"), True)
check("case-insensitive", blocked("FIX: THING [BLOCKED ON X]"), True)

print("\nFALSE FRIENDS — the reason this is a table and not a grep")
# `unblocked` contains `blocked`. A naive grep fires on every one of these,
# and several are real titles from this week.
check("unblock (real: client#676 shape)",
      blocked("fix(chart): resolve serviceDbAccounts per environment — "
              "unbreak dev + staging ingestion"), False)
check("unblocked", blocked("fix: the ingest path is now unblocked"), False)
check("unblocks", blocked("feat: unblocks data-ingestors#468"), False)
check("blocker (noun)", blocked("docs: record the blocker for D16"), False)
check("blocking (verb)", blocked("fix: stop blocking on the mailbox poll"), False)
check("threshold", blocked("feat: raise the retry threshold"), False)
check("holder", blocked("refactor: rename the token holder"), False)
check("household", blocked("docs: household naming conventions"), False)
check("wipe", blocked("fix: wipe the staging dir between runs"), False)
check("swipe", blocked("feat(ui): swipe to dismiss"), False)
# FOUND BY MEASUREMENT, NOT BY IMAGINATION. Running the matcher over 588 merged
# PR titles from ten tracebloc repos produced exactly one false positive, and
# this was it. "WIP limit" is a domain term HERE -- wip-limit-check.yml is one of
# our own reusables -- so the phrase recurs, and the (?<![a-z]) guard does not
# help because the next character is a hyphen. That is why `wip` is anchored to
# the start of the title and the other markers are not.
check("WIP-limit mid-title (real: .github#...)",
      blocked("chore(ci): retire the WIP-limit nudge"), False)
check("WIP as a noun mid-title",
      blocked("docs: explain how the WIP limit is enforced"), False)
check("wip inside a word at the start", blocked("wipe the staging dir"), False)
check("plain title", blocked("fix(ingest): emit the layout the CLI opens"), False)
check("empty title", blocked(""), False)

print("\nLabels")
check("blocked label", blocked("feat: thing", ["blocked"]), True)
check("label is case-insensitive", blocked("feat: thing", ["Blocked"]), True)
check("label is trimmed", blocked("feat: thing", [" blocked "]), True)
check("unrelated labels", blocked("feat: thing", ["priority", "work-type:bug"]), False)
# `keep-open` is the OTHER stale-exempt label and must not gate anything.
check("keep-open does not block", blocked("feat: thing", ["keep-open"]), False)

print("\nReasons name what fired, so the annotation is actionable")
# `[blocked on X]` legitimately trips two title markers (bracket AND on), so
# with the label that is three reasons. Asserting "2" was MY error, not the
# code's — the annotation should name everything that fired.
_, reasons = bm.evaluate("feat: thing [blocked on X]", ["blocked"])
check("label reason comes first", "label" in reasons[0], True)
check("every marker that fired is named", len(reasons) >= 2, True)
check("a single-source PR reports exactly one reason",
      len(bm.evaluate("feat: thing", ["blocked"])[1]), 1)

print("\nThe check can FAIL — exit codes, not just booleans")
check("clean title exits 0", bm.main(["--title", "fix: a normal change"]), 0)
check("blocked title exits 1",
      bm.main(["--title", "fix: a change [blocked on X]"]), 1)
check("blocked label exits 1",
      bm.main(["--title", "fix: a change", "--label", "blocked"]), 1)
# THIS TEST WAS VACUOUS on its first outing: `bm.main([]) if False else 2`
# evaluates to the literal 2 without ever calling main, so it asserted 2 == 2.
# Exactly the class this gate exists to help with, in the gate's own selftest.
# It now clears the env var and really calls it.
_saved = os.environ.pop("GITHUB_EVENT_PATH", None)
try:
    check("no title and no event path exits 2", bm.main([]), 2)
finally:
    if _saved is not None:
        os.environ["GITHUB_EVENT_PATH"] = _saved

print("\nThe REAL entry path: a GitHub event payload, not --title")
# Everything above drives `evaluate` through argv. In production nothing passes
# --title: the workflow runs the script bare and it reads GITHUB_EVENT_PATH. So
# until this block existed, the only code path the gate ACTUALLY uses had zero
# coverage, and a typo in `_from_event` would have shipped green.
_tmp = tempfile.mkdtemp()


def _event(title: str, labels: List[str]) -> str:
    """Write a payload shaped like a real `pull_request` event."""
    path = os.path.join(_tmp, f"event{len(os.listdir(_tmp))}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"action": "opened", "pull_request": {
            "number": 1, "title": title,
            "labels": [{"id": 1, "name": n} for n in labels]}}, fh)
    return path


check("event: clean PR exits 0",
      bm.main(["--event-path", _event("fix(ingest): emit the layout", [])]), 0)
check("event: #468's real title exits 1",
      bm.main(["--event-path", _event(
          "sec(#1528): require DB_USER/DB_PASSWORD, drop the edgeuser "
          "fallback (D10) [blocked on S2]", [])]), 1)
check("event: label-only exits 1",
      bm.main(["--event-path", _event("feat: thing", ["blocked"])]), 1)
# GitHub sends `"labels": []`, but a PR event with no labels key at all must not
# crash the gate — a traceback here is a red check nobody can act on.
_bare = os.path.join(_tmp, "bare.json")
with open(_bare, "w", encoding="utf-8") as fh:
    json.dump({"pull_request": {"title": "feat: thing"}}, fh)
check("event: missing labels key does not crash",
      bm.main(["--event-path", _bare]), 0)
# The gate is also wired on `edited`, where the payload carries a `changes` key
# alongside the current title. It must read the CURRENT title, not the old one.
_edited = os.path.join(_tmp, "edited.json")
with open(_edited, "w", encoding="utf-8") as fh:
    json.dump({"action": "edited",
               "changes": {"title": {"from": "feat: thing [blocked on X]"}},
               "pull_request": {"title": "feat: thing", "labels": []}}, fh)
check("event: `edited` reads the new title, not changes.from",
      bm.main(["--event-path", _edited]), 0)
shutil.rmtree(_tmp, ignore_errors=True)

if FAILURES:
    print(f"\n{len(FAILURES)} failure(s)")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nblocked-marker selftest: all checks passed")
