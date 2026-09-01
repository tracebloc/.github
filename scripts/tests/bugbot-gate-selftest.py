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
import contextlib
import importlib.util
import io
import json
import os
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


def ev(pr_obj, min_severity):
    """`evaluate`'s verdict, with any exception turned into a reportable value.

    Every positive-path case below goes through here rather than calling
    `evaluate` directly. Without it, a mutation that makes `evaluate` RAISE
    where a PASS was expected takes the whole suite down before it prints
    anything -- and the mutation harness correctly refuses to score a suite that
    never reported as "caught", so a genuinely-detected mutation showed up as
    UNCAUGHT. The bug was in the suite's robustness, not its coverage.
    """
    try:
        return gate.evaluate(pr_obj, min_severity)[0]
    except gate.Unreadable as exc:
        return "REFUSED(%s)" % exc
    except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
        return "CRASH(%s: %s)" % (type(exc).__name__, exc)


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


def thread(body, login="cursor", resolved=False, outdated=False, raised_against=None):
    # `raised_against` is the commit the finding was raised against, as GitHub
    # returns it under `comments.nodes[0].originalCommit.oid`. Left ABSENT by
    # default (not set to None-in-a-dict): the gate reads a missing/None commit
    # as against-this-head and blocks -- fail closed -- and every pre-#2816 case
    # here relies on that, so the default must reproduce "GitHub gave no commit".
    comment = {"author": {"login": login}, "body": body, "url": "u"}
    if raised_against is not None:
        comment["originalCommit"] = {"oid": raised_against}
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"nodes": [comment]},
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


# THE AUTHOR SHAPES ARE LITERALS, written down independently of the module's own
# constants (rule 9's corollary). `BOT_AUTHOR`/`HUMAN_AUTHOR` are imported by
# nothing here: if someone typoes `"Bot"` in the gate, iterating the gate's
# constants would carry the typo into the fixture and stay green, whereas these
# spellings come from GitHub's schema and from the measured payloads
# (`app/tracebloc-release-train` reads back as `__typename: "Bot"`).
AUTHOR_HUMAN_FIXTURE = {"__typename": "User", "login": "LukasWodka"}
AUTHOR_BOT_FIXTURE = {"__typename": "Bot", "login": "tracebloc-release-train"}
AUTHOR_ODD_FIXTURE = {"__typename": "Organization", "login": "tracebloc"}


def pr(contexts=None, threads=None, head=HEAD, ctx_total=None, thread_total=None,
       rollup=True, author=None):
    contexts = [] if contexts is None else contexts
    threads = [] if threads is None else threads
    author = AUTHOR_HUMAN_FIXTURE if author is None else author
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
        "author": author,
        "commits": {"nodes": [{"commit": commit}]},
        "reviewThreads": {
            "totalCount": len(threads) if thread_total is None else thread_total,
            "nodes": threads,
        },
    }


# --------------------------------------------------------------------------
# 1. The load-bearing claim: a terminal Bugbot verdict on THIS head.
# --------------------------------------------------------------------------
v = ev(pr(contexts=[check_run()]), "high")
check("clean head with a terminal Bugbot run passes", v == gate.PASS, "got %r" % v)

# THE FOUR "NOTHING CLAIMED IT" CASES ARE `UNCLAIMED` SINCE backend#2284, and
# what each one asserts is that it is NOT `PASS`. That is the property worth
# pinning: `UNCLAIMED` exits 0 so the context can be required, so a test that
# only checked the exit code would stop distinguishing "clean" from "nobody
# looked". The verdict is the thing that still tells them apart.
v = ev(pr(contexts=[]), "high")
check("no checks at all on the head is UNCLAIMED, not PASS",
      v == gate.UNCLAIMED and v != gate.PASS, "got %r" % v)

v = ev(pr(contexts=[], rollup=False), "high")
check("a null rollup is UNCLAIMED, not PASS",
      v == gate.UNCLAIMED and v != gate.PASS, "got %r" % v)

other = check_run(slug="github-actions", name="Unit tests", conclusion="SUCCESS")
v = ev(pr(contexts=[other]), "high")
check("a head full of OTHER green checks is still UNCLAIMED",
      v == gate.UNCLAIMED and v != gate.PASS, "got %r" % v)

v = ev(pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None)]), "high")
check("a still-running Bugbot is PENDING, not PASS", v == gate.PENDING, "got %r" % v)

