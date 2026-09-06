#!/usr/bin/env python3
"""Selftest for the closing-ref gate (tracebloc/backend#2364).

HERMETIC: every case hands a payload to the real functions in
`scripts/closing-ref-gate.py`. No token, no network, no `gh`. The one seam that
would touch the network -- `fetch`'s subprocess call -- is exercised through an
injected runner, so the JSON-decoding and error paths are covered by the same
suite rather than being the part nobody tests.

THE FIXTURES ARE MEASURED BYTES, NOT AN ASSUMED SHAPE (the backend#2114 lesson:
a fixture that encoded `login.endswith("[bot]")` while the real API returns
`{"is_bot": true, "login": "app/dependabot"}` let the test pass while the code
could not fire). Every payload below marked MEASURED was captured with
`gh api graphql` on 2026-08-23 and is pasted verbatim, PR number and title
included, so a reader can re-run the same query and diff it.

INPUTS ARE WRITTEN DOWN INDEPENDENTLY OF THE MATCHER (CLAUDE.md rule 9's
corollary). The titles, the repository names and the JSON keys are literals
here, never imported from the module -- checking the module against its own
constants is self-consistent and therefore blind.

THE TYPE VOCABULARY IS DERIVED, THOUGH (rule 6). `org-standards.md` is the
producer that DECLARES the commit-subject prefixes, so this suite parses that
line out of the real file and asserts every declared type parses. A hand-listed
set of six cannot see a seventh added later, and mutation coverage cannot see a
vocabulary gap at all. The derivation itself fails closed: finding no prefixes
in org-standards.md is an assertion failure, not a silently empty loop.

EVERY REFUSAL IS ASSERTED BY ITS OWN MESSAGE (rule 10). `expect_unreadable`
takes the substring the refusal must contain, so a case named for a truncated
page cannot pass on an unrelated exception -- which is how a hardcoded
`schema_version` once let a version refusal stand in for the inconsistency
refusal a test was named for.
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]

# NO BYTECODE. `exec_module` would otherwise write `scripts/__pycache__`, which
# `make selftests-cover` correctly fails on -- and, more importantly, the
# mutation harness rewrites the gate many times per second with mutations that
# are frequently the same length, so a cached .pyc serves one mutation's
# bytecode to the next run and a CAUGHT mutation reports as uncaught.
sys.dont_write_bytecode = True

SPEC = importlib.util.spec_from_file_location("closing_ref_gate", ROOT / "scripts" / "closing-ref-gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

FAILURES = []
COUNT = 0


def check(label, condition, detail=""):
    global COUNT
    COUNT += 1
    if not condition:
        FAILURES.append("%s%s" % (label, (" -- " + detail) if detail else ""))


def value(fn):
    """Call `fn` and return its value, or a description of the exception it raised.

    Every positive assertion goes through this. Without it, a mutation that makes
    a case RAISE where it should return kills the suite mid-run -- and a suite
    that never reports its tally is scored "harness broke", which is
    indistinguishable in a log from the mutation not being covered. Returning the
    exception text instead makes the equality assertion fail, by name.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the point is to report, not to pass
        return "RAISED %s: %s" % (type(exc).__name__, exc)


