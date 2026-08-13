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
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, os.pardir, "bricked-prs.py")

_spec = importlib.util.spec_from_file_location("bricked_prs", TARGET)
if _spec is None or _spec.loader is None:
    sys.exit(f"cannot import {TARGET}")
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)

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

failed = [r for r in RESULTS if not r[0]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