# THE SPLIT IS THE WHOLE CHANGE, so it is asserted in both directions. A claimed
# head that never finishes is a review that BROKE and still blocks; a head
# nothing ever claimed is a review that never happened and does not. Collapsing
# them back into one verdict -- in either direction -- reddens here.
check("a CLAIMED but unfinished head is not UNCLAIMED",
      ev(pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None)]), "high")
      != gate.UNCLAIMED, "a running check must not read as never-claimed")
check("an UNCLAIMED head is not PENDING",
      ev(pr(contexts=[]), "high") != gate.PENDING,
      "an absent check must not read as a running one")
check("both absences are waitable, so neither fails early",
      gate.PENDING in gate.WAITABLE and gate.UNCLAIMED in gate.WAITABLE,
      "WAITABLE=%r" % (gate.WAITABLE,))
check("PASS and FAIL are NOT waitable",
      gate.PASS not in gate.WAITABLE and gate.FAIL not in gate.WAITABLE,
      "WAITABLE=%r" % (gate.WAITABLE,))

v = ev(pr(contexts=[check_run(status="QUEUED", conclusion=None)]), "high")
check("a queued Bugbot is PENDING, not PASS", v == gate.PENDING, "got %r" % v)

# The check is matched on the PRODUCING APP, not the display name -- so a
# renamed check must still count. This is the assertion that would redden if
# somebody swapped the app-slug match for a name match.
v = ev(pr(contexts=[check_run(name="Bugbot (renamed upstream)")]), "high")
check("a RENAMED Bugbot check still counts (matched on app slug)", v == gate.PASS, "got %r" % v)

# ... and a same-named check from a DIFFERENT app must not.
v = ev(pr(contexts=[check_run(slug="impostor", name="Cursor Bugbot")]), "high")
check(
    "a check named 'Cursor Bugbot' from another app does NOT satisfy the gate",
    v == gate.UNCLAIMED and v != gate.PASS,
    "got %r" % v,
)

# A draft passes even with nothing reported -- it cannot merge, and the caller
# re-runs the gate on `ready_for_review`. Asserted rather than assumed, because
# it is the file's one deliberate fail-open.
draft = pr(contexts=[])
draft["isDraft"] = True
v = ev(draft, "high")
check("a DRAFT with no Bugbot verdict passes (it cannot merge)", v == gate.PASS, "got %r" % v)

draft_high = pr(contexts=[check_run()], threads=[thread(finding_body("High"))])
draft_high["isDraft"] = True
v = ev(draft_high, "high")
check("a DRAFT with an open High also passes -- the exemption is the draft flag", v == gate.PASS)

# ... and the same PR, no longer a draft, must fail. Without this the draft
# exemption would be indistinguishable from the gate never firing.
not_draft = pr(contexts=[check_run()], threads=[thread(finding_body("High"))])
not_draft["isDraft"] = False
v = ev(not_draft, "high")
check("the SAME PR not marked draft fails -- the exemption is not the whole gate", v == gate.FAIL)

# A SIBLING CHECK FROM THE SAME APP MUST NOT STAND IN FOR THE REVIEW.
# Bugbot raised this on .github#305: Cursor may publish `Cursor Bugbot Autofix`
# under the same app slug, so "the first CheckRun from app cursor" is a guess. It
# has never appeared in this org (measured: 120 runs from that app, all named
# `Cursor Bugbot`), so these cases construct the input rather than observing it.
autofix = check_run(name="Cursor Bugbot Autofix", status="COMPLETED", conclusion="SUCCESS")

# The exact scenario in the finding: autofix DONE, review still running.
v = ev(pr(contexts=[autofix, check_run(status="IN_PROGRESS", conclusion=None)]), "high")
check(
    "a completed Autofix does NOT satisfy the gate while the review is running",
    v == gate.PENDING,
    "got %r" % v,
)
# Order must not matter -- a scan that returned the first match would pass one of
# these two and fail the other, so both are here.
v = ev(pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None), autofix]), "high")
check(
    "...and the same holds with the two checks in the other order",
    v == gate.PENDING,
    "got %r" % v,
)
# With both terminal, the REVIEW is the one that counts: an open High must still
# fail, which it cannot do if Autofix was picked instead.
v = ev(
    pr(contexts=[autofix, check_run()], threads=[thread(finding_body("High"))]),
    "high",
)
check(
    "with both terminal, the review is picked and its findings still gate",
    v == gate.FAIL,
    "got %r" % v,
)
v = ev(pr(contexts=[autofix, check_run()]), "high")
check("a clean review alongside an Autofix run passes", v == gate.PASS, "got %r" % v)
expect_unreadable(
    "two checks from the app and NEITHER named as the review is refused, not guessed",
    lambda: gate.evaluate(
        pr(contexts=[autofix, check_run(name="Cursor Something Else")]), "high"
    ),
    because="cannot be determined",
)
expect_unreadable(
    "two checks BOTH named as the review is also refused",
    lambda: gate.evaluate(pr(contexts=[check_run(), check_run()]), "high"),
    because="cannot be determined",
)

