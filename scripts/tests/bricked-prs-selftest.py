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
import pathlib
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


def pr(number=1, draft=False, contexts=(), state="CLEAN", review="APPROVED",
       author="a-human", is_bot=False, bugbot=True):
    """A NORMAL PR carries a Cursor Bugbot check, so the default fixture has one.

    Otherwise every case written for the required-check logic would also trip the
    bugbot-absent finding, and the two would be impossible to test apart. Absence
    is constructed explicitly with `bugbot=False`, which is the point: it is a
    deviation, not the baseline (backend#2114).
    """
    rollup = [{"name": c} for c in contexts]
    if bugbot:
        rollup.append({"name": "Cursor Bugbot"})
    return {
        "number": number, "isDraft": draft, "title": "t", "url": "u",
        "headRefOid": "deadbeef", "mergeStateStatus": state,
        "reviewDecision": review,
        # THE SHAPE `gh pr list --json author` ACTUALLY RETURNS, measured rather
        # than assumed: `{"is_bot": true, "login": "app/dependabot"}`. The first
        # version of this fixture used `dependabot[bot]`, which is the REST/UI form
        # and never appears here -- so the exemption test passed while the exemption
        # could not fire (Bugbot, .github#282).
        "author": {"login": author, "is_bot": is_bot},
        "statusCheckRollup": rollup,
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
    "author": {"login": "a-human"},
    "statusCheckRollup": [{"context": "legacy"}, {"name": "Cursor Bugbot"}],
}])
f, e = bp.audit_repo("o", "r", ROLES)
record(not f, "a legacy status context counts as present",
       "the rollup carries check runs as `name` and statuses as `context`; "
       f"reading only one under-reports every legacy check (findings={f})")

# A branch with ZERO required checks must STILL surface a missing review
# (saadqbal + Bugbot, #282). The reviewer check does not depend on branch
# protection, and gating it on `required` made the watcher silent on exactly the
# branches with the least protection. Case 8 above did not catch this because the
# default fixture now carries a Bugbot row.
install(FakeProtection(set()), [pr(contexts=[], bugbot=False)])
f, e = bp.audit_repo("o", "r", ROLES)
record(len(f) == 1 and f[0]["cause"] == "bugbot-absent",
       "a branch with NO required checks still reports a missing review",
       f"findings={f}")

# And it still reports nothing when the review IS there — the zero-required branch
# must not become noisy in the other direction.
install(FakeProtection(set()), [pr(contexts=[])])
f, e = bp.audit_repo("o", "r", ROLES)
record(not f, "a zero-required branch with a review present is quiet", f"findings={f}")

# --- the REVIEWER can go missing too (backend#2114) -------------------------
# Measured 2026-08-17: Bugbot's auto-trigger dropped five open PRs across three
# repos while reviewing others opened minutes before and hours after. The PR shows
# a full green check list and nothing distinguishes "found nothing" from "never
# ran" -- it was caught by a human noticing a missing row.

# A PR with every required check present but NO Bugbot is still a finding. This is
# the case that reads as a completely clean PR, which is why it needs reporting.
install(FakeProtection({"gate"}), [pr(contexts=["gate"], bugbot=False)])
f, e = bp.audit_repo("o", "r", ROLES)
record(len(f) == 1 and f[0]["cause"] == "bugbot-absent",
       "a green PR with no Bugbot check IS a finding", f"findings={f}")

# Its own cause, because its remedy -- post `bugbot run` -- is one no other finding
# here would suggest. Conflating it with a missing required check sends someone to
# edit branch protection.
install(FakeProtection({"gate"}), [pr(contexts=[], bugbot=False)])
f, e = bp.audit_repo("o", "r", ROLES)
record(sorted(x["cause"] for x in f) == ["bugbot-absent", "never-reported"],
       "a missing check and a missing REVIEW are reported as separate causes",
       f"causes={[x['cause'] for x in f]}")

# Present is present. A SKIPPED Bugbot ran and decided; that is a verdict, not the
# silent drop this exists to catch.
install(FakeProtection({"gate"}), [pr(contexts=["gate"])])
f, e = bp.audit_repo("o", "r", ROLES)
record(not f, "a PR carrying a Bugbot check is not reported", f"findings={f}")

# Bots are exempt, and that is measured rather than assumed: dependabot[bot] was
# the one legitimately-absent case in the sample. Keyed on GitHub's `[bot]` suffix,
# not a hand-kept list of names that would go stale.
install(FakeProtection({"gate"}), [pr(contexts=["gate"], bugbot=False,
                                      author="app/dependabot", is_bot=True)])