def ev(payload, **kwargs):
    """`evaluate`, but an escaped exception becomes a reported FAILURE.

    Exactly `value`'s reason, applied to the call every positive case makes.
    `evaluate` grew a path that can raise for a NEW reason -- it derives the
    admissible non-closing forms from org-standards.md and refuses if that
    derivation comes back empty -- so a mutation to the derivation used to kill
    the suite mid-run instead of reddening a case. A suite that never prints its
    tally is scored "harness broke", which is indistinguishable in a log from the
    mutation not being covered, and the mutation harness says so out loud rather
    than counting it as coverage. Returning the exception text as the verdict
    makes the equality assertion fail, by name.
    """
    try:
        return gate.evaluate(payload, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the point is to report, not to pass
        return ("RAISED %s: %s" % (type(exc).__name__, exc), [])


def expect_unreadable(label, fn, because):
    """Assert the SPECIFIC refusal, never a bare catch-all (CLAUDE.md rule 10)."""
    global COUNT
    COUNT += 1
    try:
        fn()
    except gate.Unreadable as exc:
        if because.lower() not in str(exc).lower():
            FAILURES.append(
                "%s -- refused, but for the wrong reason: expected %r, got %r"
                % (label, because, str(exc))
            )
        return
    except Exception as exc:  # noqa: BLE001 - a non-Unreadable escape is a finding
        FAILURES.append("%s -- raised %s instead of Unreadable: %s" % (label, type(exc).__name__, exc))
        return
    FAILURES.append("%s -- did not refuse at all" % label)


# ---------------------------------------------------------------------------
# 0. MEASURED PAYLOADS, verbatim. `gh api graphql` output, 2026-08-23.
# ---------------------------------------------------------------------------

# A cross-repo link: a bare-number scope in client-runtime closing a backend
# ticket. The number 2218 does not exist in client-runtime (its issues are in
# the 300s), which is why a bare number may never be resolved locally.
MEASURED_CROSS_REPO = json.loads(
    '{"data":{"repository":{"pullRequest":{"number":365,'
    '"title":"fix(2218): three age checks compared local wall-clock against UTC",'
    '"closingIssuesReferences":{"totalCount":1,"nodes":['
    '{"number":2218,"repository":{"nameWithOwner":"tracebloc/backend"}}]}}}}}'
)

# A trailing parenthetical naming the repo WITHOUT the owner, cross-repo.
MEASURED_PARENTHETICAL = json.loads(
    '{"data":{"repository":{"pullRequest":{"number":309,'
    '"title":"feat(kanban): a bug-labelled issue lands in Ready, not Backlog (backend#2348)",'
    '"closingIssuesReferences":{"totalCount":1,"nodes":['
    '{"number":2348,"repository":{"nameWithOwner":"tracebloc/backend"}}]}}}}}'
)

# The defect itself: a title that names #2256 and links nothing.
MEASURED_UNLINKED = json.loads(
    '{"data":{"repository":{"pullRequest":{"number":109,'
    '"title":"fix(2256): split the thread count, because one number was two signals",'
    '"closingIssuesReferences":{"totalCount":0,"nodes":[]}}}}}'
)

# The error shape, verbatim, for an absent PR. `gh` also exits nonzero here.
MEASURED_NOT_FOUND = (
    '{"data":{"repository":{"pullRequest":null}},"errors":[{"type":"NOT_FOUND",'
    '"path":["repository","pullRequest"],"locations":[{"line":1,"column":83}],'
    '"message":"Could not resolve to a PullRequest with the number of 999999."}]}'
)


def pr_of(measured):
    return measured["data"]["repository"]["pullRequest"]


def pr(title, links=(), is_draft=False, total=None, body=None,
       base="develop", default="develop"):
    """A payload in the MEASURED shape, with the values a case needs.

    Keys spelled as literals on purpose: they are the contract with GitHub, not
    with the module.

    `base`/`default` default to AGREEING, which is the production case for every
    train repo (measured 2026-08-23: every one defaults to `develop`) and is
    what makes the inert check live for a case that does not mention them.
    `default=None` drops `baseRepository` entirely, which is how a payload that
    cannot answer "is this the default branch?" is spelled.
    """
    nodes = [
        {"number": number, "repository": {"nameWithOwner": full}}
        for full, number in links
    ]
    return {
        "number": 1,
        "title": title,
        "isDraft": is_draft,
        "body": body,
        "baseRefName": base,
        "baseRepository": None if default is None else {"defaultBranchRef": {"name": default}},
        "closingIssuesReferences": {
            "totalCount": len(nodes) if total is None else total,
            "nodes": nodes,
        },
    }


# ---------------------------------------------------------------------------
# 1. parse_title -- the four forms the fleet actually writes.
# ---------------------------------------------------------------------------

def refs_of(title):
    parsed = value(lambda: gate.parse_title(title))
    if isinstance(parsed, str):
        return parsed
    return [(r.owner, r.repo, r.number) for r in parsed]


def source_of(title):
    """WHICH parse path produced the first reference.

    Pinned because two paths can produce the same tuple: `fix(#349):` is both a
    `#N` scope AND a `(#N)` parenthetical, so a narrowing of either regex is
    invisible in the tuple and visible here. Measured on client-runtime#351.
    """
    parsed = value(lambda: gate.parse_title(title))
    if isinstance(parsed, str) or not parsed:
        return parsed or "no refs"
    return parsed[0].source


check(
    "a bare-number scope is a ticket (client-runtime#365, MEASURED)",
    refs_of("fix(2218): three age checks compared local wall-clock against UTC")
    == [(None, None, 2218)],
    "%r" % (refs_of("fix(2218): three age checks compared local wall-clock against UTC"),),
)
check(
    "a `#N` scope is a ticket (client-runtime#351, MEASURED)",
    refs_of("fix(#349): a credential-refresh worker must not outlive its test")
    == [(None, None, 349)],
)
check(
    "a parenthetical naming the repo is a ticket (.github#309, MEASURED)",
    refs_of("feat(kanban): a bug-labelled issue lands in Ready, not Backlog (backend#2348)")
    == [(None, "backend", 2348)],
    "%r" % (refs_of("feat(kanban): a bug-labelled issue lands in Ready, not Backlog (backend#2348)"),),
)
check(
    "a same-repo parenthetical is a ticket (release-train#112, MEASURED)",
    refs_of("fix(hop): the one-hop mutex is a concurrency group, not a read (release-train#110)")
    == [(None, "release-train", 110)],
)
check(
    "the full owner/repo form parses, owner included",
    refs_of("ci(2364): summary (tracebloc/backend#2364)")
    == [(None, None, 2364), ("tracebloc", "backend", 2364)],
    "%r" % (refs_of("ci(2364): summary (tracebloc/backend#2364)"),),
)
check(
    "a `(#N)` parenthetical is a ticket",
    refs_of("fix(installer): a cordoned node must not anchor the envelope (#2237)")
    == [(None, None, 2237)],
)
check(
    "a dotted repo name parses (`.github` starts with a dot)",
    refs_of("fix(inventory): give bugbot-gate a row (.github#312)")
    == [(None, ".github", 312)],
    "%r" % (refs_of("fix(inventory): give bugbot-gate a row (.github#312)"),),
)

# THE NEGATIVE CASE, and it is measured too: backend#2309's real title. `#2271`
# is prose about a PR and the real closing ref is backend#2308, so reading loose
# `#N` would have reddened a compliant PR.
check(
    "a loose `#N` in the summary is NOT read as a ticket (backend#2309, MEASURED)",
    refs_of("test(platform): pin the three early-close call sites #2271 rewrote") == [],
    "%r" % (refs_of("test(platform): pin the three early-close call sites #2271 rewrote"),),
)
check(
    "a word scope names no ticket (backend#2333, MEASURED)",
    refs_of("chore(global_model): delete dead TF FLOPS path") == [],
)
check(
    "a scope like `v2` is not a repo plus a number",
    refs_of("chore(v2): bump something") == [],
    "%r" % (refs_of("chore(v2): bump something"),),
)
check(
    "a title with no conventional-commit prefix at all names nothing",
    refs_of("just some prose about a change") == [],
)
check(
    "a breaking-change marker does not hide the scope",
    refs_of("feat(2364)!: summary") == [(None, None, 2364)],
    "%r" % (refs_of("feat(2364)!: summary"),),
)
check(
    "a comma-separated scope names both tickets",
    refs_of("fix(2364,2365): two at once") == [(None, None, 2364), (None, None, 2365)],
    "%r" % (refs_of("fix(2364,2365): two at once"),),
)
check(
    "the same ticket named twice is one reference",
    refs_of("ci(2364): summary (#2364)") == [(None, None, 2364)],
    "%r" % (refs_of("ci(2364): summary (#2364)"),),
)
check(
    "a `#N` scope is read by the SCOPE path, not only as a parenthetical",
    source_of("fix(#349): a credential-refresh worker must not outlive its test") == "scope",
    "%r" % (source_of("fix(#349): a credential-refresh worker must not outlive its test"),),
)
check(
    "a trailing `(repo#N)` is read by the PARENTHETICAL path",
    source_of("feat(kanban): a bug-labelled issue lands in Ready (backend#2348)") == "parenthetical",
    "%r" % (source_of("feat(kanban): a bug-labelled issue lands in Ready (backend#2348)"),),
)
check(
    "a scope naming repo and number parses",
    refs_of("fix(backend#2364): summary") == [(None, "backend", 2364)],
    "%r" % (refs_of("fix(backend#2364): summary"),),
)
expect_unreadable(
    "an absent title is a cannot-tell, not `names nothing`",
    lambda: gate.parse_title(None),
    because="title is absent or blank",
)
expect_unreadable(
    "a blank title is a cannot-tell",
    lambda: gate.parse_title("   "),
    because="title is absent or blank",
)

# ---------------------------------------------------------------------------
# 2. THE DECLARED VOCABULARY, derived from org-standards.md (rule 6).
#    The producer of the convention is that file; a hand-list here could not
#    see a type added to it later.
# ---------------------------------------------------------------------------
STANDARDS = (ROOT / "org-standards.md").read_text(encoding="utf-8")
DECLARED_TYPES = sorted({m for m in re.findall(r"`((?:[a-z]+/ )+[a-z]+/)`", STANDARDS)})
DECLARED_TYPES = sorted({t.rstrip("/") for group in DECLARED_TYPES for t in group.split()})
check(
    "org-standards.md's declared commit prefixes were found (the derivation is not vacuous)",
    len(DECLARED_TYPES) >= 6,
    "found %r" % (DECLARED_TYPES,),
)
for declared in DECLARED_TYPES:
    check(
        "the declared type %r parses a ticket scope" % declared,
        refs_of("%s(2364): summary" % declared) == [(None, None, 2364)],
        "%r" % (refs_of("%s(2364): summary" % declared),),
    )
# Types the fleet writes that org-standards.md does not declare. Written down
# independently, because the loop above walks a list that could shrink.
for observed in ("test", "refactor", "perf", "build", "style"):
    check(
        "the observed type %r parses too (the type list is not a gate)" % observed,
        refs_of("%s(2364): summary" % observed) == [(None, None, 2364)],
    )

# ---------------------------------------------------------------------------
# 2b. THE NON-CLOSING VOCABULARY, derived from org-standards.md
#     (tracebloc/backend#2616, rule 1). The canon declares the org's partial-work
#     form; the gate parses it rather than holding a copy. These cases pin the
#     DERIVATION, including both fail-closed directions -- an accepted form
#     nothing breaks is an accepted form nobody notices.
# ---------------------------------------------------------------------------

# THE QUERY MUST ACTUALLY ASK FOR THE BODY. Every `evaluate` case below hands the
# body in directly, so a query that stopped requesting `body` would leave all of
# them green while the gate read nothing in production -- inert verification, the
# exact class backend#1729 exists to catch. Asserted against the real query
# string, and mutation-pinned.
check(
    "the GraphQL query requests the PR body (without it the fix cannot fire live)",
    re.search(r"^\s*body\s*$", gate.QUERY, re.MULTILINE) is not None,
    "%r" % (gate.QUERY,),
)

# `value` returns the exception TEXT when the derivation refuses, and a string is
# iterable -- so a case that walks this list would iterate characters and, on the
# space in "RAISED Unreadable: ...", raise IndexError from `"".split()[0]`,
# killing the suite. Normalised to a list once, here, so every case below reports
# instead. (Found by the multi-word-keyword mutation, which is exactly the job:
# it was scored UNCAUGHT for breaking the harness rather than being detected.)
_DERIVED = value(gate.reference_keywords)
DERIVED_KEYWORDS = _DERIVED if isinstance(_DERIVED, list) else []
check(
    "the canon's non-closing form is derived from the real org-standards.md "
    "(the derivation is not vacuous)",
    isinstance(_DERIVED, list) and len(_DERIVED) >= 1,
    "%r" % (_DERIVED,),  # the RAW result, so a refusal shows its own message here
)
# WRITTEN DOWN INDEPENDENTLY OF THE MATCHER (rule 9's corollary): this is the
# string org-standards.md actually carries today, typed here as a literal rather
# than read back out of the module. If the canon drops it, this reddens.
check(
    "`Part of` is what the canon declares today (backend#2616's remedy exists)",
    "Part of" in (DERIVED_KEYWORDS or []),
    "%r" % (DERIVED_KEYWORDS,),
)
check(
    "GitHub's own closing keywords are SUBTRACTED, not offered as a second form",
    not {word.split()[0].lower() for word in (DERIVED_KEYWORDS or [])}
    & {"closes", "fixes", "resolves", "close", "fix", "resolve"},
    "%r" % (DERIVED_KEYWORDS,),
)
# The parse itself, against canon text written here as a literal.
check(
    "a declared `Part of <owner>/<repo>#N` line is read as a non-closing form",
    gate.declared_reference_keywords("say `Part of tracebloc/backend#N` in the body")
    == ["Part of"],
    "%r" % (gate.declared_reference_keywords("say `Part of tracebloc/backend#N` in the body"),),
)
check(
    "a declared `Closes <owner>/<repo>#N` line is NOT read as a non-closing form",
    gate.declared_reference_keywords("the body carries `Closes <owner>/<repo>#N`") == [],
    "%r" % (gate.declared_reference_keywords("the body carries `Closes <owner>/<repo>#N`"),),
)
check(
    "a backticked span with no keyword is not a declaration (`backend#1234`)",
    gate.declared_reference_keywords("referencing the ticket (`backend#1234`)") == [],
)
check(
    "a backticked span with no `#` is not a declaration (`fix/1234-ingest-timeout`)",
    gate.declared_reference_keywords("a slug like `fix/1234-ingest-timeout`") == [],
)
check(
    "a form the canon adds LATER is picked up with no code change (rule 1)",
    gate.declared_reference_keywords(
        "say `Part of tracebloc/backend#N` or `Refs tracebloc/backend#N`"
    ) == ["Part of", "Refs"],
    "%r" % (gate.declared_reference_keywords(
        "say `Part of tracebloc/backend#N` or `Refs tracebloc/backend#N`"),),
)
# FAIL CLOSED, BOTH DIRECTIONS (rule 3). Neither an unreadable canon nor a canon
# that declares nothing may quietly revert the gate to closing-only -- reverting
# IS backend#2616, and it would revert it invisibly.
#
# THE TWO REFUSALS ARE ASSERTED APART, by their own messages (rule 10). They are
# easy to conflate -- a missing directory produces BOTH a missing file and an
# empty vocabulary -- and a case that accepted either could not tell you which
# path it exercised. So the "declares nothing" case gets a canon that really
# exists and really declares nothing.
_TMP_CANON = pathlib.Path(tempfile.mkdtemp(prefix="closing-ref-canon-"))
(_TMP_CANON / gate.STANDARDS_FILE).write_text(
    "# standards\n\nCommit subjects are `type(scope): summary`, referencing the "
    "ticket (`backend#1234`). The body carries `Closes <owner>/<repo>#N`.\n",
    encoding="utf-8",
)
expect_unreadable(
    "a canon that EXISTS but declares no non-closing form is a cannot-tell, "
    "never a silent revert to closing-only",
    lambda: gate.reference_keywords(root=_TMP_CANON),
    because="declares no non-closing reference form",
)
expect_unreadable(
    "an ABSENT canon is a cannot-tell, and says so as a read failure",
    lambda: gate.reference_keywords(root=_TMP_CANON / "no-such-dir"),
    because="could not be read",
)

# ---------------------------------------------------------------------------
# 2c. parse_body -- only a DECLARED keyword counts.
# ---------------------------------------------------------------------------

def body_refs(text, keywords=("Part of",)):
    parsed = value(lambda: gate.parse_body(text, list(keywords)))
    if isinstance(parsed, str):
        return parsed
    return [(r.owner, r.repo, r.number) for r in parsed]


check(
    "the measured body of .github#356 references backend#2284",
    body_refs("Part of tracebloc/backend#2284\n\nIt makes arming the required "
              "context **possible**. It does not make Bugbot reliable.")
    == [("tracebloc", "backend", 2284)],
    "%r" % (body_refs("Part of tracebloc/backend#2284"),),
)
check(
    "the repo-without-owner form parses",
    body_refs("Part of backend#2284") == [(None, "backend", 2284)],
    "%r" % (body_refs("Part of backend#2284"),),
)
check(
    "a bare `Part of #N` parses as repo-agnostic",
    body_refs("Part of #2284") == [(None, None, 2284)],
    "%r" % (body_refs("Part of #2284"),),
)
check(
    "the keyword is case-insensitive, as GitHub's own are",
    body_refs("part of TRACEBLOC/backend#2284") == [("TRACEBLOC", "backend", 2284)],
    "%r" % (body_refs("part of TRACEBLOC/backend#2284"),),
)
check(
    "a dotted repo name parses in a body reference too (`.github`)",
    body_refs("Part of tracebloc/.github#356") == [("tracebloc", ".github", 356)],
    "%r" % (body_refs("Part of tracebloc/.github#356"),),
)
# THE NEGATIVE CASES, and they are the point: an INCIDENTAL mention is not a
# declaration. Reading GitHub's cross-reference graph instead would have accepted
# every one of these, which is why this file parses declared keywords.
check(
    "a loose `#N` in the body is NOT a reference",
    body_refs("This reverts the change #2271 made to the detector.") == [],
    "%r" % (body_refs("This reverts the change #2271 made to the detector."),),
)
check(
    "an undeclared keyword is NOT a reference (`See also`)",
    body_refs("See also tracebloc/backend#2284") == [],
    "%r" % (body_refs("See also tracebloc/backend#2284"),),
)
check(
    "the keyword must stand alone, not end another word (`Apart of`)",
    body_refs("Apart of tracebloc/backend#2284") == [],
    "%r" % (body_refs("Apart of tracebloc/backend#2284"),),
)
check("an absent body references nothing, and is not a malfunction", body_refs(None) == [])
check("an empty body references nothing", body_refs("") == [])
check(
    "the same ticket referenced twice is one reference",
    body_refs("Part of backend#2284 ... Part of backend#2284") == [(None, "backend", 2284)],
    "%r" % (body_refs("Part of backend#2284 ... Part of backend#2284"),),
)
check(
    "two different tickets are two references",
    body_refs("Part of backend#2556 and Part of backend#2616")
    == [(None, "backend", 2556), (None, "backend", 2616)],
    "%r" % (body_refs("Part of backend#2556 and Part of backend#2616"),),
)

# ---------------------------------------------------------------------------
# 3. classify -- the one comparison, called directly.
# ---------------------------------------------------------------------------
Ref = gate.TicketRef

check(
    "a bare number is satisfied by that number in ANOTHER repo (the measured norm)",
    gate.classify(Ref(None, None, 2218, "scope"), [("tracebloc", "backend", 2218)]) == gate.LINKED,
)
check(
    "a bare number is satisfied by that number in the SAME repo",
    gate.classify(Ref(None, None, 90, "scope"), [("tracebloc", "release-train", 90)]) == gate.LINKED,
)
check(
    "a bare number with no link at all is MISSING",
    gate.classify(Ref(None, None, 2256, "scope"), []) == gate.MISSING,
)
check(
    "a repo-named ref linked in that repo is LINKED",
    gate.classify(Ref(None, "backend", 2348, "parenthetical"), [("tracebloc", "backend", 2348)])
    == gate.LINKED,
)
check(
    "repo comparison is case-insensitive",
    gate.classify(Ref(None, "Backend", 2348, "parenthetical"), [("tracebloc", "backend", 2348)])
    == gate.LINKED,
)
check(
    "the full owner/repo form matches on both halves",
    gate.classify(Ref("tracebloc", "backend", 2364, "parenthetical"), [("tracebloc", "backend", 2364)])
    == gate.LINKED,
)
# THE CROSS-REPO TRAP, asserted as its own verdict: the title says backend#304,
# the PR links .github#304 -- which is what a bare `Closes #304` in `.github`
# produces, and it closes the wrong issue on merge.
check(
    "the same number in the WRONG repo is WRONG_REPO, not LINKED",
    gate.classify(Ref(None, "backend", 304, "parenthetical"), [("tracebloc", ".github", 304)])
    == gate.WRONG_REPO,
)
check(
    "the right number in the wrong ORG is WRONG_REPO",
    gate.classify(Ref("someoneelse", "backend", 2364, "parenthetical"), [("tracebloc", "backend", 2364)])
    == gate.WRONG_REPO,
)
check(
    "a different number entirely is MISSING, not WRONG_REPO",
    gate.classify(Ref(None, "backend", 2364, "parenthetical"), [("tracebloc", "backend", 9999)])
    == gate.MISSING,
)
check(
    "one right link among several satisfies the ref",
    gate.classify(
        Ref(None, "backend", 2364, "parenthetical"),
        [("tracebloc", ".github", 304), ("tracebloc", "backend", 2364)],
    )
    == gate.LINKED,
)
check(
    "the three verdicts are distinct values",
    len({gate.LINKED, gate.MISSING, gate.WRONG_REPO}) == 3,
)
check(
    "the four verdicts are distinct values (MENTIONED is its own state)",
    len({gate.LINKED, gate.MENTIONED, gate.MISSING, gate.WRONG_REPO}) == 4,
)

# --- classify against BODY references (tracebloc/backend#2616) --------------
# The measured case: .github#356's title names 2284, it links nothing, and its
# body says `Part of tracebloc/backend#2284`. Truthful, and it must pass.
check(
    "a declared body reference satisfies a bare title number (.github#356, MEASURED)",
    gate.classify(Ref(None, None, 2284, "scope"), [], [Ref("tracebloc", "backend", 2284, "body")])
    == gate.MENTIONED,
)
check(
    "a declared body reference satisfies a repo-named title ref",
    gate.classify(
        Ref(None, "backend", 2284, "parenthetical"), [], [Ref("tracebloc", "backend", 2284, "body")]
    ) == gate.MENTIONED,
)
check(
    "a body reference to a DIFFERENT number does not satisfy the title",
    gate.classify(Ref(None, None, 2284, "scope"), [], [Ref("tracebloc", "backend", 9999, "body")])
    == gate.MISSING,
)
check(
    "a body reference in a DIFFERENT repo does not satisfy a repo-named title ref",
    gate.classify(
        Ref(None, "backend", 304, "parenthetical"), [], [Ref("tracebloc", ".github", 304, "body")]
    ) == gate.MISSING,
)
check(
    "a bare body reference satisfies a repo-named title ref (it constrains no repo)",
    gate.classify(
        Ref(None, "backend", 2284, "parenthetical"), [], [Ref(None, None, 2284, "body")]
    ) == gate.MENTIONED,
)
check(
    "a closing link still outranks a body reference, so the strong form is reported",
    gate.classify(
        Ref(None, "backend", 2364, "parenthetical"),
        [("tracebloc", "backend", 2364)],
        [Ref("tracebloc", "backend", 2364, "body")],
    ) == gate.LINKED,
)
# THE ORDERING, and it is the one that matters. A truthful `Part of
# tracebloc/backend#304` must NOT mask a `Closes #304` that will close
# `.github#304` on merge: the wrong-repo trap is about a link that FIRES.
check(
    "WRONG_REPO is decided BEFORE body references, so a truthful mention cannot "
    "mask a link that closes the wrong issue",
    gate.classify(
        Ref(None, "backend", 304, "parenthetical"),
        [("tracebloc", ".github", 304)],
        [Ref("tracebloc", "backend", 304, "body")],
    ) == gate.WRONG_REPO,
)
check(
    "with no mentions at all, classify behaves exactly as before (default arg)",
    gate.classify(Ref(None, None, 2256, "scope"), []) == gate.MISSING,
)

# ---------------------------------------------------------------------------
# 4. evaluate -- against the MEASURED payloads.
# ---------------------------------------------------------------------------

verdict, lines = ev(pr_of(MEASURED_CROSS_REPO))
check("the measured cross-repo PR passes", verdict == gate.PASS, "%s %r" % (verdict, lines))
verdict, lines = ev(pr_of(MEASURED_PARENTHETICAL))
check("the measured parenthetical PR passes", verdict == gate.PASS, "%s %r" % (verdict, lines))

verdict, lines = ev(pr_of(MEASURED_UNLINKED))
check("the measured unlinked PR FAILS (release-train#109)", verdict == gate.FAIL, "%s" % verdict)
check(
    "the unlinked message says a title reference is inert",
    any("linked nowhere" in line for line in lines),
    "%r" % (lines,),
)
# THESE TWO USED TO PIN THE DEFECT. `MEASURED_UNLINKED` is release-train#109, whose
# title is a BARE `fix(2256)` -- so the gate cannot know the owning repo. The old
# assertion demanded the message contain "Closes tracebloc/", which it satisfied only
# because the remediation defaulted to `ref.repo or "backend"`. It looked correct here
# by luck: backend#2256 IS the right ticket for #109. For release-train#95's `fix(90)`
# the same default advises `tracebloc/backend#90` when the ticket is
# release-train#90 -- and following it links the wrong issue, greens this gate, and
# closes the wrong ticket on merge (Bugbot, .github#314).
#
# So a test asserting the guess is a test asserting the bug. It now asserts the honest
# form instead: name the placeholder, not a repo.
check(
    "the bare-number unlinked message offers a placeholder, never a guessed repo",
    any("<owner>/<repo>#2256" in line for line in lines)
    and not any("Closes tracebloc/backend#2256" in line for line in lines),
    "%r" % (lines,),
)
check(
    "the unlinked message still warns that a bare `Closes #N` resolves locally",
    any("resolves against THIS repo" in line for line in lines),
    "%r" % (lines,),
)

verdict, lines = ev(
    pr("fix(2242): `Done` is a Status an override may name (backend#304)",
       links=[("tracebloc/.github", 304)])
)
check("a wrong-repo link FAILS", verdict == gate.FAIL, "%s" % verdict)
check(
    "the wrong-repo message names the trap and the remedy",
    any("wrong repository" in line and "Closes tracebloc/backend#304" in line for line in lines),
    "%r" % (lines,),
)

# THE REMEDY MUST NOT NAME A REPO THE GATE CANNOT KNOW (Bugbot, .github#314). A bare
# title number is repo-agnostic by decision -- `_classify` accepts a link to ANY repo at
# that number. An earlier remediation defaulted to `ref.repo or "backend"`, so a bare
# `fix(90)` in `release-train` was advised to add `Closes tracebloc/backend#90`. Follow
# that and the gate goes GREEN having linked the wrong issue, which merge then closes:
# a remedy that manufactures the exact defect this gate exists to prevent.
verdict, lines = ev(pr("fix(90): summary"))
check("a bare title number with no link FAILS", verdict == gate.FAIL, "%s" % verdict)
_advice = " ".join(lines)
check(
    "the bare-number remedy does NOT hardcode a repo",
    "tracebloc/backend#90" not in _advice,
    "advice must not name a repo it cannot know: %r" % (lines,),
)
check(
    "the bare-number remedy says the owning repo is unknown and must be supplied",
    "cannot tell" in _advice and "<owner>/<repo>#90" in _advice,
    "%r" % (lines,),
)
# The repo-qualified branch is unaffected: when the title DOES name a repo, the remedy
# still names it. Asserted so the fix above cannot be "achieved" by dropping the repo
# from every message.
_v2, _l2 = ev(pr("fix(2242): summary (backend#304)"))
check(
    "a repo-qualified title still gets a repo-qualified remedy",
    any("Closes tracebloc/backend#304" in line for line in _l2),
    "%r" % (_l2,),
)

# `.github` STARTS WITH A DOT, and the SCOPE pattern has to know that too (Asad,
# .github#314). This was never a wrong ANSWER -- PAREN_REPO_RE matches anywhere in the
# title and a scope is a parenthetical, so a `.github` scope was detected with the right
# repo and number. It was a wrong LABEL (`source='parenthetical'`) resting on a
# coincidence: that PAREN_REPO_RE is not anchored away from the scope position.
# Narrowing it to stop double-matching the scope is a natural change, and it would have
# silently stopped detecting `.github`-scoped tickets -- fail-open on the repo this gate
# lives in. These cases pin `source`, so the two patterns can no longer drift apart
# without a test saying so.
_dot = gate.parse_title("ci(.github#300): arm the stale sweep")
check(
    "a .github scope is read AS A SCOPE, not rescued by the parenthetical pattern",
    len(_dot) == 1 and _dot[0].repo == ".github" and _dot[0].number == 300
    and _dot[0].source == "scope",
    "%r" % (_dot,),
)
_dot_owner = gate.parse_title("ci(tracebloc/.github#300): arm the stale sweep")
check(
    "an owner-qualified .github scope keeps both the owner and source=scope",
    len(_dot_owner) == 1 and _dot_owner[0].owner == "tracebloc"
    and _dot_owner[0].repo == ".github" and _dot_owner[0].source == "scope",
    "%r" % (_dot_owner,),
)
# THE OTHER DIRECTION, so the two rows above cannot be satisfied by a pattern that
# calls everything a scope: a real parenthetical must still report `parenthetical`.
_mixed = gate.parse_title("ci(.github#300): summary (tracebloc/.github#301)")
check(
    "scope and parenthetical are still distinguishable from each other",
    [r.source for r in _mixed] == ["scope", "parenthetical"],
    "%r" % (_mixed,),
)

verdict, lines = ev(pr("chore(global_model): delete dead TF FLOPS path"))
check("a title naming no ticket is NOTHING_NAMED, and passes", verdict == gate.NOTHING_NAMED)
verdict, lines = ev(
    pr("chore(global_model): delete dead TF FLOPS path", links=[("tracebloc/backend", 2288)])
)
check(
    "a link with no title reference is still NOTHING_NAMED (nothing to assert)",
    verdict == gate.NOTHING_NAMED,
)
verdict, lines = ev(pr("fix(2364): unlinked", is_draft=True))
check("a draft is exempt", verdict == gate.DRAFT, "%s" % verdict)
check(
    "the draft message says when the check applies",
    any("marked ready" in line for line in lines),
    "%r" % (lines,),
)
verdict, lines = ev(
    pr("fix(2364): two named, one linked (backend#2365)",
       links=[("tracebloc/backend", 2364)])
)
check("one linked and one not is a FAIL", verdict == gate.FAIL, "%s" % verdict)
check(
    "the report names BOTH tickets the title named",
    any("2364" in line for line in lines) and any("2365" in line for line in lines),
    "%r" % (lines,),
)
expect_unreadable(
    "evaluate refuses a blank title rather than calling it `names nothing`",
    lambda: gate.evaluate(pr("")),
    because="title is absent or blank",
)

# ---------------------------------------------------------------------------
# 5. Truncation and the permission filter -- the direction that must not guess.
# ---------------------------------------------------------------------------
expect_unreadable(
    "a link counted but not returned is a cannot-tell, NOT an absent link",
    lambda: gate.evaluate(pr("fix(2218): summary", links=[], total=1)),
    because="permission filter",
)
check(
    "an exactly-full page is complete, not truncated",
    value(lambda: gate.require_complete("x", {"totalCount": 50, "nodes": list(range(50))}))
    == list(range(50)),
    "%r" % (value(lambda: gate.require_complete("x", {"totalCount": 50, "nodes": list(range(50))})),),
)
check(
    "require_complete returns the nodes when the page is whole",
    value(lambda: gate.require_complete("x", {"totalCount": 2, "nodes": [1, 2]})) == [1, 2],
)
expect_unreadable(
    "require_complete refuses a non-connection",
    lambda: gate.require_complete("x", None),
    because="not a connection object",
)
expect_unreadable(
    "require_complete refuses a connection with no totalCount",
    lambda: gate.require_complete("x", {"nodes": []}),
    because="did not report totalCount",
)
expect_unreadable(
    "a null node in the link graph is a cannot-tell",
    lambda: gate.closing_refs({"closingIssuesReferences": {"totalCount": 1, "nodes": [None]}}),
    because="came back null",
)
expect_unreadable(
    "a node with no owner/name repository is a cannot-tell",
    lambda: gate.closing_refs(
        {"closingIssuesReferences": {"totalCount": 1, "nodes": [{"number": 1, "repository": {"nameWithOwner": "backend"}}]}}
    ),
    because="no owner/name repository",
)
expect_unreadable(
    "a node with a non-integer number is a cannot-tell",
    lambda: gate.closing_refs(
        {"closingIssuesReferences": {"totalCount": 1, "nodes": [{"number": "1", "repository": {"nameWithOwner": "tracebloc/backend"}}]}}
    ),
    because="integer number",
)
check(
    "closing_refs reads the MEASURED node shape",
    value(lambda: gate.closing_refs(pr_of(MEASURED_CROSS_REPO))) == [("tracebloc", "backend", 2218)],
    "%r" % (value(lambda: gate.closing_refs(pr_of(MEASURED_CROSS_REPO))),),
)

# ---------------------------------------------------------------------------
# 5b. The query must keep asking for totalCount, and PAGE_CAP must be derived.
# ---------------------------------------------------------------------------
check(
    "the real QUERY asks every paged connection for totalCount",
    gate.connections_missing_totalcount() == [],
    "missing: %r" % (gate.connections_missing_totalcount(),),
)
check(
    "PAGE_CAP is derived from the query's own `first:` size",
    gate.PAGE_CAP == 50,
    "got %r" % gate.PAGE_CAP,
)
# NEVER TEST A LIST AGAINST ITSELF (rule 9's corollary). The loop below walks the
# module's own tuple, which is right for a member ADDED later but blind to one
# being removed -- so the connection this gate depends on is also written down
# here as a literal.
check(
    "'closingIssuesReferences' is declared a guarded paged connection",
    "closingIssuesReferences" in gate.PAGED_CONNECTIONS,
    "PAGED_CONNECTIONS = %r" % (gate.PAGED_CONNECTIONS,),
)
for name in gate.PAGED_CONNECTIONS:
    stripped = gate.QUERY.replace(
        name + "(first: 50) {\n        totalCount", name + "(first: 50) {"
    )
    check(
        "dropping totalCount from %r is detected" % name,
        name in gate.connections_missing_totalcount(stripped),
        "detector said %r" % (gate.connections_missing_totalcount(stripped),),
    )
check(
    "the query asks for repository.nameWithOwner, the field the API returns",
    "nameWithOwner" in gate.QUERY,
)
check("the query asks whether the PR is a draft", "isDraft" in gate.QUERY)

# ---------------------------------------------------------------------------
# 6. The read seam. Every failure mode of `gh api graphql`, injected.
# ---------------------------------------------------------------------------


class Proc(object):
    def __init__(self, out="", err="", code=0):
        self.stdout = out
        self.stderr = err
        self.returncode = code


def runner_of(proc):
    def run(args, env):
        run.args = args
        return proc

    return run


expect_unreadable(
    "a nonzero gh exit is refused",
    lambda: gate.fetch("tracebloc", "release-train", 1, env={}, runner=runner_of(Proc(err="boom", code=1))),
    because="GraphQL read failed",
)
expect_unreadable(
    "a non-JSON body is refused",
    lambda: gate.fetch("tracebloc", "release-train", 1, env={}, runner=runner_of(Proc(out="<html>"))),
    because="was not JSON",
)
# MEASURED bytes: this is what an absent PR really returns on stdout.
expect_unreadable(
    "the measured NOT_FOUND payload is refused (errors[] at exit 0)",
    lambda: gate.fetch("tracebloc", "release-train", 999999, env={}, runner=runner_of(Proc(out=MEASURED_NOT_FOUND))),
    because="GraphQL returned errors",
)
expect_unreadable(
    "a payload with no repository.pullRequest is refused",
    lambda: gate.fetch("tracebloc", "release-train", 1, env={}, runner=runner_of(Proc(out=json.dumps({"data": {}})))),
    because="no repository.pullRequest",
)
expect_unreadable(
    "pullRequest: null with no errors[] is still refused",
    lambda: gate.fetch(
        "tracebloc", "release-train", 1, env={},
        runner=runner_of(Proc(out=json.dumps({"data": {"repository": {"pullRequest": None}}}))),
    ),
    because="no such pull request",
)
run = runner_of(Proc(out=json.dumps(MEASURED_CROSS_REPO)))
got = gate.fetch("tracebloc", "client-runtime", 365, env={}, runner=run)
check("a well-formed measured payload is returned", got.get("number") == 365, "%r" % (got,))
check(
    "the read asks for the PR the caller named",
    "number=365" in run.args and "name=client-runtime" in run.args,
    "%r" % (run.args,),
)

# ---------------------------------------------------------------------------
# 7. main() -- the exit codes, and the SOFT_FAIL split.
#    SOFT_FAIL governs FINDINGS. It must never cover a malfunction.
# ---------------------------------------------------------------------------


LAST_OUTPUT = {"text": ""}


def run_main(payload=None, raises=None, **env):
    """Run main() with the read seam stubbed. Its stdout is captured, not printed:
    a suite log full of the gate's own reports makes the mutation harness's own
    output unreadable, and the text is asserted below instead of skimmed."""
    saved_env = {k: os.environ.get(k) for k in ("REPO", "PR_NUMBER", "SOFT_FAIL", "GITHUB_STEP_SUMMARY")}
    saved_fetch = gate.fetch
    os.environ.pop("GITHUB_STEP_SUMMARY", None)
    os.environ["REPO"] = env.get("REPO", "tracebloc/release-train")
    os.environ["PR_NUMBER"] = env.get("PR_NUMBER", "109")
    os.environ["SOFT_FAIL"] = env.get("SOFT_FAIL", "false")

    def fake_fetch(owner, name, number, env=None, runner=None):
        if raises is not None:
            raise raises
        return payload

    gate.fetch = fake_fetch
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            return gate.main([])
    except Exception as exc:  # noqa: BLE001 - an escape from main() is a finding, not a crash
        return "RAISED %s: %s" % (type(exc).__name__, exc)
    finally:
        LAST_OUTPUT["text"] = buffer.getvalue()
        gate.fetch = saved_fetch
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


check("main exits 0 on a linked PR", run_main(pr_of(MEASURED_CROSS_REPO)) == 0)
check("main exits 1 on a finding", run_main(pr_of(MEASURED_UNLINKED)) == 1)
check(
    "a finding is annotated at ERROR level when it is not soft",
    "::error::" in LAST_OUTPUT["text"],
    "%r" % (LAST_OUTPUT["text"][-200:],),
)
check(
    "main exits 0 on a finding when SOFT_FAIL is true",
    run_main(pr_of(MEASURED_UNLINKED), SOFT_FAIL="true") == 0,
)
check(
    "a soft finding is annotated at WARNING level, and is still reported",
    "::warning::" in LAST_OUTPUT["text"] and "::error::" not in LAST_OUTPUT["text"],
    "%r" % (LAST_OUTPUT["text"][-200:],),
)
check("main exits 0 when the title names no ticket", run_main(pr("chore(x): prose")) == 0)
check("main exits 0 on a draft", run_main(pr("fix(2364): x", is_draft=True)) == 0)
check(
    "main exits 2 on a cannot-tell",
    run_main(raises=gate.Unreadable("the link graph is unreadable")) == 2,
)
check(
    "SOFT_FAIL does NOT soften a cannot-tell (it governs findings only)",
    run_main(raises=gate.Unreadable("the link graph is unreadable"), SOFT_FAIL="true") == 2,
)
check(
    "a cannot-tell is annotated at ERROR level even under SOFT_FAIL",
    "::error::" in LAST_OUTPUT["text"] and "cannot tell" in LAST_OUTPUT["text"],
    "%r" % (LAST_OUTPUT["text"][:200],),
)
check(
    "SOFT_FAIL does NOT soften an unreadable title",
    run_main(pr(""), SOFT_FAIL="true") == 2,
)
check("main exits 2 when REPO is not owner/name", run_main(pr("x"), REPO="release-train") == 2)
check("main exits 2 when PR_NUMBER is not a number", run_main(pr("x"), PR_NUMBER="abc") == 2)
check("main exits 2 when PR_NUMBER is empty", run_main(pr("x"), PR_NUMBER="") == 2)
check(
    "SOFT_FAIL is only true for the exact string, not any non-empty value",
    run_main(pr_of(MEASURED_UNLINKED), SOFT_FAIL="1") == 1,
)

# THE PREFLIGHT. If the query ever stops asking for totalCount, both the
# truncation test and the permission-filter test above are inert, so main()
# refuses BEFORE reading anything. Driven by stubbing the detector rather than
# the query, because the detector's default argument binds QUERY at import.
saved_detector = gate.connections_missing_totalcount
try:
    gate.connections_missing_totalcount = lambda query=None: ["closingIssuesReferences"]
    check(
        "main exits 2 when the query stopped asking for totalCount",
        run_main(pr_of(MEASURED_CROSS_REPO)) == 2,
    )
    check(
        "the preflight says which connection went blind",
        "closingIssuesReferences" in LAST_OUTPUT["text"] and "totalCount" in LAST_OUTPUT["text"],
        "%r" % (LAST_OUTPUT["text"][:300],),
    )
finally:
    gate.connections_missing_totalcount = saved_detector

# ---------------------------------------------------------------------------
# 8. The workflow that runs this checker must actually run it.
#    A checker nothing invokes is the same dead weight as an unwired selftest.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 7b. END TO END on the measured shape backend#2616 was filed about.
#     .github#356: title `fix(2284): ...`, no closing link, body `Part of
#     tracebloc/backend#2284`. Before the fix its only two remedies were a FALSE
#     `Closes` or deleting `(2284)` from the title.
# ---------------------------------------------------------------------------
CHILD_TITLE = "fix(2284): the gate tolerates a review that never came, and says so"
CHILD_BODY = (
    "Part of tracebloc/backend#2284\n\n"
    "It makes arming the required context **possible**. It does not make Bugbot\n"
    "reliable ... Arming remains a separate, deliberate step.\n"
)

verdict, lines = ev(pr(CHILD_TITLE, body=CHILD_BODY))
check(
    "the child PR that truthfully says `Part of` PASSES (.github#356, backend#2616)",
    verdict == gate.PASS,
    "%s %r" % (verdict, lines),
)
check(
    "and the pass SAYS it was a body reference, not a promise to close",
    any("does not close it" in line for line in lines),
    "%r" % (lines,),
)
# The same title with NO reference of either kind is still the defect the gate
# was built for, and still fails. The fix widened the admissible forms; it did
# not stop the check firing.
verdict, lines = ev(pr(CHILD_TITLE))
check(
    "the same title with NO reference at all still FAILS (the real defect)",
    verdict == gate.FAIL,
    "%s" % verdict,
)
check(
    "and the refusal now offers the truthful non-closing remedy, not only `Closes`",
    any("Part of" in line for line in lines) and any("DOES NOT FINISH" in line for line in lines),
    "%r" % (lines,),
)
# An UNDECLARED keyword must not rescue it -- otherwise any prose would.
verdict, lines = ev(pr(CHILD_TITLE, body="See also tracebloc/backend#2284"))
check(
    "an undeclared keyword in the body does NOT satisfy the title",
    verdict == gate.FAIL,
    "%s %r" % (verdict, lines),
)
# A repo-named title, same shape.
verdict, lines = ev(
    pr("feat(kanban): a bug-labelled issue lands in Ready (backend#2348)",
       body="Part of tracebloc/backend#2348")
)
check(
    "a repo-named title satisfied by a body reference passes",
    verdict == gate.PASS,
    "%s %r" % (verdict, lines),
)
check(
    "the report names the derived keyword it searched the body for",
    any("Part of" in line for line in lines),
    "%r" % (lines,),
)

# ---------------------------------------------------------------------------
HOST = (ROOT / ".github" / "workflows" / "set-pr-status.yml").read_text(encoding="utf-8")
check("the host workflow invokes the checker", "scripts/closing-ref-gate.py" in HOST)
check("the host job passes REPO", re.search(r"REPO:\s*\$\{\{\s*github\.repository\s*\}\}", HOST) is not None)
check("the host job passes PR_NUMBER", "PR_NUMBER: ${{ github.event.pull_request.number }}" in HOST)
check("the host job passes SOFT_FAIL from its own input", "closing-ref-soft-fail" in HOST)
check(
    "the host job mints a SCOPED token (backend#2157)",
    "permission-pull-requests: read" in HOST and "permission-issues: read" in HOST,
)
check(
    "the PR title is never interpolated into a run: block",
    "github.event.pull_request.title" not in HOST,
)

# THE CARD FALLBACK (backend#2731). Asserted here because this file already owns the
# assertions about `set-pr-status.yml`, and nothing covered its status-write block.
#
# SCOPE, said plainly so a green run is not overread: these read the workflow SOURCE.
# They cannot prove the fallback works against the real API -- and this repo's own
# caller pins `@main`, so the PR that lands the fix does not exercise it either. What
# they catch is the regression that matters: deleting the fallback, or softening the
# refusal back to a pass.
#
# EACH ONE PINS THE CALL, NEVER A BARE TOKEN. `"addProjectV2ItemById" in HOST` would
# pass under the mutation that DELETES the call, because the identifier also appears
# in the comment above it explaining that the add is idempotent -- documenting the fix
# keeping the assertion green, the mirror of the backend#2632 trap. A bare
# `"exit 1" in HOST` is satisfied by any of this file's other refusals, so the
# fail-closed check pins the refusal and its exit as one sequence.
check(
    "set-status adds the card itself when it is absent, rather than waiting",
    "addProjectV2ItemById(input: {projectId:" in HOST,
)
check(
    "the add is fed the PR's own node id, not the item id it is creating",
    "pullRequest(number: $num) { id }" in HOST,
)
check(
    "an unresolvable PR node still refuses, rather than writing nothing quietly",
    "could not be resolved to a node id" in HOST,
)
check(
    "a failed add still FAILS CLOSED (backend#2037's guarantee is unchanged)",
    re.search(r'this is not a race\."\s*\n\s*exit 1', HOST) is not None,
)

# ---------------------------------------------------------------------------
# 9. THE GATE MUST RE-RUN WHEN THE FIELDS IT READS CHANGE (backend#2556).
#
#    This job's verdict comes from the PR TITLE and the PR BODY. `edited` is the
#    only event GitHub fires when either changes, so without it the two inputs
#    the gate reads are the two inputs that can change without re-running it --
#    a bypass, not a gap. DERIVED by parsing the caller as YAML rather than
#    grepping for the word: `edited` appears in this file's own prose, and a
#    comment mentioning it is not a trigger (caller-drift.py rule 2, same
#    reason).
#
#    SCOPE, SAID OUT LOUD SO A GREEN RUN IS NOT OVERREAD: this asserts THIS
#    repo's caller only. The other 18 repos hold their own copies of
#    set-pr-status-caller.yml and nothing in this suite can see them -- the
#    fleet-wide assertion would need a network audit, which caller-drift.py
#    does for `uses:` and for no trigger today. Those 18 need the same one-word
#    change, tracked on backend#2556.
# ---------------------------------------------------------------------------
CALLER_PATH = ROOT / ".github" / "workflows" / "set-pr-status-caller.yml"
CALLER_TEXT = CALLER_PATH.read_text(encoding="utf-8")

def _caller_pr_types(text):
    """The caller's real `on.pull_request.types`, parsed, never grepped."""
    try:
        import yaml
    except ImportError:
        return "PyYAML absent"
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        return "caller did not parse as a mapping"
    # YAML 1.1 turns a bare `on:` key into the boolean True. Accept either, and
    # say so rather than silently reading nothing -- an empty list here would
    # make every assertion below vacuously false in the wrong direction.
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict) or "pull_request" not in triggers:
        return "caller declares no pull_request trigger"
    section = triggers["pull_request"]
    if not isinstance(section, dict) or "types" not in section:
        return "caller's pull_request trigger declares no types"
    return section["types"]