# --------------------------------------------------------------------------
# 2. The conclusion is reported, never used. This is the whole of backend#2284:
#    `neutral` must not fail on its own and `success` must not excuse a finding.
# --------------------------------------------------------------------------
v = ev(pr(contexts=[check_run(conclusion="NEUTRAL")]), "high")
check("conclusion NEUTRAL with no findings PASSES (the verdict is not the gate)", v == gate.PASS)

v = ev(
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
v = ev(pr(contexts=[check_run()], threads=[thread(finding_body("High"))]), "high")
check("an OPEN High fails at threshold high", v == gate.FAIL, "got %r" % v)

v = ev(pr(contexts=[check_run()], threads=[thread(finding_body("Medium"))]), "high")
check("an OPEN Medium passes at threshold high", v == gate.PASS, "got %r" % v)

v = ev(pr(contexts=[check_run()], threads=[thread(finding_body("Medium"))]), "medium")
check("an OPEN Medium fails at threshold medium", v == gate.FAIL, "got %r" % v)

v = ev(
    pr(contexts=[check_run()], threads=[thread(finding_body("High"), resolved=True)]), "high"
)
check("a RESOLVED High passes -- resolve-and-ship is the sanctioned disposition", v == gate.PASS)

v = ev(
    pr(contexts=[check_run()], threads=[thread(finding_body("Critical"))]), "high"
)
check("an OPEN Critical fails at threshold high (rank is ordered, not equality)", v == gate.FAIL)

v = ev(
    pr(contexts=[check_run()], threads=[thread(finding_body("High"), outdated=True)]), "high"
)
check("an OPEN High that is OUTDATED still fails (outdated is not resolved)", v == gate.FAIL)

# THE VOCABULARY, DERIVED FROM THE PRODUCER'S DECLARED SURFACE (rule 6).
for name in gate.SEVERITY_RANK:
    v = ev(
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
        v = ev(
            pr(contexts=[check_run()], threads=[thread(finding_body(name.capitalize()))]),
            higher,
        )
        check(
            "%r does not block at the stricter threshold %r" % (name, higher),
            v == gate.PASS,
            "got %r" % v,
        )

# --------------------------------------------------------------------------
# 3b. HEAD-SCOPED FAST-FAIL (backend#2816). The fast-fail must distinguish
#     WHERE an outstanding finding was raised: against THIS head -> fail fast as
#     before; against an OLDER head while Bugbot is IN_PROGRESS on this one (the
#     post-fix-push race) -> WAIT for the verdict. A narrowing, never a
#     weakening -- a finding against this head still blocks, a never-claimed
#     head still blocks any finding (no laundering), and a stalled Bugbot still
#     blocks (asserted through main, in the exit-code block below).
# --------------------------------------------------------------------------
OLDER = "b" * 40  # a commit that is not HEAD -- an older review head

# THE FIX: an open High raised against an OLDER head, Bugbot IN_PROGRESS on THIS
# head, WAITS instead of fast-failing.
v = ev(pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None)],
          threads=[thread(finding_body("High"), raised_against=OLDER)]), "high")
check("older-head High + IN_PROGRESS waits (PENDING), it does not fast-fail",
      v == gate.PENDING, "got %r" % v)

# ... and a High raised against THIS head still fails fast, even mid-review.
v = ev(pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None)],
          threads=[thread(finding_body("High"), raised_against=HEAD)]), "high")
check("this-head High + IN_PROGRESS still FAILs fast", v == gate.FAIL, "got %r" % v)

# FAIL CLOSED: a finding whose commit could not be read (no originalCommit) is
# treated as against-this-head and blocks, even while Bugbot is running. "Cannot
# tell" must not become permission to wait it out.
v = ev(pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None)],
          threads=[thread(finding_body("High"))]), "high")  # raised_against absent
check("unknown-commit High + IN_PROGRESS FAILs (fail closed)", v == gate.FAIL, "got %r" % v)

