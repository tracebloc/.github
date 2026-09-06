#!/usr/bin/env python3
"""A PR whose TITLE names a ticket must LINK it (tracebloc/backend#2364).

WHAT WAS MEASURED, 2026-08-23, before any code was written.

  1. `closingIssuesReferences` was 0 on 7 of 7 epic-relevant merged PRs:
     release-train#109, release-train#108, .github#304, .github#300,
     backend#2266, client#774, docs#131. Re-read on release-train#109 while
     writing this file: `{"totalCount":0,"nodes":[]}` on a PR titled
     `fix(2256): split the thread count, ...`.

  2. The mechanism. The house convention puts the ticket in the PR TITLE
     (`type(scope): summary`, org-standards.md). GitHub creates a closing link
     ONLY from a keyword in the PR BODY. A title reference is inert -- a
     human-readable label and nothing more. So `kanban-closure-router.yml`
     fires, finds no linked issue, and correctly does nothing: both kanban
     workflows on release-train#108 reported success, and they were right to.

     The chain, stated precisely, because a near-miss version of it is easy to
     write: the link makes GitHub CLOSE the issue on merge (the fleet's default
     branch is `develop`, measured 2026-08-23 across backend / client / .github /
     frontend-app / docs, so a closing keyword fires on the branch PRs actually
     land on); that close is the event `kanban-closure-router.yml` routes on,
     mirroring the merged PR's base to the card. This gate asserts the FIRST
     link in that chain, which is the only one that was missing.
     Every ticket transition in epic #1680 has therefore been manual, and four
     cards were measured showing a state their work had left weeks earlier.

  3. It is not a documentation gap. 7 of 7 authors followed the doc; the doc is
     about commit SUBJECTS and is correct as written. Nothing required the PR
     BODY to carry a closing keyword and nothing checked that it did.

WHAT THIS CHECKS, and it is deliberately one thing: TITLE-TO-REFERENCE
AGREEMENT. If the title names a ticket, the PR must reference that ticket --
either by CLOSING it, or by declaring the relationship it does have.

TWO ADMISSIBLE FORMS, NOT ONE (tracebloc/backend#2616). Until this file read the
PR body, `closingIssuesReferences` was the only thing it read, and that field is
populated ONLY by a closing keyword. So a PR whose title named a ticket had
exactly one satisfying form: PROMISE TO CLOSE IT. That is wrong for the shape
this org uses most -- a parent ticket with several child PRs each titled
`type(NNNN): ...`, none of which finishes it.

  Measured: `.github#356`, titled `fix(2284): the gate tolerates a review that
  never came, and says so`, whose body says accurately `Part of
  tracebloc/backend#2284` and which does NOT close #2284 -- arming the required
  context is that ticket's acceptance criterion and #356 only makes arming
  possible. Its only two remedies were to add a FALSE `Closes`, or to drop
  `(2284)` from the title, passing the check by deleting the traceability the
  check exists to enforce. Both make the repo's records worse, and the second is
  the one four PRs in one day actually took (.github#349, #350, #352, #353) --
  which is how #354 came to advise "keep the number out of the subject". A gate
  whose only remedy is a lie or a deletion is rule 4's failure mode: it trains
  people to step over the tier.

  So a NON-CLOSING reference in the BODY now satisfies the title too, reported
  as its own state (`MENTIONED`) rather than folded into `LINKED`. The two are
  different promises, and a green run should say which one was made.

THE NON-CLOSING VOCABULARY IS DERIVED, NOT INVENTED HERE (rule 1). The org
declares its partial-work form in `org-standards.md` -- today
`Part of tracebloc/backend#N` -- and this file PARSES that declaration rather
than holding a second copy of it. Add `Refs <owner>/<repo>#N` to the canon and
this gate accepts it with no code change; a hand-written list here would have
agreed with itself while disagreeing with the canon. The derivation FAILS CLOSED:
a canon declaring no non-closing form at all is a cannot-tell (exit 2), never a
silent reversion to closing-only, because reverting is precisely the defect
above.

WHAT IT DELIBERATELY DOES NOT READ: GitHub's cross-reference graph. A bare body
mention of `#N` already creates a cross-reference on the issue, so reading that
graph would make ANY prose mention satisfying -- including the loose `#N` this
file refuses to read in a TITLE for measured reasons (backend#2309, below). An
explicit declared keyword is a statement of relationship; an incidental mention
is not, and that difference is the whole value of this gate.

THE CHECK RUNS IN BOTH DIRECTIONS (tracebloc/design-system-v2#123). Until that
ticket, everything above was conditional on the TITLE naming a ticket -- so on
the majority of PRs, whose titles name none, this file asserted nothing at all
and said `nothing-named`. That is a one-way check, and its silent failure
shipped: `tracebloc/.github#381` merged 2026-08-29 with `Closes #2767` in its
body, bare-form and therefore resolved against `.github`, where no #2767 exists.
It registered nothing, this gate went green because the title
(`fix(set-status): ...`) names no ticket, and `tracebloc/backend#2767` was closed
by hand afterwards. `design-system-v2#119` is the same shape with the keyword
inside backticks, which GitHub also does not parse.

  So the second direction: THE BODY MAY NOT CLAIM A CLOSE THE GRAPH DOES NOT
  CARRY. A closing keyword in the body with no matching `closingIssuesReferences`
  entry is `INERT` and FAILS, whatever the title says. See `inert_closing_refs`,
  which holds the three measured narrowing rules that make it quiet enough to
  arm -- measured on 300 recent PRs across six repos, where the armed scan
  reports on exactly one, and that one is `.github#381`.

WHAT IT STILL DOES NOT CHECK, said here so nobody reads more into a green run.
A title that names NO ticket, on a PR whose body claims nothing, is reported and
PASSES. The guard cannot tell a PR that genuinely has no ticket from one whose
author wrote a bare-prose title, and refusing both would be a rule about titles
rather than about links -- with legitimate casualties: `chore(global_model):
delete dead TF FLOPS path` (backend#2333) names no ticket in its title and links
backend#2288 correctly, and a docs-only or CI-only PR may honestly have no ticket
at all. Requiring a ticket per PR is a separate decision from requiring the link,
and this file still only implements the second. The new direction does not change
that: it fires on a CLAIM THE AUTHOR MADE, never on the absence of one.

  This is also why the fix for design-system-v2#123 is NOT the one that ticket's
  description proposed -- "if the title names a ticket, require
  `closingIssuesReferences` to be non-empty". That is what this file did until
  2026-08-27 and it was changed on purpose (backend#2616, `MENTIONED` above);
  restoring it would leave a child PR the same two remedies it had then, a false
  `Closes` or a deleted title number, and four PRs in one day took the second.
  Nothing below ever requires the graph to be non-empty.

DERIVED, NOT RESTATED (CLAUDE.md rule 1). Three readings, no fourth:

  * the PR's real TITLE, parsed for the forms the fleet actually writes
  * the PR's real BODY, for keywords GitHub's own vocabulary defines and for the
    non-closing form the canon declares
  * the PR's real `closingIssuesReferences`, the same graph
    `kanban-closure-router.yml` consumes

There is no list of tickets, no list of repos and no list of authors in this
file. The comparison is between live reads.

THE FOUR TITLE FORMS ARE MEASURED, NOT IMAGINED. Sampled across .github,
backend, release-train and client-runtime:

    fix(2218): three age checks compared local wall-clock against UTC
        -> a bare number in the conventional-commit scope. client-runtime#365,
           whose closing ref is tracebloc/backend#2218 -- a number in the 2000s
           in a repo whose own issues are in the 300s, which is why a bare
           number may NEVER be resolved against the current repo (see below).
    fix(#349): a credential-refresh worker must not outlive its test
        -> `#N` in the scope. client-runtime#351, closing client-runtime#349.
    feat(kanban): a bug-labelled issue lands in Ready, not Backlog (backend#2348)
        -> a trailing parenthetical naming the repo WITHOUT the owner.
           .github#309, closing tracebloc/backend#2348.
    fix(hop): the one-hop mutex is a concurrency group (release-train#110)
        -> the same, same-repo. release-train#112.

A BARE `#N` OUTSIDE PARENTHESES IS NOT READ AS A TICKET, and that is a
measurement rather than a taste: backend#2309 is titled `test(platform): pin
the three early-close call sites #2271 rewrote`. `#2271` there is prose about a
PR, and its actual closing ref is backend#2308. Scanning loose `#N` would have
demanded a link to #2271 and reddened a compliant PR. Precision over recall,
the same bargain house-rules.sh documents.

A BARE NUMBER RESOLVES AGAINST NO REPO. `fix(2218)` in client-runtime means
backend#2218; `fix(90)` in release-train means release-train#90. Both are real
and measured, so inventing a repo for a bare number would be this file holding
a mapping of its own -- exactly what rule 1 forbids. So a bare number is
satisfied by a closing ref with that NUMBER in any repo, and nothing stronger
is claimed. When the title spells the repo out, the repo is checked.

THE CROSS-REPO TRAP, which is the failure mode even for authors who DO add the
keyword. A bare `Closes #2302` inside `release-train` resolves against
`release-train`, not `backend`. Two outcomes, and this guard names both:

  * no such issue in the current repo -> nothing links at all -> UNLINKED.
  * an issue with that number DOES exist locally -> the PR links the WRONG
    issue and closes it on merge. `.github` is where this bites: its own issue
    numbers are in the 300s and it references backend tickets in the 2000s and
    300s alike, so `Closes #304` in `.github` links `.github#304`. The verdict
    for that is WRONG_REPO, reported separately from UNLINKED, because the
    remedy differs: one is a missing line, the other is a line that has to be
    rewritten in the full `owner/repo#num` form.

FAIL CLOSED, AND "CANNOT TELL" IS ITS OWN REPORTED STATE (rule 3). Each of
these exits 2 -- never a pass, and never counted as a finding against the
author, because they are malfunctions of the check:

  * an absent or blank title
  * the GraphQL read failing, returning `errors[]`, or `pullRequest: null`
  * a truncated `closingIssuesReferences` page -- `totalCount > len(nodes)`.
    This one is load-bearing for a REASON BEYOND PAGINATION: a link to an issue
    the token cannot read comes back missing from `nodes` while `totalCount`
    still counts it, which is indistinguishable from "not linked" and would
    redden a compliant cross-repo PR. The token this runs under is the
    org-scoped `tracebloc-release-train` installation (measured 2026-08-23:
    `issues: write`, `pull_requests: write`, `repository_selection: all`), so
    the cross-repo read is expected to succeed -- and if it ever does not, this
    test is what says so instead of blaming the author.
  * a node with no `repository.nameWithOwner` or no integer `number`
  * the query no longer asking for `totalCount`, which would make the test
    above inert

SOFT_FAIL GOVERNS FINDINGS, NEVER THE CHECK'S OWN INTEGRITY. Under
`SOFT_FAIL=true` a finding annotates and exits 0; every malfunction above still
exits 2. That split is action-pins' rule in code-quality.yml, kept verbatim.

THE MEASURED SHAPE, VERBATIM (the backend#2114 lesson: a fixture that encodes
an assumed shape lets the test pass while the code cannot fire). Captured with
`gh api graphql` on 2026-08-23:

    {"data":{"repository":{"pullRequest":{"number":365,
      "title":"fix(2218): three age checks compared local wall-clock against UTC",
      "closingIssuesReferences":{"totalCount":1,"nodes":[
        {"number":2218,"repository":{"nameWithOwner":"tracebloc/backend"}}]}}}}}

    zero refs      -> {"totalCount":0,"nodes":[]}
    absent PR      -> {"data":{"repository":{"pullRequest":null}},
                       "errors":[{"type":"NOT_FOUND", ...}]}   (gh exits nonzero)

The repository is `repository { nameWithOwner }` -- one field, owner included.
The selftest's fixtures are those bytes, not a hand-drawn approximation.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
from collections import namedtuple

# ---------------------------------------------------------------- parsing ----

# The conventional-commit prefix. The TYPE is `[A-Za-z]+` rather than the
# declared `feat|fix|docs|sec|ci|chore` list on purpose: a type the org adds
# later must not silently stop the scope being read (rule 1 -- a list here would
# be this file's own copy of a vocabulary that lives in org-standards.md). The
# selftest walks the declared list OUT of org-standards.md and asserts every
# member parses, which is the direction that cannot go stale (rule 6).
SCOPE_RE = re.compile(r"^\s*[A-Za-z]+\s*\(([^()]*)\)\s*!?:")

# A scope that is a bare ticket number: `fix(2218):`, `fix(#349):`.
SCOPE_BARE_RE = re.compile(r"^#?(\d+)$")
# A scope that names the repo: `fix(backend#2218):`, `fix(tracebloc/.github#300):`.
#
# THE REPO CLASS ADMITS A LEADING DOT, same as PAREN_REPO_RE below and for the same
# reason: `.github` starts with one. The requirement used to be stated only on that
# pattern and missed here, and it was not a live defect -- PAREN_REPO_RE matches
# ANYWHERE in the title, a scope is a parenthetical, so a `.github` scope was still
# detected with the right repo and number. It reported `source='parenthetical'`
# though, making a `.github` scope the one input whose label does not say where the
# reference actually was.
#
# THE COINCIDENCE WAS LOAD-BEARING, which is why this is worth a character rather
# than a note. Detection of a `.github` scope depended on PAREN_REPO_RE not being
# anchored away from the scope position -- and narrowing it to stop double-matching
# the scope is a natural future change. That would have silently stopped detecting
# `.github`-scoped tickets: fail-open, on the one repo this gate lives in. Now the
# two patterns agree and neither depends on the other's reach (Asad, .github#314).
SCOPE_REPO_RE = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9.][A-Za-z0-9._-]*)#(\d+)$")

# A parenthetical anywhere in the title. `.github` starts with a dot, so the
# repo character class must admit a leading one.
PAREN_REPO_RE = re.compile(r"\(\s*(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9.][A-Za-z0-9._-]*)#(\d+)\s*\)")
PAREN_BARE_RE = re.compile(r"\(\s*#(\d+)\s*\)")

TicketRef = namedtuple("TicketRef", "owner repo number source")

# ---------------------------------------------------------------- verdicts ---

PASS = "pass"                    # every ticket the title names is linked
FAIL = "fail"                    # at least one is not
NOTHING_NAMED = "nothing-named"  # the title names no ticket; nothing to assert
DRAFT = "draft"                  # a draft is exempt; see evaluate()

# Per-ref classifications. FOUR, and each is a different remedy -- which is the
# whole reason they are not collapsed: `wrong-repo` needs a line rewritten,
# `missing` needs a line added, `linked` and `mentioned` need nothing. Folding
# `mentioned` into `linked` would leave a green run unable to say whether the PR
# promised to CLOSE the ticket or merely to reference it.
LINKED = "linked"          # a closing keyword links it: the strongest form
MENTIONED = "mentioned"    # a declared non-closing keyword names it in the body
MISSING = "missing"        # referenced no way at all -- the real defect
WRONG_REPO = "wrong-repo"  # that NUMBER is linked, in the wrong repository
# The title names a repo the org does not have (tracebloc/.github#416). The house
# shorthand `engine#898` used to be read as repo `tracebloc/engine`, which does
# not exist, so a body line `Part of tracebloc/tracebloc-engine#898` did not
# satisfy it and the gate went red on a correctly linked PR -- teaching authors
# to write the long form only where the gate looks, which is rule 7's shape (a
# check that teaches the bypass). Shorthand is now RESOLVED against the org's
# declared repo list (`resolve_repo`), and a short name that resolves to nothing
# is this verdict: a finding that says so, never a silent pass and never a guess.
UNKNOWN_REPO = "unknown-repo"

# A finding about the BODY rather than about a title reference, so it is not a
# per-ref classification and does not appear in `classify`. It is the OTHER
# DIRECTION of the same property, and the direction nothing checked until
# tracebloc/design-system-v2#123: the body writes one of GitHub's own closing
# keywords and GitHub registered no closing link for it. The keyword is inert --
# it reads to a human as a promise to close the ticket, the promise is not in the
# graph, and on merge nothing closes. See `inert_closing_refs`.
INERT = "inert"

# GITHUB'S CLOSING VOCABULARY. The one list in this file that CANNOT be derived
# from anything in this org: it is GitHub's, documented under "Linking a pull
# request to an issue", and no file here declares it. It is used only to
# SUBTRACT -- a keyword the canon declares that is ALREADY a closing keyword is
# not a new admissible form. So if GitHub ever adds one, the failure direction is
# that the canon's `Closes` example would be read as a non-closing form, and the
# selftest pins exactly that.
GITHUB_CLOSING_KEYWORDS = frozenset(
    ["close", "closes", "closed", "fix", "fixes", "fixed",
     "resolve", "resolves", "resolved"]
)

# The canon whose non-closing vocabulary this file parses. It sits beside
# `scripts/`, and the host workflow checks this whole repo out, so it is present
# wherever this script runs.
STANDARDS_FILE = "org-standards.md"
# The org's declared repo list, beside the canon and checked out with it. The
# shorthand map is DERIVED from this file (rule 1): `engine` resolves to
# `tracebloc-engine` because the inventory declares a repo of that name under
# the declared `org:`, not because a table here says so. A hand-typed table
# would be this file's own copy of the org's repo list, agreeing with itself
# while a repo is added, renamed or archived.
INVENTORY_FILE = "repo-inventory.yml"
# The two lines this file reads out of the inventory. The `repos:` block's
# members are its 2-space-indented keys; nested keys sit deeper and are not
# repos. Read with the stdlib on purpose -- this script has no dependency the
# host workflow would have to install, and it must stay that way.
INVENTORY_ORG_RE = re.compile(r"^org:\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:#.*)?$", re.MULTILINE)
INVENTORY_REPOS_HEAD_RE = re.compile(r"^repos:\s*(?:#.*)?$", re.MULTILINE)
INVENTORY_REPO_KEY_RE = re.compile(r"^  ([A-Za-z0-9.][A-Za-z0-9._-]*):(?:\s.*)?$")

# A backticked span in the canon shaped `<keyword> <ref>#<number>`:
#   `Part of tracebloc/backend#N`  -> keyword "Part of"  (admissible)
#   `Closes <owner>/<repo>#N`      -> keyword "Closes"   (subtracted as GitHub's)
# The number may be a literal or the placeholder `N`, because the canon writes
# both. Nothing else in a backtick span matches: `backend#1234` carries no
# keyword and `fix/1234-ingest-timeout` carries no `#`, so neither is read as a
# declaration.
DECLARED_FORM_RE = re.compile(
    r"^([A-Za-z][A-Za-z]*(?:\s+[A-Za-z]+)*?)\s+[^\s#]+#(?:\d+|N)$"
)
BACKTICKED_RE = re.compile(r"`([^`\n]+)`")

# The tail a body reference may take after its keyword. Same character classes as
# the title patterns above, and for the same measured reason: `.github` leads
# with a dot, and an owner is optional.
BODY_TAIL_RE = (
    r"(?:(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9.][A-Za-z0-9._-]*))?#(\d+)"
)

# GitHub's OWN closing vocabulary, shaped as a body scan, for the inert check.
# Longest alternative first so `closes #1` cannot be matched as `close` plus a
# tail that then fails -- correct either way through backtracking, but a regex
# whose correctness depends on backtracking order is a regex nobody should have
# to reason about twice.
#
# `\s*:?\s+` admits the COLON FORM deliberately. `Fixes: #47` is not a GitHub
# closing keyword (GitHub wants `Fixes #47`), so it is one of the shapes that
# reads as a promise and registers nothing -- exactly what this scan is for.
#
# The `(?<![A-Za-z0-9])` lookbehind is SUBSUMED TODAY by LINE_LEAD_RE, which
# admits no alphanumeric between the line start and the keyword, so `preclose #1`
# is already rejected there and no case can distinguish the two. It stays anyway,
# and for the reason SCOPE_REPO_RE records at length: the last time a property
# here held only by coincidence with another pattern's reach, narrowing that
# other pattern would have silently disabled detection. Neither should depend on
# the other. The mutation for it was removed rather than faked, because a
# mutation that cannot be caught is not coverage.
CLOSING_IN_BODY_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    + "|".join(sorted(GITHUB_CLOSING_KEYWORDS, key=lambda word: (-len(word), word)))
    + r")\s*:?\s+"
    + BODY_TAIL_RE,
    re.IGNORECASE,
)

# An HTML comment. STRIPPED FROM EVERY BODY SCAN, and this is a measurement
# rather than tidiness: `.github/pull_request_template.md` puts
# `Closes #123 · Cross-repo: Closes tracebloc/backend#2364` inside a comment as
# instructions to the author. A PR that leaves the template's comment in place --
# which is the default, since a comment is invisible in the rendered body -- would
# otherwise report an inert `Closes` the author never wrote. The org's own
# template would have been the largest single source of false positives.
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# The placeholder a stripped span leaves behind. It MUST NOT be whitespace, and
# that is the whole point rather than a detail: every body pattern joins its
# keyword to its tail with `\s+`, which crosses newlines, so replacing a fenced
# block with "" or " " can SPLICE a keyword before the fence onto a `#N` after it
# and manufacture a reference nobody wrote -- a fail-open introduced by the code
# that was meant to close one. U+FFFD is in none of the character classes above,
# so it terminates any match that tries to cross it.
STRIPPED = "�"

# EVERYTHING A CLOSING CLAIM MAY BE PRECEDED BY ON ITS OWN LINE: list bullets,
# blockquote markers, heading hashes, emphasis, and the backtick that made
# design-system-v2#119 inert in the first place. Nothing else -- a keyword with
# WORDS in front of it on the same line is prose, not a claim.
#
# THIS IS THE RULE THE 300-PR MEASUREMENT ADDED, and it is doing most of the
# work: without it the scan reported on 4 of 300 recent PRs and only ONE was
# real. The other three are all the same English, a past participle used
# adjectivally, which is unavoidable while GitHub's vocabulary contains
# `closed`/`fixed`/`resolved`:
#
#   backend#3037       "...and the closed backend#2643 audited that exact layer"
#   release-train#153  "...pointing at the closed backend#1680"
#   release-train#145  "STAGE 1 (backend#2979) already closes #2976 ... so this
#                       PR does not"
#
# IT ALSO REPLACED A RULE THAT LOOKED NECESSARY AND WAS NOT. An earlier draft
# skipped any keyword with a negation (`not`, `never`, `n't`, `without`) within
# 40 characters behind it, aimed at exactly the prose above. Re-measured with
# this rule in place, that one changed the verdict on 0 of the same 300 PRs --
# every negated instance in the fleet is mid-sentence, so this rule had already
# rejected it -- and the mutation harness said so by refusing to be caught when
# the negation skip was deleted. It is gone rather than kept as insurance: a
# narrowing rule nothing can break is a rule nobody can reason about, and
# release-train#145 shows why a backward window was the wrong shape anyway (its
# negation TRAILS the keyword).
#
# The rule costs no recall on either known true positive, both of which write
# the claim where GitHub's own documentation puts it -- at the start of a line:
# `.github#381` has `Closes #2767` as line 1, design-system-v2#119 has
# `Fixes #47` opening its line. Precision over recall, the same bargain
# PAREN_BARE_RE strikes in a title.
LINE_LEAD_RE = re.compile(r"^[\s>*+\-#`_~]*$")

QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      title
      isDraft
      body
      baseRefName
      baseRepository { defaultBranchRef { name } }
      closingIssuesReferences(first: 50) {
        totalCount
        nodes {
          number
          repository { nameWithOwner }
        }
      }
    }
  }
}
"""