f, e = bp.audit_repo("o", "r", ROLES)
record(not f, "a bot-authored PR with no Bugbot is NOT a finding", f"findings={f}")

# The login form is NOT what exempts, and asserting that is the point: a PR whose
# login merely LOOKS bot-ish but is not flagged `is_bot` must still be reported,
# or the rule drifts back to name-matching.
install(FakeProtection({"gate"}), [pr(contexts=["gate"], bugbot=False,
                                      author="dependabot[bot]", is_bot=False)])
f, e = bp.audit_repo("o", "r", ROLES)
record(len(f) == 1 and f[0]["cause"] == "bugbot-absent",
       "a bot-LOOKING login that is not flagged is_bot IS still reported",
       f"findings={f}")

# And the real shape exempts even when the login carries no bot marker at all.
install(FakeProtection({"gate"}), [pr(contexts=["gate"], bugbot=False,
                                      author="some-app", is_bot=True)])
f, e = bp.audit_repo("o", "r", ROLES)
record(not f, "is_bot alone exempts, whatever the login looks like", f"findings={f}")

# Same young-head rule as the required-check case: a review that has not started
# yet is not a drop.
install(FakeProtection({"gate"}), [pr(contexts=["gate"], bugbot=False)], age=5)
f, e = bp.audit_repo("o", "r", ROLES)
record(not f, "a young head is not reported as a missing review", f"findings={f}")

# The LABEL matters as much as the finding. An unreviewed PR is not bricked -- it
# merges perfectly well -- so rendering it as BRICKED would send the reader to
# branch protection for something one comment fixes. Asserted from the source,
# because the rendering lives in main() and no case above reaches it.
_src = pathlib.Path(bp.__file__).read_text()
record('"bugbot-absent": "UNREVIEWED"' in _src,
       "an absent review renders as UNREVIEWED, not BRICKED",
       "a missing review and a missing check need different labels")
# ASSERT THE RENDERED LINE, not the bare phrase (saadqbal, #282). `bugbot run`
# also appears in this module's comments, so `'bugbot run' in _src` matched whether
# or not the remedy was ever PRINTED -- deleting the print left the suite green.
# My mutation missed it too, because it replaced every occurrence including the
# comments, so it failed for the wrong reason and read as coverage.
record('fix:   comment `bugbot run` on the PR.' in _src,
       "the report PRINTS the remedy (`bugbot run`), which no other cause suggests",
       "a finding whose fix is unstated gets triaged as noise")

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
# --- the age lookup is paid by CANDIDATES ONLY ------------------------------
# saadqbal (#282) wanted one lookup instead of two; Bugbot (#282) wanted none on a
# healthy PR. Both are the same requirement read from opposite ends, and neither is
# observable from a finding count -- so these assert the CALL COUNT directly.
#
# `install()` replaces head_age_minutes wholesale, so the counter wraps it here
# rather than hiding inside the helper: a test that cannot see the call cannot
# assert anything about when it happens.
def counting_age(value):
    box = {"n": 0}
    def f(org, name, sha):
        box["n"] += 1
        return value
    return box, f

# A HEALTHY PR pays nothing. Bugbot present, every required context present.
box, fake = counting_age(999.0)
install(FakeProtection(["build"]), [pr(number=10, contexts=["build"])])
bp.head_age_minutes = fake
findings, errors = bp.audit_repo("o", "r", {"prod": "main"})
record(not findings and not errors and box["n"] == 0,
       "a healthy PR costs ZERO age lookups",
       f"lookups={box['n']} findings={[f['cause'] for f in findings]} — one uncached "
       "call per healthy PR, fleet-wide, on the credential #2036 measured exhausted")

# A PR failing BOTH checks pays exactly one. This is saadqbal's half: it was two.
box, fake = counting_age(999.0)
install(FakeProtection(["build"]), [pr(number=11, contexts=[], bugbot=False)])
bp.head_age_minutes = fake
findings, errors = bp.audit_repo("o", "r", {"prod": "main"})
record(box["n"] == 1 and {f["cause"] for f in findings} == {"bugbot-absent", "never-reported"},
       "a PR missing BOTH a review and a required check pays ONE lookup, not two",
       f"lookups={box['n']} findings={[f['cause'] for f in findings]}")