# ANTI-LAUNDER, UNCHANGED: an older-head High with NO check on the head still
# fast-fails -- there is no in-progress verdict to wait for, so tolerating it
# would launder a finding across a dropped review (the #356 order bug).
v = ev(pr(contexts=[], threads=[thread(finding_body("High"), raised_against=OLDER)]), "high")
check("older-head High + NEVER-CLAIMED still FAILs (no laundering)", v == gate.FAIL, "got %r" % v)

# A completed review on the head blocks on ANY open blocker, whichever head it
# was raised against -- the reviewed path is head-agnostic and must stay so.
v = ev(pr(contexts=[check_run()], threads=[thread(finding_body("High"), raised_against=OLDER)]), "high")
check("older-head High + COMPLETED review still FAILs (reviewed path)", v == gate.FAIL, "got %r" % v)

# THE SPLIT MUST BE REAL: the same open older-head High yields OPPOSITE verdicts,
# decided solely by whether Bugbot is IN_PROGRESS (wait) or absent (block). A
# mutation that ignores the head classification collapses these two.
v_wait = ev(pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None)],
               threads=[thread(finding_body("High"), raised_against=OLDER)]), "high")
v_block = ev(pr(contexts=[], threads=[thread(finding_body("High"), raised_against=OLDER)]), "high")
check("the head-scope split is real: IN_PROGRESS waits, absent blocks",
      v_wait == gate.PENDING and v_block == gate.FAIL and v_wait != v_block,
      "in_progress=%r absent=%r" % (v_wait, v_block))

# A this-head finding is NOT deferred just because an older-head one shares the
# PR: the this-head blocker dominates the fast-fail mid-review.
v = ev(pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None)],
          threads=[thread(finding_body("High"), raised_against=OLDER),
                   thread(finding_body("High"), raised_against=HEAD)]), "high")
check("a this-head High still fast-fails alongside an older-head one mid-review",
      v == gate.FAIL, "got %r" % v)

# THE QUERY-INTEGRITY GUARD, both directions, inputs written independently of
# the module (rule 9's corollary) and mirroring the author.__typename guard.
check("the live query DOES ask for the finding's originalCommit",
      gate.query_lacks_finding_commit() is False)
check("a query that omits originalCommit is caught",
      gate.query_lacks_finding_commit("{ comments { nodes { body url } } }") is True)
check("a query that asks for originalCommit.oid is not caught",
      gate.query_lacks_finding_commit(
          "{ comments { nodes { originalCommit { oid } } } }") is False)

# --------------------------------------------------------------------------
# 4. What is, and is not, a finding.
# --------------------------------------------------------------------------
v = ev(
    pr(contexts=[check_run()], threads=[thread(finding_body("High"), login="LukasWodka")]), "high"
)
check("a HUMAN thread quoting a severity line is not a Bugbot finding", v == gate.PASS)

v = ev(
    pr(contexts=[check_run()], threads=[thread(finding_body("High", marker=False))]), "high"
)
check("a Bugbot comment WITHOUT the BUGBOT_BUG_ID marker is not a finding", v == gate.PASS)

v = ev(pr(contexts=[check_run()], threads=[{"isResolved": False, "comments": {"nodes": []}}]), "high")
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
# AN EXACTLY-FULL PAGE IS COMPLETE, NOT TRUNCATED. The first version of this
# gate refused it, copying bricked-prs.py's `>= cap` without noticing that file
# has no `totalCount` to compare against (Bugbot, .github#305). Refusing a
# complete page would brick any PR landing on exactly 100 contexts or threads,
# so both directions are pinned here.
full_contexts = [check_run(slug="filler-%d" % i, name="check %d" % i) for i in range(99)]
full_contexts.append(check_run())
v = ev(
    pr(contexts=full_contexts, ctx_total=gate.PAGE_CAP), "high"
)
check(
    "an exactly-full rollup page (totalCount == len(nodes) == cap) is COMPLETE",
    v == gate.PASS,
    "got %r" % v,
)
full_threads = [thread(finding_body("Medium"), resolved=True) for _ in range(gate.PAGE_CAP)]
v = ev(
    pr(contexts=[check_run()], threads=full_threads, thread_total=gate.PAGE_CAP), "high"
)
check(
    "an exactly-full thread page is COMPLETE, not truncated",
    v == gate.PASS,
    "got %r" % v,
)