# Derived from the query itself, so a page size changed in one place cannot
# leave a message here claiming the other.
PAGE_CAP = max([int(n) for n in re.findall(r"first:\s*(\d+)", QUERY)] or [0])

# The connections whose completeness this gate depends on. One today; named as a
# tuple so adding a second cannot forget the self-check below.
PAGED_CONNECTIONS = ("closingIssuesReferences",)


class Unreadable(Exception):
    """The check could not establish the facts. Never a pass, never the author's fault."""


def connections_missing_totalcount(query=QUERY):
    """Which paged connections in `query` fail to ask for `totalCount`.

    Without `totalCount` the truncation test is inert and a page that lost a
    node -- to pagination OR to a permission filter -- reads as "not linked".
    Derived by reading the real query, so it cannot drift from it. Same
    self-check, same reason, as bugbot-gate.py and stale-backlog.py.
    """
    missing = []
    for name in PAGED_CONNECTIONS:
        match = re.search(name + r"\(first:\s*\d+\)\s*\{([^{]*)", query)
        if match is None or "totalCount" not in match.group(1):
            missing.append(name)
    return missing


def require_complete(kind, conn):
    """Return a connection's nodes, or refuse because the page is not the whole set."""
    if not isinstance(conn, dict):
        raise Unreadable("%s was not a connection object, so it cannot be read" % kind)
    total = conn.get("totalCount")
    if total is None:
        raise Unreadable(
            "%s did not report totalCount, so a missing node cannot be told from "
            "an absent link. The query must keep asking for it." % kind
        )
    nodes = conn.get("nodes") or []
    if total > len(nodes):
        raise Unreadable(
            "%s counts %d link(s) but only %d came back (the query asks for %d). "
            "A node lost to pagination or to a permission filter is "
            "indistinguishable from an absent link, so this is a finding about "
            "the check, not about the PR." % (kind, total, len(nodes), PAGE_CAP)
        )
    return nodes


