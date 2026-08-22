#!/usr/bin/env python3
"""Selftest for the Bugbot review gate (tracebloc/backend#2284).

HERMETIC: every case builds a GraphQL payload by hand and hands it to the real
functions in `scripts/bugbot-gate.py`. No token, no network, no `gh`. The one
seam that would touch the network -- `fetch`'s subprocess call -- is exercised
through an injected runner, so the JSON-decoding and error paths are covered by
the same suite rather than being the part nobody tests.

INPUTS ARE WRITTEN DOWN INDEPENDENTLY OF THE MATCHER (CLAUDE.md rule 9's
corollary). The severity strings, the app slug, the bot login and the finding
marker are spelled out as LITERALS in the fixtures below rather than imported
from the module -- iterating the module's own constants to check the module
would be self-consistent and therefore blind: typo one and the fixture would
carry the typo too and still pass.

THE VOCABULARY IS DERIVED, THOUGH (rule 6). `test_every_declared_severity_...`
walks `SEVERITY_RANK` itself, because a rank order this gate ADDS a member to
later must be exercised on the day it is added -- a hand-listed set of four
cannot see a fifth. Mutation coverage cannot see a vocabulary gap; only
iterating the producer's declared surface can.
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

# NO BYTECODE, and this line is load-bearing twice over.
#
#   1. `exec_module` below would otherwise write `scripts/__pycache__`, and
#      `make selftests-cover` fails on any unmatched file under scripts/ --
#      correctly, since a stray directory is exactly what makes its wildcard
#      assertion pass vacuously.
#   2. More importantly: a pyc is revalidated on the source's
#      (mtime-to-the-second, byte size), and the mutation harness rewrites the
#      gate many times per second with mutations that are frequently the SAME
#      length. A cached .pyc then serves one mutation's bytecode to the next
#      run, so a mutation that IS caught reports as uncaught. That cost real
#      debugging before this line existed, and it is the failure this whole
#      tier is for: an inert mutation and real coverage look identical in a log.
sys.dont_write_bytecode = True

SPEC = importlib.util.spec_from_file_location("bugbot_gate", ROOT / "scripts" / "bugbot-gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

FAILURES = []
COUNT = 0


def check(label, condition, detail=""):
    global COUNT
    COUNT += 1
    if not condition:
        FAILURES.append("%s%s" % (label, (" -- " + detail) if detail else ""))


def expect_unreadable(label, fn, because):
    """`fn` must raise Unreadable, AND for the stated reason.

    `because` is a substring the message must contain, and it is REQUIRED rather
    than optional -- CLAUDE.md rule 10. `Unreadable` is raised from nine places
    in the gate, so a bare "did it raise Unreadable?" is a coin toss that reports
    success: the first draft of this suite passed three mutations that disabled a
    guard in `fetch()`, because with the guard gone the read fell through to a
    DIFFERENT refusal one line down and the type still matched. The test was
    green, the guard was gone, and the log could not tell the difference.
    """
    global COUNT
    COUNT += 1
    try:
        fn()
    except gate.Unreadable as exc:
        if because not in str(exc):
            FAILURES.append(
                "%s -- refused, but for the wrong reason: expected %r in %r"
                % (label, because, str(exc)[:200])
            )
        return
    except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
        FAILURES.append("%s -- raised %s(%s), not Unreadable" % (label, type(exc).__name__, exc))
        return
    FAILURES.append("%s -- returned instead of raising Unreadable" % label)


# --------------------------------------------------------------------------
# Fixture builders. Every literal here is written independently of the module.
# --------------------------------------------------------------------------
HEAD = "a" * 40


def finding_body(severity="High", title="A real bug", marker=True):
    parts = ["### " + title, ""]
    if severity is not None:
        parts += ["**%s Severity**" % severity, ""]
    parts += ["<!-- DESCRIPTION START -->", "words", "<!-- DESCRIPTION END -->", ""]
    if marker:
        parts.append("<!-- BUGBOT_BUG_ID: 9ec8b437-d982-4ffd-b9e9-4ce0466e2730 -->")
    return "\n".join(parts)


def thread(body, login="cursor", resolved=False, outdated=False):
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"nodes": [{"author": {"login": login}, "body": body, "url": "u"}]},
    }


def check_run(slug="cursor", name="Cursor Bugbot", status="COMPLETED", conclusion="NEUTRAL"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": "d",
        "checkSuite": {"app": {"slug": slug}},
    }


def pr(contexts=None, threads=None, head=HEAD, ctx_total=None, thread_total=None, rollup=True):
    contexts = [] if contexts is None else contexts
    threads = [] if threads is None else threads
    commit = {"oid": head}
    commit["statusCheckRollup"] = (
        {
            "contexts": {
                "totalCount": len(contexts) if ctx_total is None else ctx_total,
                "nodes": contexts,
            }
        }
        if rollup
        else None
    )
    return {
        "number": 1,
        "isDraft": False,
        "headRefOid": HEAD,
        "commits": {"nodes": [{"commit": commit}]},
        "reviewThreads": {
            "totalCount": len(threads) if thread_total is None else thread_total,
            "nodes": threads,
        },
    }


# --------------------------------------------------------------------------
# 1. The load-bearing claim: a terminal Bugbot verdict on THIS head.
# --------------------------------------------------------------------------
v, _ = gate.evaluate(pr(contexts=[check_run()]), "high")
check("clean head with a terminal Bugbot run passes", v == gate.PASS, "got %r" % v)

v, _ = gate.evaluate(pr(contexts=[]), "high")
check("no checks at all on the head is PENDING, not PASS", v == gate.PENDING, "got %r" % v)

v, _ = gate.evaluate(pr(contexts=[], rollup=False), "high")
check("a null rollup is PENDING, not PASS", v == gate.PENDING, "got %r" % v)

other = check_run(slug="github-actions", name="Unit tests", conclusion="SUCCESS")
v, _ = gate.evaluate(pr(contexts=[other]), "high")
check("a head full of OTHER green checks is still PENDING", v == gate.PENDING, "got %r" % v)

v, _ = gate.evaluate(pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None)]), "high")
check("a still-running Bugbot is PENDING, not PASS", v == gate.PENDING, "got %r" % v)

v, _ = gate.evaluate(pr(contexts=[check_run(status="QUEUED", conclusion=None)]), "high")
check("a queued Bugbot is PENDING, not PASS", v == gate.PENDING, "got %r" % v)

# The check is matched on the PRODUCING APP, not the display name -- so a
# renamed check must still count. This is the assertion that would redden if
# somebody swapped the app-slug match for a name match.
v, _ = gate.evaluate(pr(contexts=[check_run(name="Bugbot (renamed upstream)")]), "high")
check("a RENAMED Bugbot check still counts (matched on app slug)", v == gate.PASS, "got %r" % v)

# ... and a same-named check from a DIFFERENT app must not.
v, _ = gate.evaluate(pr(contexts=[check_run(slug="impostor", name="Cursor Bugbot")]), "high")
check(
    "a check named 'Cursor Bugbot' from another app does NOT satisfy the gate",
    v == gate.PENDING,
    "got %r" % v,
)

# A draft passes even with nothing reported -- it cannot merge, and the caller
# re-runs the gate on `ready_for_review`. Asserted rather than assumed, because
# it is the file's one deliberate fail-open.
draft = pr(contexts=[])
draft["isDraft"] = True
v, _ = gate.evaluate(draft, "high")
check("a DRAFT with no Bugbot verdict passes (it cannot merge)", v == gate.PASS, "got %r" % v)

draft_high = pr(contexts=[check_run()], threads=[thread(finding_body("High"))])
draft_high["isDraft"] = True
v, _ = gate.evaluate(draft_high, "high")
check("a DRAFT with an open High also passes -- the exemption is the draft flag", v == gate.PASS)

# ... and the same PR, no longer a draft, must fail. Without this the draft
# exemption would be indistinguishable from the gate never firing.
not_draft = pr(contexts=[check_run()], threads=[thread(finding_body("High"))])
not_draft["isDraft"] = False
v, _ = gate.evaluate(not_draft, "high")
check("the SAME PR not marked draft fails -- the exemption is not the whole gate", v == gate.FAIL)

# --------------------------------------------------------------------------
# 2. The conclusion is reported, never used. This is the whole of backend#2284:
#    `neutral` must not fail on its own and `success` must not excuse a finding.
# --------------------------------------------------------------------------
v, _ = gate.evaluate(pr(contexts=[check_run(conclusion="NEUTRAL")]), "high")
check("conclusion NEUTRAL with no findings PASSES (the verdict is not the gate)", v == gate.PASS)

v, _ = gate.evaluate(
    pr(contexts=[check_run(conclusion="SUCCESS")], threads=[thread(finding_body("High"))]),
    "high",
)
check(
    "conclusion SUCCESS does NOT excuse an open High -- derived from threads",
    v == gate.FAIL,
    "got %r" % v,
)

# --------------------------------------------------------------------------
# 3. Severity, and the threshold.
# --------------------------------------------------------------------------
v, _ = gate.evaluate(pr(contexts=[check_run()], threads=[thread(finding_body("High"))]), "high")
check("an OPEN High fails at threshold high", v == gate.FAIL, "got %r" % v)

v, _ = gate.evaluate(pr(contexts=[check_run()], threads=[thread(finding_body("Medium"))]), "high")
check("an OPEN Medium passes at threshold high", v == gate.PASS, "got %r" % v)

v, _ = gate.evaluate(pr(contexts=[check_run()], threads=[thread(finding_body("Medium"))]), "medium")
check("an OPEN Medium fails at threshold medium", v == gate.FAIL, "got %r" % v)

v, _ = gate.evaluate(
    pr(contexts=[check_run()], threads=[thread(finding_body("High"), resolved=True)]), "high"
)
check("a RESOLVED High passes -- resolve-and-ship is the sanctioned disposition", v == gate.PASS)

v, _ = gate.evaluate(
    pr(contexts=[check_run()], threads=[thread(finding_body("Critical"))]), "high"
)
check("an OPEN Critical fails at threshold high (rank is ordered, not equality)", v == gate.FAIL)

v, _ = gate.evaluate(
    pr(contexts=[check_run()], threads=[thread(finding_body("High"), outdated=True)]), "high"
)
check("an OPEN High that is OUTDATED still fails (outdated is not resolved)", v == gate.FAIL)

# THE VOCABULARY, DERIVED FROM THE PRODUCER'S DECLARED SURFACE (rule 6).
for name in gate.SEVERITY_RANK:
    v, _ = gate.evaluate(
        pr(contexts=[check_run()], threads=[thread(finding_body(name.capitalize()))]),
        gate.SEVERITY_RANK[0],
    )
    check(
        "declared severity %r is parsed and blocks at the lowest threshold" % name,
        v == gate.FAIL,
        "got %r" % v,
    )
for i, name in enumerate(gate.SEVERITY_RANK):
    below = gate.SEVERITY_RANK[i + 1 :]
    for higher in below:
        v, _ = gate.evaluate(
            pr(contexts=[check_run()], threads=[thread(finding_body(name.capitalize()))]),
            higher,
        )
        check(
            "%r does not block at the stricter threshold %r" % (name, higher),
            v == gate.PASS,
            "got %r" % v,
        )

# --------------------------------------------------------------------------
# 4. What is, and is not, a finding.
# --------------------------------------------------------------------------
v, _ = gate.evaluate(
    pr(contexts=[check_run()], threads=[thread(finding_body("High"), login="LukasWodka")]), "high"
)
check("a HUMAN thread quoting a severity line is not a Bugbot finding", v == gate.PASS)

v, _ = gate.evaluate(
    pr(contexts=[check_run()], threads=[thread(finding_body("High", marker=False))]), "high"
)
check("a Bugbot comment WITHOUT the BUGBOT_BUG_ID marker is not a finding", v == gate.PASS)

v, _ = gate.evaluate(pr(contexts=[check_run()], threads=[{"isResolved": False, "comments": {"nodes": []}}]), "high")
check("a thread with no comments is skipped, not crashed on", v == gate.PASS)

# --------------------------------------------------------------------------
# 5. Fail closed. "Cannot tell" is a finding, never a pass.
# --------------------------------------------------------------------------
expect_unreadable(
    "an UNRECOGNISED severity token is refused, not ranked harmless",
    lambda: gate.evaluate(
        pr(contexts=[check_run()], threads=[thread(finding_body("Spicy"))]), "high"
    ),
    because="severity this gate does not",
)
expect_unreadable(
    "a finding with NO severity line is refused",
    lambda: gate.evaluate(
        pr(contexts=[check_run()], threads=[thread(finding_body(None))]), "high"
    ),
    because="severity this gate does not",
)
expect_unreadable(
    "a threshold outside the declared rank is refused",
    lambda: gate.evaluate(pr(contexts=[check_run()]), "showstopper"),
    because="is not one of",
)
expect_unreadable(
    "a rollup at exactly the page cap is refused as possibly truncated",
    lambda: gate.evaluate(pr(contexts=[check_run()], ctx_total=gate.PAGE_CAP), "high"),
    because="check contexts",
)
expect_unreadable(
    "a rollup ABOVE the page cap is refused",
    lambda: gate.evaluate(pr(contexts=[check_run()], ctx_total=gate.PAGE_CAP + 40), "high"),
    because="check contexts",
)
expect_unreadable(
    "a thread page at exactly the cap is refused as possibly truncated",
    lambda: gate.evaluate(pr(contexts=[check_run()], thread_total=gate.PAGE_CAP), "high"),
    because="review threads",
)
expect_unreadable(
    "a rollup with no totalCount is refused (truncation cannot be ruled out)",
    lambda: gate.evaluate(
        {
            "headRefOid": HEAD,
            "commits": {"nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": {"contexts": {"nodes": []}}}}]},
            "reviewThreads": {"totalCount": 0, "nodes": []},
        },
        "high",
    ),
    because="rollup had no totalCount",
)
expect_unreadable(
    "a threads block with no totalCount is refused",
    lambda: gate.evaluate(
        {
            "headRefOid": HEAD,
            "commits": {"nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": None}}]},
            "reviewThreads": {"nodes": []},
        },
        "high",
    ),
    because="reviewThreads had no totalCount",
)
expect_unreadable(
    "a PR reporting no commits is refused, not treated as having no findings",
    lambda: gate.evaluate(
        {"headRefOid": HEAD, "commits": {"nodes": []}, "reviewThreads": {"totalCount": 0, "nodes": []}},
        "high",
    ),
    because="reported no commits",
)
expect_unreadable(
    "last-commit != headRefOid is refused as an inconsistent read",
    lambda: gate.evaluate(pr(contexts=[check_run()], head="b" * 40), "high"),
    because="inconsistent read",
)
# A rollup at the cap must be refused EVEN WHEN a Bugbot run is visible in the
# page -- otherwise the truncation guard is dead code on exactly the heads that
# have it, which is the inert-verification shape backend#1729 exists to catch.
expect_unreadable(
    "truncation is refused even when Bugbot IS in the visible page",
    lambda: gate.evaluate(
        pr(contexts=[check_run()], threads=[thread(finding_body("Medium"))], ctx_total=200),
        "high",
    ),
    because="check contexts",
)

# --------------------------------------------------------------------------
# 6. The read seam: fetch()'s failure modes, through an injected runner.
# --------------------------------------------------------------------------
class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def runner_of(proc):
    return lambda args, env: proc


expect_unreadable(
    "a nonzero gh exit is refused",
    lambda: gate.fetch("o", "n", 1, env={}, runner=runner_of(Proc(rc=1, err="boom"))),
    because="GraphQL read failed",
)
expect_unreadable(
    "non-JSON output is refused",
    lambda: gate.fetch("o", "n", 1, env={}, runner=runner_of(Proc(out="<html>rate limited"))),
    because="not JSON",
)
expect_unreadable(
    "a GraphQL errors[] payload is refused even at exit 0",
    lambda: gate.fetch(
        "o", "n", 1, env={},
        runner=runner_of(Proc(out=json.dumps({"data": None, "errors": [{"message": "nope"}]}))),
    ),
    because="GraphQL returned errors",
)
expect_unreadable(
    "a payload with no repository.pullRequest is refused",
    lambda: gate.fetch("o", "n", 1, env={}, runner=runner_of(Proc(out=json.dumps({"data": {}})))),
    because="no repository.pullRequest",
)
expect_unreadable(
    "pullRequest: null is refused, not read as an empty PR",
    lambda: gate.fetch(
        "o", "n", 1, env={},
        runner=runner_of(Proc(out=json.dumps({"data": {"repository": {"pullRequest": None}}}))),
    ),
    because="no such pull request",
)
ok_payload = json.dumps({"data": {"repository": {"pullRequest": pr(contexts=[check_run()])}}})
got = gate.fetch("o", "n", 1, env={}, runner=runner_of(Proc(out=ok_payload)))
check("a well-formed payload is returned", got.get("headRefOid") == HEAD)

# --------------------------------------------------------------------------
# 7. severity_of, directly.
# --------------------------------------------------------------------------
check("severity_of lowercases", gate.severity_of("**High Severity**") == "high")
check("severity_of tolerates extra whitespace", gate.severity_of("**High   Severity**") == "high")
check("severity_of returns None with no marker", gate.severity_of("just prose") is None)
check("severity_of returns None on empty input", gate.severity_of("") is None)
check("severity_of returns None on None", gate.severity_of(None) is None)

# --------------------------------------------------------------------------
if FAILURES:
    print("bugbot-gate-selftest: %d/%d FAILED" % (len(FAILURES), COUNT))
    for f in FAILURES:
        print("  FAIL: " + f)
    sys.exit(1)
print("bugbot-gate-selftest: %d assertions, all passed" % COUNT)