# ... and truncation is `totalCount > len(nodes)`, which has nothing to do with
# the cap: a short page at ANY size is a cut page.
expect_unreadable(
    "a rollup claiming more contexts than came back is refused",
    lambda: gate.evaluate(pr(contexts=[check_run()], ctx_total=gate.PAGE_CAP + 40), "high"),
    because="the page is truncated",
)
expect_unreadable(
    "a rollup truncated well BELOW the cap is still refused",
    lambda: gate.evaluate(pr(contexts=[check_run()], ctx_total=5), "high"),
    because="the page is truncated",
)
expect_unreadable(
    "a thread page claiming more threads than came back is refused",
    lambda: gate.evaluate(pr(contexts=[check_run()], thread_total=gate.PAGE_CAP), "high"),
    because="the page is truncated",
)
expect_unreadable(
    "a thread page truncated well BELOW the cap is still refused",
    lambda: gate.evaluate(pr(contexts=[check_run()], thread_total=3), "high"),
    because="the page is truncated",
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
    because="did not report totalCount",
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
    because="did not report totalCount",
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
    because="the page is truncated",
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
# 6b. The query must keep asking for totalCount, and PAGE_CAP must be derived.
#     Without totalCount both truncation guards above are inert, so this is the
#     check that keeps them honest -- and it reads the real QUERY, not a copy.
# --------------------------------------------------------------------------
check(
    "the real QUERY asks every paged connection for totalCount",
    gate.connections_missing_totalcount() == [],
    "missing: %r" % (gate.connections_missing_totalcount(),),
)
check(
    "PAGE_CAP is derived from the query's own `first:` size",
    gate.PAGE_CAP == 100,
    "got %r" % gate.PAGE_CAP,
)
# NEVER TEST A LIST AGAINST ITSELF (CLAUDE.md rule 9's corollary). The loop below
# iterates `gate.PAGED_CONNECTIONS`, which is right for completeness -- a member
# added later is exercised the day it is added -- but it is BLIND to a member
# being REMOVED, because the domain it walks is the very thing under test. So the
# two connections this gate depends on are also written down here as literals,
# independently of the module. Dropping either from PAGED_CONNECTIONS now fails.
for name in ("contexts", "reviewThreads"):
    check(
        "%r is declared a guarded paged connection" % name,
        name in gate.PAGED_CONNECTIONS,
        "PAGED_CONNECTIONS = %r" % (gate.PAGED_CONNECTIONS,),
    )

for name in gate.PAGED_CONNECTIONS:
    stripped = gate.QUERY.replace(name + "(first: 100) {\n                totalCount", name + "(first: 100) {")
    stripped = stripped.replace(name + "(first: 100) {\n        totalCount", name + "(first: 100) {")
    check(
        "dropping totalCount from %r is detected" % name,
        name in gate.connections_missing_totalcount(stripped),
        "detector said %r" % (gate.connections_missing_totalcount(stripped),),
    )

check(
    "require_complete returns the nodes when the page is whole",
    gate.require_complete("x", {"totalCount": 2, "nodes": [1, 2]}) == [1, 2],
)
expect_unreadable(
    "require_complete refuses a non-connection",
    lambda: gate.require_complete("x", None),
    because="not a connection object",
)

# --------------------------------------------------------------------------
# 7. severity_of, directly.
# --------------------------------------------------------------------------
check("severity_of lowercases", gate.severity_of("**High Severity**") == "high")
check("severity_of tolerates extra whitespace", gate.severity_of("**High   Severity**") == "high")
check("severity_of returns None with no marker", gate.severity_of("just prose") is None)
check("severity_of returns None on empty input", gate.severity_of("") is None)
check("severity_of returns None on None", gate.severity_of(None) is None)

# --------------------------------------------------------------------------
# --- THE EXIT CODES, WHICH ARE THE ACTUAL BEHAVIOUR CHANGE -------------------
#
# `evaluate` returning UNCLAIMED is only half of backend#2284; what the gate
# DOES with it at the deadline is the half that decides whether a PR merges.
# Asserted through `main` with WAIT_SECONDS=0, so the deadline is already past
# on the first pass and no test sleeps. NOTE `main` takes its budget from the
# ENVIRONMENT, not argv -- passing `--wait-seconds 0` is silently ignored and
# the test then polls for the real 900s. Measured the slow way.
#
# The pairing is the point: same absence-shaped input, opposite exit codes,
# decided solely by whether Bugbot ever claimed the head.
_ENV_KEYS = ("REPO", "PR_NUMBER", "WAIT_SECONDS", "POLL_SECONDS",
             "GITHUB_STEP_SUMMARY")
_env_keep = {k: os.environ.get(k) for k in _ENV_KEYS}
try:
    os.environ["REPO"] = "tracebloc/demo"
    os.environ["PR_NUMBER"] = "1"
    os.environ["WAIT_SECONDS"] = "0"
    os.environ["POLL_SECONDS"] = "0"
    os.environ.pop("GITHUB_STEP_SUMMARY", None)

    def _main_rc(pr_obj):
        gate.fetch = lambda *a, **k: pr_obj
        return gate.main([])

    _real_fetch = gate.fetch
    try:
        rc_unclaimed = _main_rc(pr(contexts=[]))
        check("main: an UNCLAIMED head at the deadline exits 0 (not blocked)",
              rc_unclaimed == 0, "got rc=%r" % rc_unclaimed)

        rc_pending = _main_rc(pr(contexts=[check_run(status="IN_PROGRESS",
                                                     conclusion=None)]))
        check("main: a CLAIMED-but-unfinished head at the deadline exits 1 (blocked)",
              rc_pending == 1, "got rc=%r" % rc_pending)

        # And the two must not have collapsed into the same answer.
        check("main: the two absences produce DIFFERENT exit codes",
              rc_unclaimed != rc_pending,
              "both returned %r -- the split is inert" % rc_unclaimed)

        # THE BANNER IS THE ONLY THING A SKIMMER READS, so it is pinned
        # separately from the exit code. UNCLAIMED exits 0; if its headline
        # also said "pass", the summary would assert cleanliness about a head
        # nothing looked at -- and no other assertion here would notice,
        # because every one of them checks verdicts or exit codes. Measured:
        # the mutation `the UNREVIEWED banner reads as a pass` came back
        # UNCAUGHT until this existed.
        def _banner_for(pr_obj):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _main_rc(pr_obj)
            return buf.getvalue().splitlines()[0] if buf.getvalue() else ""

        _unclaimed_banner = _banner_for(pr(contexts=[]))
        check("main: the UNCLAIMED headline says UNREVIEWED",
              "UNREVIEWED" in _unclaimed_banner, "got %r" % _unclaimed_banner)
        check("main: the UNCLAIMED headline does NOT read as a pass",
              "pass" not in _unclaimed_banner.lower(), "got %r" % _unclaimed_banner)

        _pass_banner = _banner_for(pr(contexts=[check_run()]))
        check("main: a genuine pass still says pass",
              "pass" in _pass_banner.lower() and "UNREVIEWED" not in _pass_banner,
              "got %r" % _pass_banner)

        # A real finding still blocks; tolerance must not have leaked into FAIL.
        rc_fail = _main_rc(pr(contexts=[check_run()],
                              threads=[thread(finding_body("High"), resolved=False)]))
        check("main: an open finding still exits 1", rc_fail == 1, "got rc=%r" % rc_fail)

        # backend#2816 (c): A STALLED Bugbot STILL BLOCKS. Bugbot is IN_PROGRESS
        # on this head with only an OLDER-head finding open; WAIT_SECONDS=0 makes
        # the deadline already past on the first pass, so this is the stall, not
        # the race resolving. Head-scoping defers it to PENDING (rather than the
        # old fast-fail FAIL), and PENDING blocks at the deadline -- same exit
        # code 1, so both `.github#383` stall cases still fail. Without this, the
        # narrowing could have turned a stall into an exit-0 wait.
        rc_stall = _main_rc(
            pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None)],
               threads=[thread(finding_body("High"), resolved=False,
                               raised_against="b" * 40)]))
        check("main: older-head High + STALLED (IN_PROGRESS past budget) exits 1",
              rc_stall == 1, "got rc=%r" % rc_stall)
        # And the race, at the deadline, must not have become a silent pass: a
        # never-claimed head carrying an older-head High still exits 1, never 0.
        rc_older_unclaimed = _main_rc(
            pr(contexts=[], threads=[thread(finding_body("High"), resolved=False,
                                            raised_against="b" * 40)]))
        check("main: older-head High + never-claimed head exits 1, not 0",
              rc_older_unclaimed == 1, "got rc=%r" % rc_older_unclaimed)

        # -------------------------------------------------------------------
        # THE TOLERANCE MUST NOT LAUNDER A FINDING (Bugbot, #356).
        #
        # The dangerous input is the COMBINATION, and neither existing test
        # holds it: "no check on the head" was only ever paired with no
        # threads, and "an open High" only ever with a completed check. So the
        # gate could return on the head question before applying the
        # threshold, and every assertion above would still pass. The reachable
        # sequence is ordinary -- Bugbot reviews head A and files a High, the
        # author pushes head B, Bugbot drops B (which is the measurement this
        # whole change is built on) -- and the answer must be decided by the
        # open finding, not by the missing review.
        rc_unclaimed_open = _main_rc(
            pr(contexts=[], threads=[thread(finding_body("High"), resolved=False)]))
        check("main: UNCLAIMED + an open High exits 1, not 0",
              rc_unclaimed_open == 1, "got rc=%r" % rc_unclaimed_open)
        check("main: the tolerance is what would have been laundered",
              rc_unclaimed == 0 and rc_unclaimed_open == 1,
              "same-shaped absence gave %r clean / %r with an open High"
              % (rc_unclaimed, rc_unclaimed_open))

        _laundered_banner = _banner_for(
            pr(contexts=[], threads=[thread(finding_body("High"), resolved=False)]))
        check("main: that headline does NOT say UNREVIEWED-not-blocked",
              "UNREVIEWED" not in _laundered_banner,
              "got %r" % _laundered_banner)

        # SAME HOLE, OTHER ABSENCE. PENDING already exits 1, so the exit code
        # cannot tell whether the threshold was applied -- the verdict can.
        # Without this, fixing only the UNCLAIMED branch would read as done.
        _v_pending_open = ev(
            pr(contexts=[check_run(status="IN_PROGRESS", conclusion=None)],
               threads=[thread(finding_body("High"), resolved=False)]),
            "high")
        check("evaluate: PENDING + an open High is FAIL, not PENDING",
              _v_pending_open == gate.FAIL, "got %r" % _v_pending_open)

        # A finding BELOW the threshold does not cancel the tolerance: the
        # split backend#2284 measured has to survive its own fix.
        _v_low = ev(pr(contexts=[],
                       threads=[thread(finding_body("Low"), resolved=False)]),
                    "high")
        check("evaluate: UNCLAIMED + an open LOW is still UNCLAIMED",
              _v_low == gate.UNCLAIMED, "got %r" % _v_low)

        # Nor does a RESOLVED one -- otherwise the remedy the FAIL message
        # tells people to use ("reply with the ticket and resolve") would not
        # clear it.
        _v_resolved = ev(pr(contexts=[],
                            threads=[thread(finding_body("High"), resolved=True)]),
                         "high")
        check("evaluate: UNCLAIMED + a RESOLVED High is still UNCLAIMED",
              _v_resolved == gate.UNCLAIMED, "got %r" % _v_resolved)

        # The FAIL has to NAME the finding, not just refuse. One renderer feeds
        # both paths for exactly this reason; asserting it here is what keeps
        # the unclaimed path wired to it.
        _, _laundered_lines = gate.evaluate(
            pr(contexts=[], threads=[thread(finding_body("High"), resolved=False)]),
            "high")
        # The TITLE and the OPEN marker on one row -- not "OPEN appears
        # somewhere", which the prose above would satisfy on its own.
        check("evaluate: the unclaimed FAIL lists the finding as OPEN",
              any("OPEN" in ln and "A real bug" in ln and "high" in ln
                  for ln in _laundered_lines),
              "got %r" % ("\n".join(_laundered_lines)[:400],))
        # -------------------------------------------------------------------
        # THE AUTHOR IS THE DISCRIMINATOR (backend#2586).
        #
        # An unclaimed head means two opposite things, and the tolerance is only
        # defensible for one of them: a Bot-authored PR can never be reviewed
        # (0 of 35 measured) and nothing here can change that, while a
        # human-authored PR going unreviewed is unprecedented (637 of 637 were)
        # and the reader should re-run rather than shrug. The exit code is 0 for
        # both -- that is deliberate and unchanged -- so the REPORT is the whole
        # behaviour, and it is what these assertions hold.
        # -------------------------------------------------------------------
        def _report_for(pr_obj):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _main_rc(pr_obj)
            return buf.getvalue()

        check("author_kind: a User author is human",
              gate.author_kind(pr(author=AUTHOR_HUMAN_FIXTURE)) == gate.AUTHOR_HUMAN,
              "got %r" % gate.author_kind(pr(author=AUTHOR_HUMAN_FIXTURE)))
        check("author_kind: a Bot author is a bot",
              gate.author_kind(pr(author=AUTHOR_BOT_FIXTURE)) == gate.AUTHOR_BOT,
              "got %r" % gate.author_kind(pr(author=AUTHOR_BOT_FIXTURE)))
        # THE TWO MUST NOT COLLAPSE. A discriminator that answers the same thing
        # for both is inert, and every message assertion below would still pass
        # if only one of them were checked in isolation.
        check("author_kind: the two kinds are DIFFERENT answers",
              gate.author_kind(pr(author=AUTHOR_HUMAN_FIXTURE))
              != gate.author_kind(pr(author=AUTHOR_BOT_FIXTURE)),
              "both read as %r" % gate.author_kind(pr(author=AUTHOR_BOT_FIXTURE)))
        # A deleted account returns `author: null`; an actor type this gate has
        # not measured returns something else. Neither may be filed under a
        # branch that then tells the reader something specific and wrong.
        _null_author_pr = pr(contexts=[])
        _null_author_pr["author"] = None  # what GitHub returns for a deleted account
        check("author_kind: a null author is 'cannot tell', not human",
              gate.author_kind(_null_author_pr) is None,
              "got %r" % gate.author_kind(_null_author_pr))
        check("author_kind: an unmeasured actor type is 'cannot tell'",
              gate.author_kind(pr(author=AUTHOR_ODD_FIXTURE)) is None,
              "got %r" % gate.author_kind(pr(author=AUTHOR_ODD_FIXTURE)))
        check("author_kind: a missing author key is 'cannot tell'",
              gate.author_kind({}) is None)

        # The guard on the query, both directions, with the inputs written here
        # rather than taken from the module (rule 9's corollary).
        check("the live query DOES ask for author.__typename",
              gate.query_lacks_author_kind() is False)
        check("a query that asks only for the login is caught",
              gate.query_lacks_author_kind("{ pullRequest { author { login } } }")
              is True)
        check("a query that asks for __typename is not caught",
              gate.query_lacks_author_kind(
                  "{ pullRequest { author { __typename login } } }") is False)

        _bot_report = _report_for(pr(contexts=[], author=AUTHOR_BOT_FIXTURE))
        _human_report = _report_for(pr(contexts=[], author=AUTHOR_HUMAN_FIXTURE))
        _unknown_report = _report_for(pr(contexts=[], author=AUTHOR_ODD_FIXTURE))

        check("main: an UNCLAIMED Bot-authored head names the Bot as the cause",
              "Bot" in _bot_report and "tracebloc-release-train" in _bot_report,
              "got %r" % _bot_report[-500:])
        check("main: it names the remedy (open it as a human), not a re-run",
              "2590" in _bot_report and "Re-running will not help" in _bot_report,
              "got %r" % _bot_report[-500:])
        check("main: an UNCLAIMED human-authored head reads as anomalous",
              "anomalous" in _human_report and "RE-RUN" in _human_report,
              "got %r" % _human_report[-500:])
        # THE CROSS-CHECK IS THE POINT: without it, a report that emitted the
        # bot paragraph for everyone would satisfy every positive assertion
        # above and still tell a human author the false thing the retraction
        # above WAITABLE is about.
        check("main: the human report does NOT claim Bugbot skipped it by design",
              "Re-running will not help" not in _human_report
              and "cannot be fixed from here" not in _human_report,
              "got %r" % _human_report[-500:])
        check("main: the bot report does NOT tell the reader to re-run",
              "anomalous" not in _bot_report, "got %r" % _bot_report[-500:])
        check("main: the three author cases produce THREE distinct reports",
              len({_bot_report, _human_report, _unknown_report}) == 3)
        check("main: an unreadable author kind says so",
              "cannot tell" in _unknown_report,
              "got %r" % _unknown_report[-500:])
        # And the tolerance itself is unchanged by any of this: still exit 0,
        # still UNREVIEWED, for every author kind.
        for _label, _author in (("bot", AUTHOR_BOT_FIXTURE),
                                ("human", AUTHOR_HUMAN_FIXTURE),
                                ("unknown", AUTHOR_ODD_FIXTURE)):
            _rc = _main_rc(pr(contexts=[], author=_author))
            check("main: UNCLAIMED still exits 0 for a %s author" % _label,
                  _rc == 0, "got rc=%r" % _rc)
        check("main: every author kind still reads UNREVIEWED",
              all("UNREVIEWED" in r
                  for r in (_bot_report, _human_report, _unknown_report)))
    finally:
        gate.fetch = _real_fetch
finally:
    for k, v in _env_keep.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


if FAILURES:
    print("bugbot-gate-selftest: %d/%d FAILED" % (len(FAILURES), COUNT))
    for f in FAILURES:
        print("  FAIL: " + f)
    sys.exit(1)
print("bugbot-gate-selftest: %d assertions, all passed" % COUNT)