CALLER_TYPES = _caller_pr_types(CALLER_TEXT)
check(
    "the caller's pull_request types parse (the derivation is not vacuous)",
    isinstance(CALLER_TYPES, list) and len(CALLER_TYPES) >= 4,
    "%r" % (CALLER_TYPES,),
)
check(
    "the caller listens for `edited`, the only event a title or body change fires "
    "(backend#2556)",
    isinstance(CALLER_TYPES, list) and "edited" in CALLER_TYPES,
    "%r" % (CALLER_TYPES,),
)
# The triggers that were already there must stay: `edited` is an addition, and a
# rewrite that dropped one would be a regression this case names.
for required in ("opened", "reopened", "ready_for_review", "converted_to_draft"):
    check(
        "the caller still listens for `%s`" % required,
        isinstance(CALLER_TYPES, list) and required in CALLER_TYPES,
        "%r" % (CALLER_TYPES,),
    )

# THE CONSEQUENCE OF `edited`, guarded (backend#2556). `edited` fires on a MERGED
# PR too, and `set-status` writes Status unconditionally -- so without this
# condition, fixing a typo in a shipped PR's description drags its card from
# `Prod` back to `Code review`. Every OTHER trigger this workflow has is
# reachable only on an open PR, which is why the guard did not exist before and
# is mandatory now.
check(
    "set-status only writes while the PR is OPEN, so an edit to a merged PR "
    "cannot demote its card (backend#2556)",
    re.search(
        r"^  set-status:\n    if: \$\{\{ github\.event\.pull_request\.state == 'open' \}\}$",
        HOST,
        re.MULTILINE,
    ) is not None,
    "set-status carries no open-state guard",
)
check(
    "closing-ref also only runs while the PR is OPEN, so a merged PR gets no "
    "late red X",
    "inputs.closing-ref-check && github.event.pull_request.state == 'open'" in HOST,
)

