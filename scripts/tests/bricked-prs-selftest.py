#!/usr/bin/env python3
"""Offline self-test for scripts/bricked-prs.py (tracebloc/backend#1721).

The watcher's whole product is a distinction that is invisible at a glance:

  * a required context that is ABSENT because it will never report  -> bricked
  * a required context that is absent because the run has not STARTED -> not yet
  * a branch or PR list that could not be READ -> unknown, never "clean"

Each of those is asserted here rather than trusted, because getting any of them
wrong makes the watcher either ignorable or actively misleading -- and both were
observed while building it: the first version reported release-train#67 bricked
three minutes after a push, and it had every context a few minutes later.

No network and no token: the module's `gh` entry points are replaced.

Exit 0 when every path behaves as specified.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, os.pardir, "bricked-prs.py")

_spec = importlib.util.spec_from_file_location("bricked_prs", TARGET)
if _spec is None or _spec.loader is None:
    sys.exit(f"cannot import {TARGET}")
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)

# Captured BEFORE any case swaps them out. `install()` replaces these module
# attributes wholesale, so a later case that wants the real implementation must
# put them back -- otherwise it silently tests the previous case's stub, which is
# this file's own subject matter.
REAL_OPEN_PRS = bp.open_prs
REAL_HEAD_AGE = bp.head_age_minutes

RESULTS: "list[tuple[bool, str, str]]" = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        {detail}")


class FakeProtection:
    def __init__(self, checks, error=None):
        self.required_checks = set(checks)
        self.error = error


def install(protection, prs, age=999.0):
    """Point the module at canned answers for one scenario."""
    bp.CD.read_protection = lambda org, name, branch: protection
    bp.open_prs = lambda org, name, base: list(prs)
    bp.head_age_minutes = lambda org, name, sha: age


def pr(number=1, draft=False, contexts=(), state="CLEAN", review="APPROVED"):
    return {
        "number": number, "isDraft": draft, "title": "t", "url": "u",
        "headRefOid": "deadbeef", "mergeStateStatus": state,
        "reviewDecision": review,
        "statusCheckRollup": [{"name": c} for c in contexts],
    }


ROLES = {"develop": "develop"}

# 1. The finding itself.
install(FakeProtection({"gate", "lint"}), [pr(contexts=["lint"])])
f, e = bp.audit_repo("o", "r", ROLES)
record(len(f) == 1 and f[0]["missing"] == ["gate"] and not e,
       "a required context absent from an old head is reported",
       f"findings={[x['missing'] for x in f]} errors={e}")

# 2. THE DISTINCTION THAT MAKES IT READABLE. Same state, young head.
install(FakeProtection({"gate", "lint"}), [pr(contexts=["lint"])], age=5.0)
f, e = bp.audit_repo("o", "r", ROLES)
record(not f and not e,
       "the same PR is NOT reported while its head is young",
       "a queued run and a run that will never happen look identical; only "
       f"time separates them (findings={f})")

# 3. An unreadable age is treated as young. A false brick is what gets the
#    whole report ignored; the next run picks it up anyway.
install(FakeProtection({"gate"}), [pr()], age=None)
f, e = bp.audit_repo("o", "r", ROLES)
record(not f, "an unreadable head age does not produce a finding", f"findings={f}")

# 4. Drafts cannot merge, so a missing context on one is not a brick.
install(FakeProtection({"gate"}), [pr(draft=True)])
f, e = bp.audit_repo("o", "r", ROLES)
record(not f, "a draft PR is not reported", f"findings={f}")

# 5. Present contexts satisfy the requirement -- including a legacy status,
#    which carries `context` rather than `name`.
install(FakeProtection({"legacy"}), [{
    "number": 9, "isDraft": False, "title": "t", "url": "u",
    "headRefOid": "d", "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED",
    "statusCheckRollup": [{"context": "legacy"}],
}])
f, e = bp.audit_repo("o", "r", ROLES)
record(not f, "a legacy status context counts as present",
       "the rollup carries check runs as `name` and statuses as `context`; "
       f"reading only one under-reports every legacy check (findings={f})")

# 6. FAIL CLOSED. An unreadable branch is not a clean branch.
install(FakeProtection(set(), error="classic protection unreadable (502)"), [pr()])
f, e = bp.audit_repo("o", "r", ROLES)
record(not f and len(e) == 1,
       "an unreadable branch is reported as could-not-audit, not as clean",
       f"errors={e}")

# 7. A conflicted PR is a DIFFERENT diagnosis: no merge commit means no
#    pull_request run at all, and the fix is a rebase, not protection.
install(FakeProtection({"gate"}), [pr(state="DIRTY")])
f, e = bp.audit_repo("o", "r", ROLES)
record(len(f) == 1 and f[0]["cause"] == "conflicted",
       "a conflicted PR is labelled as such, not as a protection problem",
       f"cause={[x['cause'] for x in f]}")

install(FakeProtection({"gate"}), [pr(state="BLOCKED")])
f, e = bp.audit_repo("o", "r", ROLES)
record(len(f) == 1 and f[0]["cause"] == "never-reported",
       "a non-conflicted PR keeps the never-reported cause",
       f"cause={[x['cause'] for x in f]}")

# 8. A branch with no required checks cannot brick anything.
install(FakeProtection(set()), [pr()])
f, e = bp.audit_repo("o", "r", ROLES)
record(not f and not e, "a branch requiring nothing produces no findings", f"findings={f}")

# 9. An unreadable PR list is an error, not an empty list.
bp.CD.read_protection = lambda org, name, branch: FakeProtection({"gate"})
def _boom(org, name, base):
    raise bp.CD.GhError(None, "pr list exploded")
bp.open_prs = _boom
f, e = bp.audit_repo("o", "r", ROLES)
record(not f and len(e) == 1 and "PR list unreadable" in e[0],
       "an unreadable PR list is reported, not silently empty", f"errors={e}")

# 10. A CAPPED PR LIST IS NOT AN AUDITED ONE (Bugbot, .github#239). `gh pr list`
#     truncates silently, so a partial view would report "clean" for the PRs it
#     never saw -- the watcher committing the fail-open it exists to find.
bp.open_prs = REAL_OPEN_PRS
_real_gh = bp.CD.gh
bp.CD.gh = lambda args: json.dumps([pr(number=i, contexts=["gate"])
                                    for i in range(bp.PR_LIST_LIMIT)])
try:
    caught = ""
    try:
        bp.open_prs("o", "r", "develop")
    except bp.CD.GhError as exc:
        caught = exc.detail
    record("cap" in caught,
           "a PR list that hits the cap raises rather than returning a partial view",
           f"detail={caught!r}")
finally:
    bp.CD.gh = _real_gh

# 11. THE GRACE WINDOW'S CLOCK. A force-push can put a long-dated commit on a
#     branch a second ago, so the commit timestamp is not "how long CI has had
#     this head". The oldest check suite is.
NOW = datetime.now(timezone.utc)

def _fake_api(payload_by_path):
    def gh_json(args):
        path = args[-1]
        for frag, payload in payload_by_path.items():
            if frag in path:
                return payload
        raise bp.CD.GhError(None, f"unstubbed {path}")
    return gh_json

bp.head_age_minutes = REAL_HEAD_AGE
_real_json = bp.CD.gh_json
try:
    # A suite created 5 minutes ago, on a commit dated last week: young.
    bp.CD.gh_json = _fake_api({
        "check-suites": {"check_suites": [
            {"created_at": (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")}]},
        "commits/": {"commit": {"committer": {
            "date": (NOW - timedelta(days=7)).isoformat().replace("+00:00", "Z")}}},
    })
    age = bp.head_age_minutes("o", "r", "sha")
    record(age is not None and age < 10,
           "a force-pushed old commit is dated by CI's clock, not the commit's",
           f"age={age!r} minutes (commit date was 7 days ago)")

    # No suite at all -- the conflicted case. Only the commit date exists.
    bp.CD.gh_json = _fake_api({
        "check-suites": {"check_suites": []},
        "commits/": {"commit": {"committer": {
            "date": (NOW - timedelta(hours=3)).isoformat().replace("+00:00", "Z")}}},
    })
    age = bp.head_age_minutes("o", "r", "sha")
    record(age is not None and age > 120,
           "with no check suite at all it falls back to the commit date",
           f"age={age!r} minutes — the conflicted case, where no suite will ever exist")

    # An UNREADABLE check-suites read (502/403) is not "no suites": it must NOT
    # fall through to an old commit date and brick falsely. The suites call
    # raises; the commit call, if it were ever reached, is dated last week.
    def _raise_on_suites(args):
        if args[-1].endswith("check-suites"):
            raise bp.CD.GhError(None, "502 reading check-suites")
        return {"commit": {"committer": {
            "date": (NOW - timedelta(days=7)).isoformat().replace("+00:00", "Z")}}}
    bp.CD.gh_json = _raise_on_suites
    age = bp.head_age_minutes("o", "r", "sha")
    record(age is None,
           "an unreadable check-suites read is undateable (None), not the commit date",
           f"age={age!r} — a 502 here must read as young, never brick a 7-day-old commit")
finally:
    bp.CD.gh_json = _real_json

failed = [r for r in RESULTS if not r[0]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