def parse_title(title):
    """Every ticket the TITLE names, in order, de-duplicated.

    Raises Unreadable on an absent or blank title: a title is the one input this
    check cannot do without, and "no title" must not read as "names no ticket".
    """
    if title is None or not str(title).strip():
        raise Unreadable(
            "the PR title is absent or blank, so what it names cannot be read"
        )
    title = str(title)
    refs = []

    scope_match = SCOPE_RE.match(title)
    if scope_match is not None:
        # Comma-separated scopes (`fix(2364,2365):`) are one token each. Splitting
        # costs nothing and a scope that names two tickets is not a special case.
        for token in scope_match.group(1).split(","):
            token = token.strip()
            bare = SCOPE_BARE_RE.match(token)
            if bare is not None:
                refs.append(TicketRef(None, None, int(bare.group(1)), "scope"))
                continue
            spelled = SCOPE_REPO_RE.match(token)
            if spelled is not None:
                refs.append(
                    TicketRef(spelled.group(1), spelled.group(2), int(spelled.group(3)), "scope")
                )

    for match in PAREN_REPO_RE.finditer(title):
        refs.append(
            TicketRef(match.group(1), match.group(2), int(match.group(3)), "parenthetical")
        )
    for match in PAREN_BARE_RE.finditer(title):
        refs.append(TicketRef(None, None, int(match.group(1)), "parenthetical"))

    unique = []
    for ref in refs:
        key = (
            (ref.owner or "").lower(),
            (ref.repo or "").lower(),
            ref.number,
        )
        if key not in [
            ((r.owner or "").lower(), (r.repo or "").lower(), r.number) for r in unique
        ]:
            unique.append(ref)
    return unique