# ---------------------------------------------------------------------------
# 9. THE SECOND DIRECTION: a body claim the link graph does not carry
#    (tracebloc/design-system-v2#123).
#
# Everything above this section asserts the FIRST direction, title-to-reference,
# and every one of its 160 assertions passed UNCHANGED when the second direction
# was added -- which is the precise sense in which the old suite could not see
# this defect. The gap was never a wrong answer; it was an unasked question.
# ---------------------------------------------------------------------------

# MEASURED, captured 2026-09-04 with the query this file's module now sends.
# The body is truncated to its first line and that truncation is SAFE rather
# than convenient: the full body is 49 lines / 3062 bytes and
#   grep -nE '^[[:space:]>*+#`~_-]*(close[sd]?|fix(es|ed)?|resolve[sd]?)[[:space:]]*:?[[:space:]]+'
# matches line 1 only, so no dropped line carries a second claim.
#
# This is tracebloc/.github#381, the shipped instance: `Closes #2767` bare-form,
# resolved against `.github` where no #2767 exists, so GitHub registered
# nothing. Its title names no ticket, which is why the gate passed it and
# tracebloc/backend#2767 was closed by hand after the merge.
MEASURED_381 = {
    "number": 381,
    "title": "fix(set-status): add a concurrency group so the newest title/body edit wins",
    "isDraft": False,
    "body": "Closes #2767\n",
    "baseRefName": "develop",
    "baseRepository": {"defaultBranchRef": {"name": "develop"}},
    "closingIssuesReferences": {"totalCount": 0, "nodes": []},
}