# An UNDATEABLE candidate is an error, not a silent skip. This is the polarity
# Bugbot flagged: it used to fold into `young` and vanish, so a candidate that
# looks wrong and cannot be judged produced nothing at all.
box, fake = counting_age(None)
install(FakeProtection(["build"]), [pr(number=12, contexts=[], bugbot=False)])
bp.head_age_minutes = fake
findings, errors = bp.audit_repo("o", "r", {"prod": "main"})
record(not findings and len(errors) == 1 and "NOT audited" in errors[0] and "#12" in errors[0],
       "an undateable candidate is an UNKNOWN error, not a silent young-skip",
       f"findings={[f['cause'] for f in findings]} errors={errors}")

# And a genuinely young candidate is still skipped silently -- that one IS correct,
# and without this the fix above could have turned every young PR into an error.
box, fake = counting_age(5.0)
install(FakeProtection(["build"]), [pr(number=13, contexts=[], bugbot=False)])
bp.head_age_minutes = fake
findings, errors = bp.audit_repo("o", "r", {"prod": "main"})
record(not findings and not errors,
       "a genuinely YOUNG candidate is still skipped silently, with no error",
       f"findings={[f['cause'] for f in findings]} errors={errors} — 'has not reported "
       "yet' is not a finding and must not become one")


# --- a rollup at the page size is UNKNOWN, never a finding -----------------
# `gh` asks for `contexts(first: 100)` and does not say when it truncated, so a
# context dropped by pagination looks exactly like a context that never ran. That
# is fail-open in the one direction this whole file exists to close.
#
# The fixture is built so that WITHOUT the guard it yields two findings -- no
# Bugbot and the required check missing -- and both are confident and wrong. So a
# green result here cannot come from the scenario being harmless.
big = pr(number=90, contexts=[f"filler-{i}" for i in range(bp.ROLLUP_CONTEXT_CAP)],
         bugbot=False)
install(FakeProtection(["build"]), [big])
findings, errors = bp.audit_repo("o", "r", {"prod": "main"})
record(not findings and len(errors) == 1,
       "a rollup at the page size produces an ERROR and no findings",
       f"findings={[f['cause'] for f in findings]} errors={errors} — "
       "without the guard this is bugbot-absent + never-reported, both false")
record(errors and "NOT audited" in errors[0] and "#90" in errors[0],
       "the error names the PR and says it was not audited",
       f"errors={errors} — a silent skip is the same fail-open with better manners")

# One under the cap is a NORMAL audit. Without this the guard could be `>= 0` and
# the test above would still pass; this is what makes the boundary mean something.
small = pr(number=91, contexts=[f"filler-{i}" for i in range(bp.ROLLUP_CONTEXT_CAP - 2)],
           bugbot=False)
install(FakeProtection(["build"]), [small])
findings, errors = bp.audit_repo("o", "r", {"prod": "main"})
record(not errors and {f["cause"] for f in findings} == {"bugbot-absent", "never-reported"},
       "one context under the page size is audited normally",
       f"findings={[f['cause'] for f in findings]} errors={errors}")

# A HEALTHY PR AT THE CAP IS NOT A REFUSAL (saadqbal, #282, second round).
# Truncation only ever REMOVES names, so `unreviewed`/`missing` can be falsely
# true and never falsely false: a PR that looks healthy on the partial list would
# look healthy on the full one. Refusing it made a guard fire on the innocent case
# -- and the cost was the whole run, because `main()` returns 2 on any error BEFORE
# `return 1 if findings`, so one big healthy PR demoted every real finding in the
# fleet to a second-class exit code.
#
# The fixture is at the cap AND complete: Bugbot present, `build` present.
healthy_big = pr(number=99,
                 contexts=[f"filler-{i}" for i in range(bp.ROLLUP_CONTEXT_CAP - 2)] + ["build"])
install(FakeProtection(["build"]), [healthy_big])
findings, errors = bp.audit_repo("o", "r", {"prod": "main"})
record(not findings and not errors,
       "a HEALTHY PR at the page size is silent, not an error",
       f"findings={[f['cause'] for f in findings]} errors={errors} — "
       f"rollup is {len(healthy_big['statusCheckRollup'])} contexts, at the cap, "
       "and complete; there is nothing pagination could have hidden")

record(bp.rollup_truncated({"statusCheckRollup": [{"name": "x"}]}) is False
       and bp.rollup_truncated({"statusCheckRollup": []}) is False,
       "a small rollup is not truncated",
       "the polarity, asserted directly rather than only through audit_repo")


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
           f"age={age!r} — a 502 must not let a force-pushed 7-day-old commit read as "
           "old; the CALLER now turns this None into an UNKNOWN error, not a young skip")
finally:
    bp.CD.gh_json = _real_json

failed = [r for r in RESULTS if not r[0]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