def declared_reference_keywords(standards_text):
    """The org's declared NON-CLOSING reference keywords, parsed from the canon.

    Reads `org-standards.md`'s own backticked examples and keeps those whose
    leading keyword is not one of GitHub's closing keywords. Today that is
    exactly `Part of`; a `Refs <owner>/<repo>#N` added to the canon tomorrow is
    picked up here with no edit to this file (rule 1 -- the alternative is this
    file holding a second copy of a vocabulary the canon owns, which would agree
    with itself while disagreeing with the canon).

    Returns a sorted list of the keywords as WRITTEN, so the refusal message can
    quote the canon's own spelling back at the author.
    """
    found = {}
    for span in BACKTICKED_RE.findall(standards_text or ""):
        match = DECLARED_FORM_RE.match(span.strip())
        if match is None:
            continue
        keyword = " ".join(match.group(1).split())
        if keyword.split()[0].lower() in GITHUB_CLOSING_KEYWORDS:
            continue
        found.setdefault(keyword.lower(), keyword)
    return [found[key] for key in sorted(found)]


def reference_keywords(root=None):
    """`declared_reference_keywords` against the real canon on disk.

    FAILS CLOSED IN BOTH DIRECTIONS (rule 3). An unreadable canon is a
    cannot-tell, and so is a canon that declares NO non-closing form: the
    tempting fallback -- carry on with closing-only -- is not a degraded mode,
    it is tracebloc/backend#2616 restored, and it would restore it silently.
    """
    root = pathlib.Path(__file__).resolve().parents[1] if root is None else pathlib.Path(root)
    path = root / STANDARDS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Unreadable(
            "%s could not be read (%s), so the org's declared non-closing "
            "reference form cannot be derived. This check parses that file "
            "rather than holding its own copy of the vocabulary." % (STANDARDS_FILE, exc)
        )
    keywords = declared_reference_keywords(text)
    if not keywords:
        raise Unreadable(
            "%s declares no non-closing reference form (nothing shaped "
            "`<keyword> <owner>/<repo>#N` that is not one of GitHub's closing "
            "keywords). Without one, the only way to satisfy a title that names "
            "a ticket is to promise to CLOSE it -- which is the defect "
            "tracebloc/backend#2616 fixed, so this refuses rather than quietly "
            "reverting to it." % STANDARDS_FILE
        )
    return keywords