check(
    "MEASURED .github#381 FAILS: its body claims a close GitHub never registered, "
    "and its title names no ticket -- the exact PR that shipped green",
    ev(MEASURED_381)[0] == gate.FAIL,
    "%r" % (ev(MEASURED_381)[0],),
)
check(
    "and the finding NAMES the ticket the inert keyword claimed, so the author is "
    "not left hunting for it",
    any("#2767" in line for line in ev(MEASURED_381)[1]),
    "%r" % (ev(MEASURED_381)[1],),
)

# --- the fail-open that must SURVIVE: a PR with no ticket and no claim --------
#
# The whole reason this direction is admissible is that it does NOT require a PR
# to have a ticket. A docs-only or CI-only PR is the case the org actually has,
# and reddening it would be reverting tracebloc/backend#2616 by another route.
check(
    "a docs-only PR with no ticket in the title and no claim in the body still "
    "passes -- this direction fires on a CLAIM, never on the absence of one",
    ev(pr("docs: fix a typo in the README", body="Tidy-up only."))[0] == gate.NOTHING_NAMED,
    "%r" % (ev(pr("docs: fix a typo in the README", body="Tidy-up only."))[0],),
)
check(
    "the measured backend#2333 shape -- no ticket in the title, a REGISTERED "
    "link -- still passes",
    ev(pr("chore(global_model): delete dead TF FLOPS path",
          links=[("tracebloc/backend", 2288)],
          body="Closes tracebloc/backend#2288"))[0] == gate.NOTHING_NAMED,
)

