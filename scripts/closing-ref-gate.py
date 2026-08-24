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

WHAT THIS CHECKS, and it is deliberately one thing: TITLE-TO-LINK AGREEMENT.
If the title names a ticket, the PR's own link graph must contain that ticket.

WHAT IT DOES NOT CHECK, said here so nobody reads more into a green run.
A title that names NO ticket is reported and PASSES. The guard cannot tell a
PR that genuinely has no ticket from one whose author wrote a bare-prose title,
and refusing both would be a rule about titles rather than about links -- with
legitimate casualties: `chore(global_model): delete dead TF FLOPS path`
(backend#2333) names no ticket in its title and links backend#2288 correctly.
Requiring a ticket per PR is a separate decision from requiring the link, and
this file only implements the second.

DERIVED, NOT RESTATED (CLAUDE.md rule 1). Two readings, no third:

  * the PR's real TITLE, parsed for the forms the fleet actually writes
  * the PR's real `closingIssuesReferences`, the same graph
    `kanban-closure-router.yml` consumes

There is no list of tickets, no list of repos and no list of authors in this
file. The comparison is between two live reads.

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
# A scope that names the repo: `fix(backend#2218):`, `fix(tracebloc/backend#2218):`.
SCOPE_REPO_RE = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)$")

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

# Per-ref classifications. Three, never two: `wrong-repo` and `missing` have
# different remedies and are reported apart.
LINKED = "linked"
MISSING = "missing"
WRONG_REPO = "wrong-repo"

QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      title
      isDraft
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


def classify(ref, links):
    """LINKED / WRONG_REPO / MISSING for one title reference against the link graph.

    THE ONE FUNCTION the assertions and the mutations both go through
    (CLAUDE.md rule 9). No caller re-implements this comparison.
    """
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
    return WRONG_REPO if number_seen else MISSING


def _spell(ref):
    if ref.repo is None:
        return "#%d" % ref.number
    if ref.owner is None:
        return "%s#%d" % (ref.repo, ref.number)
    return "%s/%s#%d" % (ref.owner, ref.repo, ref.number)


def evaluate(pr):
    """(verdict, lines) for one PR payload. Raises Unreadable on a cannot-tell."""
    # A DRAFT IS EXEMPT, and this is the one deliberate fail-open. A draft is
    # work in progress by definition and the board does not act on it; the
    # non-draft transition is what set-pr-status.yml reacts to, so that is where
    # the convention has to hold. Both directions of this are pinned by the
    # mutation harness, because a fail-open nobody can break is a fail-open
    # nobody notices.
    if pr.get("isDraft"):
        return DRAFT, ["This PR is a draft; the link is checked when it is marked ready."]

    refs = parse_title(pr.get("title"))
    links = closing_refs(pr)
    linked_text = ", ".join("%s/%s#%d" % triple for triple in links) or "none"

    if not refs:
        return NOTHING_NAMED, [
            "The title names no ticket, so there is nothing to assert about the "
            "link graph. This check compares a title against the links; it does "
            "not require that a PR have a ticket.",
            "",
            "closingIssuesReferences: %s" % linked_text,
        ]

    results = [(ref, classify(ref, links)) for ref in refs]
    bad = [(ref, verdict) for ref, verdict in results if verdict != LINKED]
    lines = [
        "Title names: %s" % ", ".join(_spell(ref) for ref in refs),
        "closingIssuesReferences: %s" % linked_text,
        "",
    ]
    if not bad:
        return PASS, lines + ["Every ticket the title names is linked."]

    for ref, verdict in bad:
        if verdict == WRONG_REPO:
            lines.append(
                "%s -- an issue numbered %d IS linked, but in the wrong repository. "
                "This is the cross-repo trap: a bare `Closes #%d` resolves against "
                "THIS repo, so it links (and on merge closes) the wrong issue. Use "
                "the full form: `Closes tracebloc/%s#%d`."
                % (_spell(ref), ref.number, ref.number, ref.repo, ref.number)
            )
        elif ref.repo is not None:
            lines.append(
                "%s -- named in the title, linked nowhere. A title reference is "
                "inert: GitHub creates a closing link only from a keyword in the "
                "PR BODY. Add `Closes tracebloc/%s#%d` to the body -- and note that "
                "a bare `Closes #%d` would resolve against THIS repo, not the "
                "ticket's."
                % (_spell(ref), ref.repo, ref.number, ref.number)
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
    lines.append("")
    lines.append(
        "Why this matters, one link at a time: this graph is what makes GitHub "
        "close the issue when the PR merges (every train repo's default branch is "
        "`develop`, measured 2026-08-23, so a closing keyword DOES fire there); "
        "that close is the event `kanban-closure-router.yml` routes on, mirroring "
        "the PR's base to the card. With the graph empty, nothing closes, the "
        "router never runs, and the card sits where it was while every check is "
        "green (tracebloc/backend#2364). Edit the PR BODY, then re-run this check "
        "-- a body edit is not a push and produces no new run by itself."
    )
    return FAIL, lines


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
    print("::%s::%s: the title names a ticket this PR does not link. %s"
          % (level, HEADING, " ".join(lines[3:])[:600]))
    return 0 if soft else 1


if __name__ == "__main__":
    sys.exit(main())