def declared_repos(inventory_text):
    """(org, [repo names]) as `repo-inventory.yml` DECLARES them.

    Names are the 2-space-indented keys of the top-level `repos:` block, in file
    order; the block ends at the next column-0 line. Comment and blank lines are
    skipped, deeper keys are the entries' properties and are not repos. Returns
    an empty list when the block is absent or empty and `None` for a missing
    `org:` -- `known_repos` turns both into refusals.
    """
    text = inventory_text or ""
    org_match = INVENTORY_ORG_RE.search(text)
    org = org_match.group(1) if org_match is not None else None
    names = []
    head = INVENTORY_REPOS_HEAD_RE.search(text)
    if head is not None:
        for line in text[head.end():].splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line.startswith(" "):
                break
            key = INVENTORY_REPO_KEY_RE.match(line)
            if key is not None:
                names.append(key.group(1))
    return org, names


def known_repos(root=None):
    """`declared_repos` against the real inventory on disk: (org, names).

    FAILS CLOSED IN BOTH DIRECTIONS (rule 3), exactly as `reference_keywords`
    does for the canon: an unreadable inventory is a cannot-tell, and so is one
    that declares no `org:` or no repos -- with an empty list every short name
    in every title would be "unknown", which is a finding about this check
    wearing the author's name.
    """
    root = pathlib.Path(__file__).resolve().parents[1] if root is None else pathlib.Path(root)
    path = root / INVENTORY_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Unreadable(
            "%s could not be read (%s), so the org's declared repository list "
            "cannot be derived and a short repo name in the title cannot be "
            "resolved. This check parses that file rather than holding its own "
            "copy of the list." % (INVENTORY_FILE, exc)
        )
    org, names = declared_repos(text)
    if org is None:
        raise Unreadable(
            "%s declares no `org:`, so a short repo name cannot be resolved "
            "against the org's repositories." % INVENTORY_FILE
        )
    if len(names) == 0:
        raise Unreadable(
            "%s declares no repositories under `repos:`, so every repo name a "
            "title could carry would read as unknown. Refusing rather than "
            "reporting the org's whole fleet as a finding." % INVENTORY_FILE
        )
    return org, names


def resolve_repo(ref, inventory):
    """The reference with its repo spelled as the inventory declares it, or as-is.

    THREE OUTCOMES, one function, so `classify` and the report agree on which
    happened (rule 9):
      * a repo the inventory declares, in any case -> its declared spelling;
      * `<org>-<short>` declared -> that repo: `engine` is `tracebloc-engine`
        because the inventory has one and only because of that (.github#416);
      * anything else -> returned unchanged, and `classify` reports UNKNOWN_REPO.
    A reference that names ANOTHER owner is left alone: the inventory says
    nothing about other orgs' repositories, and pretending it does would turn a
    correctly written `otherorg/engine#5` into a finding about this org.
    """
    if ref.repo is None:
        return ref
    org, names = inventory
    if ref.owner is not None and ref.owner.lower() != org.lower():
        return ref
    short = ref.repo.lower()
    exact = [name for name in names if name.lower() == short]
    if exact:
        return ref._replace(repo=exact[0])
    full = "%s-%s" % (org.lower(), short)
    candidates = [name for name in names if name.lower() == full]
    if len(candidates) == 1:
        return ref._replace(repo=candidates[0])
    return ref


def is_known_repo(ref, inventory):
    """Whether `ref` names a repo the inventory declares (or another owner's)."""
    if ref.repo is None:
        return True
    org, names = inventory
    if ref.owner is not None and ref.owner.lower() != org.lower():
        return True
    return ref.repo.lower() in [name.lower() for name in names]


def readable_text(body):
    """The body with HTML comments removed. Both body scans read this.

    AN HTML COMMENT IS THE ONE THING STRIPPED, and the boundary is measured
    rather than chosen. It cost 300 PRs to find, so it is written down:

      * A COMMENT IS INVISIBLE TO A REVIEWER, so nothing inside one can be the
        author's statement about the PR. Both scans need it gone, for opposite
        reasons -- `<!-- Part of tracebloc/backend#47 -->` satisfying a title
        invisibly is the second half of tracebloc/design-system-v2#123, and the
        `Closes #123` in `.github/pull_request_template.md`'s own instruction
        comment would otherwise be read as a claim on every PR that leaves the
        template in place.
      * A BACKTICKED OR FENCED SPAN IS NOT STRIPPED, and an earlier draft of
        this function that stripped both was wrong in a way only the corpus
        showed: `design-system-v2#227` and `#228` write their declaration as
        `` `Part of tracebloc/design-system-v2#89` ``, in backticks, on the
        first line -- an honest, in-use convention that stripping code spans
        turns from `MENTIONED` into `MISSING`. Two of 300 recent PRs, both
        merged, both correct. GitHub parses `Part of` not at all, so backticks
        change nothing about who can read it: a human, and a human reads a
        code-formatted line perfectly well.

    THE COST IS STATED RATHER THAN HIDDEN: a `Part of ...#N` inside a FENCED
    block -- a pasted review, a quoted docs example -- still satisfies a title,
    which is `design-system-v2#123`'s case D and remains open. It is a fenced
    block's visibility that keeps it open: a reviewer sees it, and every
    measured instance of a code-formatted declaration in this fleet is an honest
    one. Closing it needs evidence of the dishonest shape actually occurring,
    which nobody has.
    """
    if body is None:
        return ""
    return HTML_COMMENT_RE.sub(STRIPPED, str(body))


def targets_default_branch(pr):
    """True / False / None -- does this PR target its base repo's default branch?

    `None` means the payload did not say. It is a distinct answer from `False`
    because the caller treats them the same way for opposite reasons, and a
    two-valued return would have hidden one of them.

    WHY THIS IS LOAD-BEARING FOR THE INERT CHECK, and not a taste: GitHub
    populates `closingIssuesReferences` only for a PR whose base is the
    repository's DEFAULT branch. So on a stacked PR or a train promotion the
    graph is empty BY GITHUB'S DESIGN, and a perfectly honest `Closes #N` in the
    body registers nothing through no fault of the author. `backend#2779` is the
    measured case: a plain first-line `Closes #2775.`, empty graph, because it is
    stacked onto `fix/2770-explicit-record-count`. Without this rule every
    stacked PR and every promotion PR in the fleet reports.
    """
    base = pr.get("baseRefName")
    default = ((pr.get("baseRepository") or {}).get("defaultBranchRef") or {}).get("name")
    if not isinstance(base, str) or not isinstance(default, str) or not base or not default:
        return None
    return base == default