# --- rule A: the graph must be EMPTY ----------------------------------------
#
# Measured false positives, all prose about some OTHER ticket on a PR that
# demonstrably knew how to write a working link: client#885, release-train#134,
# .github#345, .github#360, frontend-app#948.
check(
    "rule A -- a PR that DID register a closing link is not reported for prose "
    "keyword text about another ticket",
    ev(pr("chore: tidy", links=[("tracebloc/backend", 1)],
          body="Closes tracebloc/backend#1\n\nCloses #999 is what the docs example writes."))[0]
    == gate.NOTHING_NAMED,
    "%r" % (ev(pr("chore: tidy", links=[("tracebloc/backend", 1)],
                  body="Closes tracebloc/backend#1\n\nCloses #999 is the docs example."))[0],),
)
check(
    "rule A -- and this check NEVER requires the graph to be non-empty, which is "
    "what keeps it from reverting tracebloc/backend#2616",
    gate.inert_closing_refs("Closes #47", [("tracebloc", "backend", 99)], True) == [],
)

# --- rule C: the base must be the DEFAULT branch -----------------------------
#
# backend#2779 is why: a plain first-line `Closes #2775.`, empty graph, because
# it is stacked onto `fix/2770-explicit-record-count`. GitHub populates
# closingIssuesReferences only for default-branch PRs, so on a stacked PR or a
# train promotion an HONEST claim registers nothing through no fault of anyone.
# Title names NO ticket on purpose, so the FIRST direction cannot fire and this
# case measures rule C alone. The same body on the default branch is the
# must-fail control immediately below -- one input, two bases, opposite verdicts.
STACKED = dict(title="fix(record-count): the stacked one", body="Closes #2775.")
check(
    "rule C -- the measured backend#2779 shape: a stacked PR's honest `Closes` is "
    "not a finding, because GitHub registers no link off the default branch",
    ev(pr(base="fix/2770-explicit-record-count", **STACKED))[0] == gate.NOTHING_NAMED,
    "%r" % (ev(pr(base="fix/2770-explicit-record-count", **STACKED))[0],),
)
check(
    "rule C -- and the SAME body on the default branch does fail, so the rule is "
    "an exemption for stacked PRs and not a hole for everyone",
    ev(pr(base="develop", **STACKED))[0] == gate.FAIL,
    "%r" % (ev(pr(base="develop", **STACKED))[0],),
)
check(
    "rule C -- inert_closing_refs itself declines on a non-default base",
    gate.inert_closing_refs("Closes #2775.", [], False) == [],
)
check(
    "rule C -- and declines when the payload cannot say (None), rather than "
    "manufacturing a finding out of a missing field",
    gate.inert_closing_refs("Closes #2775.", [], None) == [],
)
check(
    "targets_default_branch reports True / False / None as three distinct answers",
    (
        gate.targets_default_branch(pr("t", base="develop", default="develop")),
        gate.targets_default_branch(pr("t", base="staging", default="develop")),
        gate.targets_default_branch(pr("t", default=None)),
    ) == (True, False, None),
    "%r" % ((gate.targets_default_branch(pr("t", base="develop", default="develop")),
             gate.targets_default_branch(pr("t", base="staging", default="develop")),
             gate.targets_default_branch(pr("t", default=None))),),
)

# --- rule B: the claim must OPEN its line ------------------------------------
#
# The rule the 300-PR measurement added. Without it the scan reported on 4 of
# 300 and three were a past participle used adjectivally -- unavoidable while
# GitHub's vocabulary contains `closed`/`fixed`/`resolved`.
for prose, why in (
    ("...and the closed backend#2643 audited that exact layer", "backend#3037"),
    ("`RETRO.md` pointing at the closed backend#1680", "release-train#153"),
    ("STAGE 1 (backend#2979) already closes #2976, so this PR does not",
     "release-train#145 -- the negation TRAILS the keyword, so a backward negation window was the wrong shape; only this rule catches it"),
):
    check(
        "rule B -- mid-line prose is not a claim (%s)" % why,
        gate.inert_closing_refs(prose, [], True) == [],
        "%r" % ([r.number for r in gate.inert_closing_refs(prose, [], True)],),
    )