def inert_closing_refs(body, links, base_default):
    """The closing keywords in the BODY that GitHub registered nothing for.

    THE ONE FUNCTION the inert assertions and the inert mutations both go
    through (CLAUDE.md rule 9). Three narrowing rules live here, each derived
    from a measured false positive rather than imagined, because the naive scan
    is far too noisy to arm. MEASURED over 300 recent PRs across
    `.github`/`backend`/`client`/`release-train`/`frontend-app`/`design-system-v2`:
    with all three armed the scan reports on exactly ONE, and that one is the
    real finding (`.github#381`). A fourth candidate rule was measured and
    DELETED for adding nothing; see LINE_LEAD_RE.

      A. THE GRAPH MUST BE EMPTY. An author whose PR already registered a closing
         link demonstrably knows how to write one, so remaining keyword text is
         prose about some OTHER ticket -- `client#885` and `release-train#134`
         quote docs examples, `.github#345`/`#360` and `frontend-app#948` discuss
         other PRs. It is also what keeps this check from reverting
         tracebloc/backend#2616: it NEVER requires the graph to be non-empty. It
         fires on the author's own claim, never on the absence of one, so a
         docs-only or CI-only PR with no ticket and no keyword stays green --
         which is the `NOTHING_NAMED` fail-open this file has always documented,
         left exactly as it was.
      B. THE CLAIM MUST OPEN ITS LINE. See LINE_LEAD_RE, which carries the
         measurement: it is what took a 4-in-300 report rate with three prose
         false positives down to the one real finding.
      C. THE BASE MUST BE THE DEFAULT BRANCH, or be readable at all. See
         `targets_default_branch`. A `None` skips too: with no base information
         the check cannot tell an inert keyword from GitHub's by-design empty
         graph on a non-default base, and manufacturing a finding out of a
         missing field would put the defect in this file. That is the second
         deliberate fail-open here, alongside DRAFT, and the mutation harness
         pins it in both directions for the reason stated there -- a fail-open
         nobody can break is a fail-open nobody notices.

    Rule A is why no per-ref comparison against `links` is needed below: with an
    EMPTY graph, every keyword-shaped body reference matches nothing in it by
    construction.
    """
    if links:
        return []
    if base_default is not True:
        return []
    text = readable_text(body)
    refs = []
    seen = set()
    for match in CLOSING_IN_BODY_RE.finditer(text):
        lead = text[text.rfind("\n", 0, match.start()) + 1:match.start()]
        if LINE_LEAD_RE.match(lead) is None:
            continue
        ref = TicketRef(match.group(2), match.group(3), int(match.group(4)), "body-closing")
        key = ((ref.owner or "").lower(), (ref.repo or "").lower(), ref.number)
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs


def parse_body(body, keywords):
    """Every ticket the BODY references with a declared non-closing keyword.

    A body is optional -- GitHub returns `""` or `null` for an empty one -- and
    an absent body is not a malfunction, unlike an absent title: it simply
    references nothing. Only a keyword the canon DECLARES counts; a loose `#N`
    does not, for the same measured reason `PAREN_BARE_RE` is anchored to
    parentheses in a title (backend#2309: `#2271` there was prose about a PR).
    """
    if body is None:
        return []
    body = str(body)
    refs = []
    for keyword in keywords:
        spaced = r"\s+".join(re.escape(word) for word in keyword.split())
        pattern = re.compile(r"(?<![A-Za-z0-9])" + spaced + r"\s+" + BODY_TAIL_RE, re.IGNORECASE)
        for match in pattern.finditer(body):
            refs.append(
                TicketRef(match.group(1), match.group(2), int(match.group(3)), "body")
            )

    unique = []
    seen = set()
    for ref in refs:
        key = ((ref.owner or "").lower(), (ref.repo or "").lower(), ref.number)
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


def closing_refs(pr):
    """The PR's real link graph as (owner, repo, number) triples."""
    nodes = require_complete("closingIssuesReferences", pr.get("closingIssuesReferences"))
    out = []
    for node in nodes:
        if not isinstance(node, dict):
            raise Unreadable(
                "a closingIssuesReferences node came back null -- the link graph is "
                "partially unreadable, which is not evidence of an absent link"
            )
        full = ((node.get("repository") or {}).get("nameWithOwner")) or ""
        number = node.get("number")
        if "/" not in full or not isinstance(number, int):
            raise Unreadable(
                "a closingIssuesReferences node had no owner/name repository or no "
                "integer number: %s" % json.dumps(node)[:200]
            )
        owner, _, name = full.partition("/")
        out.append((owner, name, number))
    return out


def same_ticket(ref, other):
    """Whether two references can name the same ticket.

    Used for BODY references, where -- unlike the link graph, which always comes
    back fully qualified from GitHub -- either side may name no repo. A side that
    names no repo constrains nothing, the same decision `classify` makes for a
    bare title number and for the same measured reason: a bare number resolves
    against no repo, so inventing one would be this file holding a mapping.
    """
    if ref.number != other.number:
        return False
    if ref.repo is None or other.repo is None:
        return True
    if ref.repo.lower() != other.repo.lower():
        return False
    if ref.owner is not None and other.owner is not None:
        if ref.owner.lower() != other.owner.lower():
            return False
    return True


def classify(ref, links, mentions=(), inventory=None):
    """LINKED / WRONG_REPO / MENTIONED / MISSING / UNKNOWN_REPO for one title reference.

    `inventory` is `known_repos()`'s (org, names); when given, a repo-named ref
    that resolves to no declared repo is UNKNOWN_REPO BEFORE the graph is
    consulted -- a link at that number in some repo cannot vouch for a name
    this org does not have, and neither can a body mention (.github#416). `None`
    keeps the historical behaviour for callers that have no inventory.

    THE ONE FUNCTION the assertions and the mutations both go through
    (CLAUDE.md rule 9). No caller re-implements this comparison.

    ORDER IS LOAD-BEARING. `WRONG_REPO` is decided BEFORE any body reference is
    consulted, so a correct `Part of tracebloc/backend#304` cannot mask a
    `Closes #304` that will close `.github#304` on merge. The wrong-repo trap is
    a finding about a link that FIRES; a truthful mention alongside it does not
    make the firing link right. `MENTIONED` only ever rescues a ref that is
    linked NOWHERE -- which was the sole defect this gate was built to catch.
    """
    if inventory is not None and not is_known_repo(ref, inventory):
        return UNKNOWN_REPO
    number_seen = False
    for owner, name, number in links:
        if number != ref.number:
            continue
        number_seen = True
        if ref.repo is None:
            # A bare number names no repo, so any repo satisfies it. See the
            # header: client-runtime#365's `fix(2218)` is backend#2218 and
            # release-train#95's `fix(90)` is release-train#90.
            return LINKED
        if ref.repo.lower() != name.lower():
            continue
        if ref.owner is not None and ref.owner.lower() != owner.lower():
            continue
        return LINKED
    if number_seen:
        return WRONG_REPO
    for mention in mentions:
        if same_ticket(ref, mention):
            return MENTIONED
    return MISSING


def _spell(ref):
    if ref.repo is None:
        return "#%d" % ref.number
    if ref.owner is None:
        return "%s#%d" % (ref.repo, ref.number)
    return "%s/%s#%d" % (ref.owner, ref.repo, ref.number)


def _full(ref, org):
    """`<owner>/<repo>#N` for a repo-named ref: its own owner, else the inventory's org.

    The remedies used to spell `tracebloc/%s` for every repo-named ref, so a
    title naming `otherorg/engine#5` was advised to add `Closes tracebloc/engine#5`
    -- a repo in the wrong org that does not exist. The org is the inventory's
    declared one, not a literal here (tracebloc/.github#416).
    """
    return "%s/%s#%d" % (ref.owner or org, ref.repo, ref.number)


def _remedy_forms(keywords, target):
    """Spell the canon's declared non-closing forms for `target`.

    Quotes the canon's OWN spelling, so the advice cannot drift from what
    `parse_body` will actually accept -- both come from the same parsed list.
    """
    return " or ".join("`%s %s`" % (word, target) for word in keywords)


def _why_lines():
    """The standing explanation every FAIL ends with.

    ONE COPY, because there are now two paths that fail -- a title reference the
    body does not make good, and a body claim the graph does not carry -- and a
    second copy of this argument would be the drift `_remedy_forms` exists to
    avoid.
    """
    return [
        "",
        "Why this matters, one link at a time: a CLOSING link is what makes GitHub "
        "close the issue when the PR merges (every train repo's default branch is "
        "`develop`, measured 2026-08-23, so a closing keyword DOES fire there); "
        "that close is the event `kanban-closure-router.yml` routes on, mirroring "
        "the PR's base to the card. With no reference at all, a PR that DOES finish "
        "its ticket closes nothing, the router never runs, and the card sits where "
        "it was while every check is green (tracebloc/backend#2364).",
        "That argument is about the PR that FINISHES a ticket, and this check no "
        "longer pretends it covers the others: a child PR's own card is routed from "
        "its base with no closing keyword involved, so for a child the requirement "
        "is traceability, not closure -- which a declared body reference gives "
        "truthfully (tracebloc/backend#2616).",
        "Edit the PR TITLE or BODY and this check re-runs by itself: its callers "
        "listen for `edited`, which is the event both fields fire "
        "(tracebloc/backend#2556). No push and no manual re-run is needed.",
    ]


def evaluate(pr, standards_root=None):
    """(verdict, lines) for one PR payload. Raises Unreadable on a cannot-tell.

    `standards_root` overrides where the canon is read from, for the selftest
    only; production passes nothing and reads the checkout this script lives in.
    """
    # A DRAFT IS EXEMPT, and this is the one deliberate fail-open. A draft is
    # work in progress by definition and the board does not act on it; the
    # non-draft transition is what set-pr-status.yml reacts to, so that is where
    # the convention has to hold. Both directions of this are pinned by the
    # mutation harness, because a fail-open nobody can break is a fail-open
    # nobody notices.
    if pr.get("isDraft"):
        return DRAFT, ["This PR is a draft; the link is checked when it is marked ready."]

    named = parse_title(pr.get("title"))
    links = closing_refs(pr)
    keywords = reference_keywords(root=standards_root)
    # SHORTHAND IS RESOLVED ONCE, HERE, for the title and the body alike, so a
    # `Part of engine#898` in the body satisfies an `engine#898` in the title
    # through the same declared name (tracebloc/.github#416).
    inventory = known_repos(root=standards_root)
    refs = [resolve_repo(ref, inventory) for ref in named]
    resolved = [
        (before, after) for before, after in zip(named, refs)
        if before.repo is not None and after.repo is not None
        and before.repo.lower() != after.repo.lower()
    ]
    mentions = [
        resolve_repo(ref, inventory)
        for ref in parse_body(readable_text(pr.get("body")), keywords)
    ]
    inert = inert_closing_refs(pr.get("body"), links, targets_default_branch(pr))
    linked_text = ", ".join("%s/%s#%d" % triple for triple in links) or "none"
    keyword_text = ", ".join("`%s`" % word for word in keywords)
    mention_text = ", ".join(_spell(ref) for ref in mentions) or "none"

    # THE SECOND DIRECTION, ASSERTED BEFORE THE TITLE IS CONSULTED AT ALL
    # (tracebloc/design-system-v2#123). Until this block, the whole check was
    # conditional on `parse_title` returning something, so a PR whose title
    # names no ticket -- most PRs -- had nothing asserted about it. The measured
    # consequence was not hypothetical: `tracebloc/.github#381` merged
    # 2026-08-29 with `Closes #2767` in its body, resolved against `.github`
    # where no #2767 exists, registered nothing, went green on this very check
    # because its title (`fix(set-status): ...`) names no ticket -- and
    # `backend#2767` had to be closed by hand. `design-system-v2#119` is the
    # same shape with the keyword backticked.
    #
    # It is deliberately NOT a rule about titles, and so does not touch the
    # `NOTHING_NAMED` fail-open below: it fires on a CLAIM THE AUTHOR MADE, never
    # on the absence of one. A docs-only or CI-only PR with no ticket writes no
    # closing keyword and stays green, which is why this could be armed without
    # first deciding the separate question of whether every PR needs a ticket.
    inert_lines = []
    for ref in inert:
        inert_lines.append(
            "%s -- the body writes a closing keyword for this, and GitHub "
            "registered NO closing link. The keyword is inert: it reads as a "
            "promise to close the ticket, and on merge nothing will close. "
            "GitHub parses a closing keyword only in PLAIN body text, only in "
            "the exact form `Closes <owner>/<repo>#N` (no colon after the "
            "keyword, not inside backticks or a fenced block), and a bare "
            "`Closes #%d` resolves against THIS repo -- which registers nothing "
            "at all when the ticket lives elsewhere."
            % (_spell(ref), ref.number)
        )
        inert_lines.append(
            "    IF THIS PR DOES NOT FINISH THAT TICKET, that is fine and the "
            "remedy is not a working `Closes`: say what is true instead -- %s. "
            "Only remove the claim or make it real; do not leave a promise the "
            "graph does not carry."
            % _remedy_forms(keywords, "<owner>/<repo>#%d" % ref.number)
        )

    if not refs:
        if not inert:
            return NOTHING_NAMED, [
                "The title names no ticket, so there is nothing to assert about "
                "the link graph from the title. This check compares a title "
                "against the links; it does not require that a PR have a ticket.",
                "",
                "closingIssuesReferences: %s" % linked_text,
            ]
        return FAIL, [
            "The title names no ticket, but the BODY claims to close one and "
            "GitHub registered no closing link.",
            "closingIssuesReferences: %s" % linked_text,
            "",
        ] + inert_lines + _why_lines()

    results = [(ref, classify(ref, links, mentions, inventory)) for ref in refs]
    bad = [(ref, verdict) for ref, verdict in results if verdict not in (LINKED, MENTIONED)]
    lines = [
        "Title names: %s" % ", ".join(_spell(ref) for ref in refs),
        "closingIssuesReferences: %s" % linked_text,
        "Body references (%s): %s" % (keyword_text, mention_text),
    ]
    if resolved:
        # SAY WHAT WAS RESOLVED AND FROM WHERE, so a reader can check the map
        # against the file it came from rather than against this script.
        lines.append(
            "Shorthand resolved against %s: %s"
            % (INVENTORY_FILE, ", ".join(
                "%s -> %s" % (before.repo, after.repo) for before, after in resolved))
        )
    lines.append("")
    if inert:
        # FOLDED INTO THE SAME FAIL rather than reported separately, so a PR that
        # satisfies its title AND carries an inert keyword is still red. Both are
        # the same defect: a reference the body asserts and the graph does not.
        return FAIL, lines + inert_lines + _why_lines()
    if not bad:
        # SAY WHICH FORM SATISFIED EACH REF. A green run that cannot distinguish
        # "promises to close it" from "says it is part of it" hides the very
        # distinction backend#2616 added.
        return PASS, lines + [
            "Every ticket the title names is referenced:",
        ] + [
            "  %s -- %s"
            % (_spell(ref), "closing link" if verdict == LINKED
               else "declared body reference (does not close it)")
            for ref, verdict in results
        ]

    org, names = inventory
    for ref, verdict in bad:
        if verdict == UNKNOWN_REPO:
            # NO GUESS. The old behaviour advised `Closes tracebloc/<short>#N` for
            # a repo that does not exist, which no author could follow and which
            # GitHub registers as nothing. Name the list the check derived from,
            # name the one resolution it tried, and send the author to the
            # spelling the org actually declares.
            lines.append(
                "%s -- the title names repo `%s`, which is not a repository in the "
                "org inventory (%s declares: %s), and no `%s-%s` is declared to "
                "resolve the shorthand to. This check cannot tell which ticket "
                "that is, so it does not guess: write the repo's declared name "
                "(`<repo>#%d`) or the bare `#%d`."
                % (_spell(ref), ref.repo, INVENTORY_FILE, ", ".join(names),
                   org, ref.repo, ref.number, ref.number)
            )
        elif verdict == WRONG_REPO:
            lines.append(
                "%s -- an issue numbered %d IS linked, but in the wrong repository. "
                "This is the cross-repo trap: a bare `Closes #%d` resolves against "
                "THIS repo, so it links (and on merge closes) the wrong issue. Use "
                "the full form: `Closes %s`."
                % (_spell(ref), ref.number, ref.number, _full(ref, org))
            )
        elif ref.repo is not None:
            lines.append(
                "%s -- named in the title, linked nowhere. A title reference is "
                "inert: GitHub creates a closing link only from a keyword in the "
                "PR BODY. Add `Closes %s` to the body -- and note that "
                "a bare `Closes #%d` would resolve against THIS repo, not the "
                "ticket's."
                % (_spell(ref), _full(ref, org), ref.number)
            )
            lines.append(
                "    IF THIS PR DOES NOT FINISH THAT TICKET, do not write `Closes` "
                "-- say what is true instead: %s. That satisfies this check "
                "without promising a close, and without deleting the number from "
                "your title (tracebloc/backend#2616)."
                % _remedy_forms(keywords, _full(ref, org))
            )
        else:
            # THE REMEDY MUST NOT NAME A REPO THE CHECK CANNOT KNOW. A bare title
            # number is repo-agnostic by decision -- `_classify` returns LINKED for
            # ANY repo at that number (see its comment: client-runtime#365's
            # `fix(2218)` is backend#2218, release-train#95's `fix(90)` is
            # release-train#90). An earlier version defaulted to
            # `ref.repo or "backend"` and so advised `Closes tracebloc/backend#N`
            # on every bare number. Following that advice on a `release-train`
            # ticket links the WRONG issue, turns this check GREEN, and closes the
            # wrong ticket on merge -- a remedy that manufactures the defect the
            # gate exists to prevent (Bugbot, .github#314).
            lines.append(
                "%s -- named in the title, linked nowhere. A title reference is "
                "inert: GitHub creates a closing link only from a keyword in the "
                "PR BODY. The title names only a NUMBER, so this check cannot tell "
                "which repo owns it -- and will accept a link to any repo at that "
                "number. Add `Closes <owner>/<repo>#%d` for the repo that actually "
                "owns the ticket (most internal work lives in `tracebloc/backend`, "
                "but confirm it rather than assuming). A bare `Closes #%d` resolves "
                "against THIS repo, which is only right when the ticket lives here."
                % (_spell(ref), ref.number, ref.number)
            )
            lines.append(
                "    IF THIS PR DOES NOT FINISH THAT TICKET, do not write `Closes` "
                "-- say what is true instead: %s. That satisfies this check "
                "without promising a close, and without deleting the number from "
                "your title (tracebloc/backend#2616)."
                % _remedy_forms(keywords, "<owner>/<repo>#%d" % ref.number)
            )
    return FAIL, lines + _why_lines()