for lead in ("", "  ", "- ", "* ", "> ", "`", "**", "#### "):
    check(
        "rule B -- markdown noise before the claim is still a claim: %r" % lead,
        [r.number for r in gate.inert_closing_refs(lead + "Closes #47", [], True)] == [47],
        "%r" % ([r.number for r in gate.inert_closing_refs(lead + "Closes #47", [], True)],),
    )

# --- the shapes GitHub does not parse, which is the point of the check --------
for body, why in (
    ("`Fixes #47`", "design-system-v2#119 verbatim: a code span, which GitHub ignores"),
    ("Fixes: #47", "the colon form, which is not a GitHub closing keyword"),
    ("Closes #2767", "bare form resolved against the wrong repo (.github#381)"),
    ("```\nCloses #47\n```", "a fenced block, which GitHub ignores"),
):
    check(
        "an inert keyword IS reported: %s" % why,
        [r.number for r in gate.inert_closing_refs(body, [], True)] != [],
        "%r -- %r" % (body, [r.number for r in gate.inert_closing_refs(body, [], True)]),
    )

# --- the inert finding does not depend on the TITLE, in either direction -----
#
# This is the assertion the ticket is named for. The SAME body must fail whether
# or not the title names a ticket; a check whose verdict moves with the title is
# the defect, not the fix.
INERT_BODY = "Closes #2767"
check(
    "the verdict does not depend on the title: an inert body fails when the title "
    "names NO ticket ...",
    ev(pr("chore: tidy up", body=INERT_BODY))[0] == gate.FAIL,
    "%r" % (ev(pr("chore: tidy up", body=INERT_BODY))[0],),
)
check(
    "... and when the title names one it is STILL a FAIL, folded into the same "
    "verdict rather than masked by it",
    ev(pr("fix(2767): the same body (#2767)", body=INERT_BODY))[0] == gate.FAIL,
    "%r" % (ev(pr("fix(2767): the same body (#2767)", body=INERT_BODY))[0],),
)
check(
    "a title that names a ticket the body LINKS is unaffected by the new "
    "direction -- no claim is inert when the graph carries it",
    ev(pr("fix(47): real (#47)", links=[("tracebloc/design-system-v2", 47)],
          body="Closes tracebloc/design-system-v2#47"))[0] == gate.PASS,
    "%r" % (ev(pr("fix(47): real (#47)", links=[("tracebloc/design-system-v2", 47)],
                  body="Closes tracebloc/design-system-v2#47"))[0],),
)
# THE CASE THAT PINS "FOLDED INTO THE SAME FAIL". A PR whose title IS satisfied
# and which ALSO carries an inert claim must still be red -- otherwise the inert
# finding is computed, printed, and then discarded by the pass that follows,
# which is the exact shape of the defect this ticket reported one level up.
check(
    "a satisfied title does not MASK an inert claim elsewhere in the body: both "
    "are the same defect, a reference the body asserts and the graph does not",
    ev(pr("fix(88): partial (tracebloc/design-system-v2#88)",
          body="Part of tracebloc/design-system-v2#88\n\nCloses #999\n"))[0] == gate.FAIL,
    "%r" % (ev(pr("fix(88): partial (tracebloc/design-system-v2#88)",
                  body="Part of tracebloc/design-system-v2#88\n\nCloses #999\n"))[0],),
)
check(
    "... and the same body WITHOUT the inert line passes, so the case above is "
    "measuring the inert claim and not the title",
    ev(pr("fix(88): partial (tracebloc/design-system-v2#88)",
          body="Part of tracebloc/design-system-v2#88\n"))[0] == gate.PASS,
    "%r" % (ev(pr("fix(88): partial (tracebloc/design-system-v2#88)",
                  body="Part of tracebloc/design-system-v2#88\n"))[0],),
)

check(
    "a child PR with a truthful `Part of` and NO closing keyword still passes -- "
    "backend#2616's shape is untouched",
    ev(pr("fix(2284): partial (tracebloc/backend#2284)",
          body="Part of tracebloc/backend#2284"))[0] == gate.PASS,
    "%r" % (ev(pr("fix(2284): partial (tracebloc/backend#2284)",
                  body="Part of tracebloc/backend#2284"))[0],),
)

# --- readable_text: HTML comments, and ONLY HTML comments --------------------
check(
    "the org's own PR template does not become a claim: `Closes #123` inside its "
    "instruction comment is stripped (.github/pull_request_template.md)",
    gate.inert_closing_refs(
        "<!-- REQUIRED ... Same repo: Closes #123 - Cross-repo: Closes "
        "tracebloc/backend#2364 -->\n\n## What\nA thing.\n", [], True) == [],
    "%r" % ([r.number for r in gate.inert_closing_refs(
        "<!-- Closes #123 -->", [], True)],),
)
check(
    "an HTML-commented `Part of` no longer satisfies a title INVISIBLY -- the "
    "second half of design-system-v2#123",
    ev(pr("fix(47): something (tracebloc/backend#47)",
          body="<!-- Part of tracebloc/backend#47 -->"))[0] == gate.FAIL,
    "%r" % (ev(pr("fix(47): something (tracebloc/backend#47)",
                  body="<!-- Part of tracebloc/backend#47 -->"))[0],),
)
check(
    "a BACKTICKED `Part of` still satisfies a title: design-system-v2#227 and "
    "#228 write it that way, honestly, and stripping code spans reddened both",
    ev(pr("docs(tooltip): correct four statements (#89)",
          body="`Part of tracebloc/design-system-v2#89` - `Refs #34`"))[0] == gate.PASS,
    "%r" % (ev(pr("docs(tooltip): correct four statements (#89)",
                  body="`Part of tracebloc/design-system-v2#89`"))[0],),
)
check(
    "stripping a comment leaves a NON-WHITESPACE placeholder, so a keyword before "
    "it cannot be spliced onto a `#N` after it -- a fail-open introduced by the "
    "code that closes one",
    gate.parse_body(gate.readable_text("Part of\n<!-- x -->\ntracebloc/backend#1"),
                    ["Part of"]) == [],
    "%r" % (gate.parse_body(
        gate.readable_text("Part of\n<!-- x -->\ntracebloc/backend#1"), ["Part of"]),),
)
check(
    "readable_text tolerates an absent body (GitHub returns null for an empty one)",
    gate.readable_text(None) == "",
)
# A COMMENT MAY SPAN LINES -- that is HTML's rule, not a guess about this fleet,
# and it is the shape a wrapped template instruction takes. Without DOTALL the
# comment is left in place and its own example text becomes a claim, so the
# stripping half-works in exactly the direction that manufactures findings.
# (Measured: .github, backend and design-system-v2 all carry a SINGLE-line
# instruction comment with `Closes #123` in it today, so this is the shape one
# reflow away rather than one already present.)
# The keyword opens its own line INSIDE the comment, which is what makes this
# case bite: rule B would reject `Same repo: Closes #123` on prose grounds alone,
# so a fixture written that way passes whether the comment was stripped or not.
MULTILINE_COMMENT = (
    "<!--\nREQUIRED when the title names a ticket. Same repo:\n"
    "Closes #123\n-->\n\n## What\nA thing.\n"
)
check(
    "an HTML comment that spans lines is stripped whole, so a wrapped template "
    "instruction cannot become a claim",
    gate.inert_closing_refs(MULTILINE_COMMENT, [], True) == [],
    "%r" % ([r.number for r in gate.inert_closing_refs(MULTILINE_COMMENT, [], True)],),
)

# --- the read must actually ASK for the fields rule C depends on -------------
#
# Rule C fails OPEN when the payload cannot answer, so a query that silently
# stopped requesting these fields would disarm the whole direction and report
# nothing -- green, quiet, and wrong. Same reason `connections_missing_totalcount`
# exists for the link graph.
for field in ("baseRefName", "defaultBranchRef"):
    check(
        "the GraphQL query requests %s, without which rule C fails open and the "
        "inert check silently disarms" % field,
        field in gate.QUERY,
    )

# --- the annotation must name the direction that failed ----------------------
check(
    "main's annotation no longer claims `the title names a ticket` -- on "
    ".github#381 the title names none, and an annotation pointing at the title "
    "sends the author to the one field that is fine",
    "the title names a ticket this PR does not reference"
    not in pathlib.Path(ROOT / "scripts" / "closing-ref-gate.py").read_text(encoding="utf-8"),
)

# ---------------------------------------------------------------------------
# 9. THE HOUSE SHORTHAND, resolved against the org's declared repo list
#    (tracebloc/.github#416). `engine#898` in a title was read as repo
#    `tracebloc/engine`, so `Part of tracebloc/tracebloc-engine#898` in the body
#    did not satisfy it and the gate went red on tracebloc-engine#900. The map is
#    DERIVED from repo-inventory.yml (rule 1); an unknown short name is a
#    finding, never a guess and never a silent pass.
#
#    THE INPUTS ARE WRITTEN DOWN INDEPENDENTLY OF THE PARSER (rule 9's corollary):
#    the inventory text below is typed here, and the names expected out of it are
#    typed here, so the module is never checked against its own constants.
# ---------------------------------------------------------------------------

_INVENTORY_TEXT = """# a comment before anything
schema_version: 2
org: acme   # trailing comment must not become part of the name
reusables:
  set-pr-status.yml: {}
repos:
  # a comment inside the block
  .dotted:
    visibility: private
    callers:
      set-pr-status.yml: required
  acme-engine:
    visibility: private

  backend:
    protection: {}
shared_reasons:
  not_a_repo: "this block comes AFTER repos and must not be read"
"""

_org, _names = value(lambda: gate.declared_repos(_INVENTORY_TEXT))
check("the inventory's declared `org:` is read, without its trailing comment",
      _org == "acme", "%r" % (_org,))
check("the `repos:` block's 2-space keys are the repo names, in file order; nested "
      "keys, comments, blank lines and the following block are not",
      _names == [".dotted", "acme-engine", "backend"], "%r" % (_names,))