# ------------------------------------------------------------------- read ----


def _run_gh(args, env):
    return subprocess.run(args, capture_output=True, text=True, env=env, check=False)


def fetch(owner, name, number, env=None, runner=_run_gh):
    """Read the PR. Any failure raises rather than returning a partial view."""
    env = dict(os.environ if env is None else env)
    proc = runner(
        [
            "gh", "api", "graphql",
            "-f", "query=" + QUERY,
            "-F", "owner=" + owner,
            "-F", "name=" + name,
            "-F", "number=%d" % number,
        ],
        env,
    )
    if proc.returncode != 0:
        raise Unreadable(
            "GraphQL read failed (exit %d): %s"
            % (proc.returncode, (proc.stderr or "").strip()[:400])
        )
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise Unreadable("GraphQL response was not JSON: %s" % exc)
    if payload.get("errors"):
        raise Unreadable("GraphQL returned errors: %s" % json.dumps(payload["errors"])[:400])
    try:
        pr = payload["data"]["repository"]["pullRequest"]
    except (KeyError, TypeError):
        raise Unreadable("GraphQL response had no repository.pullRequest")
    if pr is None:
        raise Unreadable("no such pull request: %s/%s#%d" % (owner, name, number))
    return pr


# ------------------------------------------------------------------ report ---

HEADING = "Closing-ref gate"


def _emit(verdict, lines, hard_error=None, soft=False):
    body = []
    if hard_error is not None:
        body.append("**Cannot tell, so this is a finding about the check.**")
        body.append("")
        body.append("```")
        body.append(str(hard_error))
        body.append("```")
    else:
        label = {
            PASS: "pass",
            FAIL: "FAIL" if not soft else "FAIL (reported only: soft-fail is on)",
            NOTHING_NAMED: "no ticket named in the title",
            DRAFT: "draft, not checked",
        }[verdict]
        body.append("**%s: %s**" % (HEADING, label))
        body.append("")
        body.extend(lines)
    text = "\n".join(body)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write("## %s\n\n%s\n" % (HEADING, text))
        except OSError:
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    repo = os.environ.get("REPO", "")
    number = os.environ.get("PR_NUMBER", "")
    soft = (os.environ.get("SOFT_FAIL") or "").strip().lower() == "true"

    if "/" not in repo or not number.isdigit():
        _emit(FAIL, [], "REPO must be owner/name and PR_NUMBER a number; got %r / %r"
              % (repo, number))
        return 2
    owner, name = repo.split("/", 1)

    # Before any read: if the query stopped asking for totalCount, the
    # truncation test is inert and a filtered link graph would read as an absent
    # one. That is a defect in this file, so it fails the run rather than the
    # author's day.
    blind = connections_missing_totalcount()
    if blind:
        _emit(FAIL, [], "the GraphQL query no longer requests totalCount for: %s. "
                        "Without it a truncated or permission-filtered page cannot "
                        "be detected." % ", ".join(blind))
        return 2

    try:
        pr = fetch(owner, name, int(number))
        verdict, lines = evaluate(pr)
    except Unreadable as exc:
        # A malfunction, not a finding: SOFT_FAIL deliberately does NOT cover
        # this. Same split as action-pins in code-quality.yml.
        print("::error::%s: cannot tell -- %s" % (HEADING, exc))
        _emit(FAIL, [], exc)
        return 2

    _emit(verdict, lines, soft=soft)
    if verdict != FAIL:
        return 0
    level = "warning" if soft else "error"
    # The findings are whatever follows the blank separator `evaluate` emits after
    # its header block. Derived from the blank line rather than a literal index:
    # the header grew by one row when body references started being reported, and
    # a hardcoded `lines[3:]` silently began quoting a header row as a finding.
    body_start = lines.index("") + 1 if "" in lines else 0
    # THE HEADLINE MUST NAME THE DIRECTION THAT FAILED. There are two now, and a
    # single sentence about titles was the whole defect
    # tracebloc/design-system-v2#123 reported: an annotation that says "the title
    # names a ticket" on a PR whose title names none is worse than no annotation,
    # because the author looks at the title and finds nothing wrong.
    print("::%s::%s: this PR's title and body disagree with its link graph. %s"
          % (level, HEADING, " ".join(lines[body_start:])[:600]))
    return 0 if soft else 1


if __name__ == "__main__":
    sys.exit(main())