check("an inventory with no `repos:` block declares no repositories",
      value(lambda: gate.declared_repos("org: acme\nreusables: {}\n")) == ("acme", []))
check("an inventory with no `org:` reports None for it",
      value(lambda: gate.declared_repos("repos:\n  backend: {}\n")) == (None, ["backend"]))

# The REAL inventory, as the production path reads it. The literals here are a
# floor typed independently of the file: the header of repo-inventory.yml says
# "ALL 20 repos", and these four are the ones this ticket and its neighbours turn
# on. A rename or removal reddens this by name rather than by count.
_real_org, _real_names = value(lambda: gate.known_repos())
check("the real inventory declares org `tracebloc`", _real_org == "tracebloc", "%r" % (_real_org,))
check("the real inventory is read as the org's fleet (>= 20 repos), not a fragment",
      isinstance(_real_names, list) and len(_real_names) >= 20, "%r" % (_real_names,))
for _name in ("tracebloc-engine", "client-runtime", "backend", ".github"):
    check("the real inventory declares `%s`" % _name, _name in _real_names, "%r" % (_real_names,))
check("no repo named `engine` or `runtime` exists: the shorthand MUST be resolved, "
      "it is not a repo", "engine" not in _real_names and "runtime" not in _real_names)

# Fail closed, by name (rule 3, rule 10).
_TMP_INV = pathlib.Path(tempfile.mkdtemp(prefix="closing-ref-inventory-"))
expect_unreadable("an unreadable inventory is a cannot-tell, not an empty fleet",
                  lambda: gate.known_repos(root=_TMP_INV / "no-such-dir"),
                  "repo-inventory.yml could not be read")
(_TMP_INV / gate.INVENTORY_FILE).write_text("org: acme\nreusables: {}\n", encoding="utf-8")
expect_unreadable("an inventory declaring no repositories refuses rather than calling "
                  "every title's repo unknown",
                  lambda: gate.known_repos(root=_TMP_INV),
                  "declares no repositories")
(_TMP_INV / gate.INVENTORY_FILE).write_text("repos:\n  backend: {}\n", encoding="utf-8")
expect_unreadable("an inventory declaring no `org:` refuses",
                  lambda: gate.known_repos(root=_TMP_INV),
                  "declares no `org:`")

# Resolution, against the real inventory. Inputs: the shorthand the org writes.
def _resolved(owner, repo, number=898):
    out = value(lambda: gate.resolve_repo(gate.TicketRef(owner, repo, number, "scope"),
                                          (_real_org, _real_names)))
    if isinstance(out, str):
        return out
    return (out.owner, out.repo, out.number)

check("`engine` resolves to `tracebloc-engine` because the inventory declares it",
      _resolved(None, "engine") == (None, "tracebloc-engine", 898), "%r" % (_resolved(None, "engine"),))
check("`Engine` resolves too: the shorthand is case-insensitive like GitHub's names",
      _resolved(None, "Engine") == (None, "tracebloc-engine", 898))
check("`tracebloc/engine` (the org spelled out) resolves the same way",
      _resolved("tracebloc", "engine") == ("tracebloc", "tracebloc-engine", 898))
check("`py-package` resolves to `tracebloc-py-package`",
      _resolved(None, "py-package") == (None, "tracebloc-py-package", 898))
check("a declared name is returned in the inventory's own spelling",
      _resolved(None, "BACKEND") == (None, "backend", 898))
check("`runtime` resolves to NOTHING: there is no `tracebloc-runtime`, and guessing "
      "`client-runtime` would be a table nobody declared",
      _resolved(None, "runtime") == (None, "runtime", 898))
check("another org's repo is left alone: the inventory says nothing about it",
      _resolved("otherorg", "engine", 5) == ("otherorg", "engine", 5))
check("a bare number has no repo to resolve",
      _resolved(None, None) == (None, None, 898))

_inv = (_real_org, _real_names)
check("classify: an unresolved short name is UNKNOWN_REPO even when some repo links that number",
      value(lambda: gate.classify(gate.TicketRef(None, "runtime", 123, "scope"),
                                  [("tracebloc", "client-runtime", 123)], (), _inv)) == gate.UNKNOWN_REPO)
check("classify: another org's repo is not unknown; it is simply not linked",
      value(lambda: gate.classify(gate.TicketRef("otherorg", "engine", 5, "scope"), [], (), _inv)) == gate.MISSING)
check("classify: without an inventory the historical verdicts stand",
      value(lambda: gate.classify(gate.TicketRef(None, "runtime", 123, "scope"),
                                  [("tracebloc", "client-runtime", 123)], ())) == gate.WRONG_REPO)

# The measured shape (tracebloc-engine#900, 2026-09-05): shorthand in the title,
# the long form in the body, and NO closing link -- a child PR.
_ENGINE_TITLE = "feat(sharding): every rank reads its own shard (engine#898)"
verdict, lines = ev(pr(_ENGINE_TITLE, body="Part of tracebloc/tracebloc-engine#898"))
check("#416: `engine#898` in the title is satisfied by `Part of tracebloc/tracebloc-engine#898`",
      verdict == gate.PASS, "%s %r" % (verdict, lines))
check("#416: the report names the repo as the inventory spells it",
      any(line.startswith("Title names: tracebloc-engine#898") for line in lines), "%r" % (lines,))
check("#416: the report says what was resolved and from which file",
      any("Shorthand resolved against repo-inventory.yml: engine -> tracebloc-engine" in line
          for line in lines), "%r" % (lines,))
check("#416: it is reported as a declared body reference, not a closing link",
      any("tracebloc-engine#898 -- declared body reference" in line for line in lines), "%r" % (lines,))

verdict, lines = ev(pr(_ENGINE_TITLE, links=[("tracebloc/tracebloc-engine", 898)]))
check("#416: `engine#898` is satisfied by a closing link to tracebloc/tracebloc-engine#898",
      verdict == gate.PASS, "%s %r" % (verdict, lines))
check("#416: ... and reported as the closing link it is",
      any("tracebloc-engine#898 -- closing link" in line for line in lines), "%r" % (lines,))

verdict, lines = ev(pr(_ENGINE_TITLE, body="Part of engine#898"))
check("#416: shorthand in the BODY resolves through the same map, so the two agree",
      verdict == gate.PASS, "%s %r" % (verdict, lines))

verdict, lines = ev(pr(_ENGINE_TITLE, links=[("tracebloc/backend", 898)]))
check("#416: a link to backend#898 does not satisfy the resolved tracebloc-engine#898 (wrong repo)",
      verdict == gate.FAIL and any("wrong repository" in line for line in lines), "%s %r" % (verdict, lines))
check("#416: the wrong-repo remedy spells the RESOLVED repo",
      any("Closes tracebloc/tracebloc-engine#898" in line for line in lines), "%r" % (lines,))

# An unknown short name is a FINDING -- in both directions a silent pass could hide.
verdict, lines = ev(pr("fix(runtime#123): a title naming a repo that does not exist"))
_advice = " ".join(lines)
check("an unknown short repo name FAILS", verdict == gate.FAIL, "%s" % verdict)
check("the finding says the repo is not in the inventory and names the file",
      "not a repository in the org inventory (repo-inventory.yml declares:" in _advice, "%r" % (lines,))
check("the finding lists the declared repos, so the author can pick the real one",
      "client-runtime" in _advice and "tracebloc-engine" in _advice, "%r" % (lines,))
check("the finding names the one resolution it tried",
      "no `tracebloc-runtime` is declared" in _advice, "%r" % (lines,))
check("the finding does NOT advise closing a repo that does not exist",
      "Closes tracebloc/runtime#123" not in _advice, "%r" % (lines,))
verdict, lines = ev(pr("fix(runtime#123): x", links=[("tracebloc/client-runtime", 123)]))
check("an unknown short name is still a finding when some repo links that number -- "
      "the link cannot vouch for a name the org does not have",
      verdict == gate.FAIL and "not a repository in the org inventory" in " ".join(lines),
      "%s %r" % (verdict, lines))

# Another org's repo: untouched by the inventory, and the remedy spells ITS owner.
verdict, lines = ev(pr("fix(otherorg/engine#5): x"))
check("another org's repo is a plain missing link, not an unknown-repo finding",
      verdict == gate.FAIL and "not a repository in the org inventory" not in " ".join(lines),
      "%s %r" % (verdict, lines))
check("the remedy spells the ref's own owner, not `tracebloc/engine#5`",
      any("Closes otherorg/engine#5" in line for line in lines)
      and "tracebloc/engine#5" not in " ".join(lines), "%r" % (lines,))

# evaluate reads the inventory from the same root as the canon, and refuses by name.
_TMP_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="closing-ref-root-"))
(_TMP_ROOT / gate.STANDARDS_FILE).write_text(STANDARDS, encoding="utf-8")
expect_unreadable("evaluate with no inventory beside the canon is a cannot-tell",
                  lambda: gate.evaluate(pr(_ENGINE_TITLE), standards_root=_TMP_ROOT),
                  "repo-inventory.yml could not be read")
(_TMP_ROOT / gate.INVENTORY_FILE).write_text(
    "org: acme\nrepos:\n  acme-engine: {}\n  backend: {}\n", encoding="utf-8")
verdict, lines = ev(pr("fix(engine#7): x", links=[("acme/acme-engine", 7)]), standards_root=_TMP_ROOT)
check("the map follows the inventory it is handed: under org `acme`, `engine` is `acme-engine`",
      verdict == gate.PASS, "%s %r" % (verdict, lines))
verdict, lines = ev(pr("fix(engine#7): x", links=[("tracebloc/tracebloc-engine", 7)]), standards_root=_TMP_ROOT)
check("... and `tracebloc-engine` is then the WRONG repo, not a resolution",
      verdict == gate.FAIL and "wrong repository" in " ".join(lines), "%s %r" % (verdict, lines))

# ---------------------------------------------------------------------------
if FAILURES:
    print("closing-ref-gate-selftest: %d/%d FAILED" % (len(FAILURES), COUNT))
    for f in FAILURES:
        print("  FAIL: " + f)
    sys.exit(1)
print("closing-ref-gate-selftest: %d assertions, all passed" % COUNT)
