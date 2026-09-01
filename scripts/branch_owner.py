#!/usr/bin/env python3
"""Who owns a branch. ONE definition of it (backend#2365).

WHY THIS FILE EXISTS
--------------------
git has several notions of "who owns this branch" and they disagree. The one that
is easiest to reach is the one that is wrong:

    git for-each-ref --format='%(authorname)' refs/remotes/origin

That is the author of the branch's TIP COMMIT, which answers "who touched it
last". Push a review fixup onto someone else's branch -- ordinary, encouraged,
what a reviewer with write access does -- and the tip author becomes you while
nothing about whose work it is has changed.

MEASURED, on this org, 2026-08-23/24 (backend#2365):

    client fix/bugbot-tier0-helm-prereqs  PR #395  author shujaatTracebloc
                                          tip commit author Lukas Wuttke
    client fix/583-wire-ca-proxy          PR #592  author shujaatTracebloc
                                          tip commit author Lukas Wuttke

A "my branches" list built from the tip author claimed both. The next step in
that workflow is `git push origin --delete`, so the cost of the wrong answer is
another engineer's unlanded work. Only the mandatory confirm-the-author step
stopped it.

THE RULE, IN PRIORITY ORDER
---------------------------
1. THE PULL REQUEST AUTHOR. The person who proposed the work. A PR whose head oid
   still equals the branch tip is the strongest form (`pr-exact`); a PR for the
   head name whose oid has been overtaken by later pushes is still the author,
   reported as `pr`.
2. THE FIRST COMMIT NOT ON THE DEFAULT BRANCH, for a branch that never had a PR.
   The commit that brought the ref into being, not the one that happens to be on
   the end of it.
3. NOTHING. `unattributable`, said out loud, with the reason.

THE TIP AUTHOR IS NOT ON THAT LIST, AND `attribute()` CANNOT REACH IT. It is not
a parameter. There is no code path in this module that can return it, which is a
stronger guarantee than a comment asking future edits not to.

WHY `CreateEvent` IS NOT SIGNAL 2, THOUGH backend#2365 PROPOSED IT
------------------------------------------------------------------
The ref creator is the ideal answer for a branch with no PR, and GitHub records
it as a `CreateEvent` in the repo's event stream. That stream cannot serve it.
Measured on `tracebloc/backend`, 2026-08-24:

    GET /repos/tracebloc/backend/events   300 events, hard cap
                                          (page 4 -> HTTP 422, pagination refused)
    the whole 300-event window spanned    07:30Z .. 11:24Z -- under four hours
    branch CreateEvents inside it         2

An active repo pushes its own branch-creation events out of the window in hours,
and the branches this rule exists for are months old. So the event stream answers
for almost nothing, and a signal that answers for almost nothing is worse than
one that says "cannot tell": it looks like coverage. The first-commit author is
derivable from the clone for every branch, forever, and is named for what it is.

THE PULL-REQUEST LIST IS READ TO THE END, NOT TO A CEILING (backend#2972)
------------------------------------------------------------------------
This used to ask `gh pr list --limit 1000` and refuse when the answer came back
at the cap. `tracebloc/backend` has 1418 pull requests, so the tool refused on the
org's LARGEST repo -- the one it is most needed on, carrying 62 of the org's 99
stale branches -- and every one of its 108 branches came back `unattributable`
when a complete list attributes 102 of them.

A bigger number would only move the date this happens again. So the read pages to
the end (`gh api graphql --paginate`) and asks the repository for its OWN count on
the same connection, and "did the read finish?" is then a comparison of two
measured numbers rather than a threshold somebody picked:

    rows read == pullRequests.totalCount    ->  the list is complete
    anything else                           ->  refuse, naming both numbers

THE CURSOR VARIABLE MUST BE NAMED `$endCursor`, EXACTLY. `gh --paginate` injects
the next page's cursor into a variable of that name and no other. Misname it and
gh re-requests page 1 forever; the secondary rate limit that loop trips is
invisible to `gh api rate_limit`, so the failure does not even look like one. The
name is asserted by the selftest against the query text rather than trusted.

FAIL CLOSED, AND SAY SO
-----------------------
"No PR was found for this branch" is only a fact when the PR list was actually
read, and completely. If `gh` is missing, unauthenticated, failing, or came back
with fewer rows than the repository says it has, then absence proves nothing --
and falling through to the commit-author path in that state is precisely the
misattribution this module exists to prevent, arrived at from a clean read of the
wrong thing. So a PR-list problem makes EVERY branch `unattributable`, before any
other evidence is weighed. Same posture, and the same reasoning, as
`scripts/git-reap`.

AND A REFUSAL MUST NOT WEAR THE SHAPE OF DATA. `108 branch(es): 0 attributed, 108
unattributable` is also what a genuine "nobody can be named in this repo" answer
looks like, so the two were indistinguishable and a reader who trusted the tool
concluded that every branch in the largest repo was unowned. An INCOMPLETE
pull-request list is therefore not reported as N unattributable rows at all:
`main` names the repo, both counts and the shortfall, prints no rows, and exits
non-zero, so no caller can read it as an inventory. The other PR-list problems
still print rows -- each saying why it was refused -- and the summary line now
carries the one reason with them.

Ambiguity is also a refusal, not a tie-break. Two PRs for one head name by two
different people -- branch names get reused, `fix/typo` twice, months apart -- is
the exact case where guessing costs someone their work.

WHAT THIS RULE STILL CANNOT SEE, named rather than implied
----------------------------------------------------------
* CO-AUTHORED WORK. A PR has exactly one author and `Co-Authored-By:` trailers
  are invisible here. `owner` means "the one person to ask before touching this",
  not a credit list.
* A CHERRY-PICK, or any commit imported from elsewhere. git preserves the
  ORIGINAL author across a cherry-pick and a rebase (only the committer changes),
  so a no-PR branch whose first commit was picked from someone else's work is
  attributed to that someone else. Signal 1 is unaffected; this is a signal-2
  limitation.
* A HANDOVER. The PR author is who proposed it, not who is carrying it now. The
  board's assignee field is the authority on that (RFC-BACKEND-0008 D31), and
  this module deliberately does not try to second-guess it.
* A BOT OR AGENT IDENTITY. `cursoragent@cursor.com`, `github-actions[bot]` and
  friends are returned verbatim. Mapping identities to people is a roster, and a
  hand-written roster is the thing this module refuses to be -- the caller that
  needs display names can map them and own that.

USE
---
    ./scripts/branch_owner.py                       every origin/* branch, TSV
    ./scripts/branch_owner.py fix/123-thing         just these
    ./scripts/branch_owner.py --json                machine-readable
    ./scripts/branch_owner.py --repo owner/name     an explicit repo

or, from another script:

    from branch_owner import attribute, UNATTRIBUTABLE
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import NamedTuple

# The owner value that means "this module refuses to name anybody". Empty, so a
# caller that forgets to check it gets nothing rather than a plausible name.
UNATTRIBUTABLE = ""

# GitHub's own maximum for a GraphQL connection page. Not a number to tune: `first:`
# above 100 is refused by the API, so this is simply the largest page the read can
# ask for, and the page COUNT is whatever the repo's size makes it.
PAGE_SIZE = 100

# The prefix on the one problem string that means "the list is SHORT -- fewer rows
# came back than the repository says it has". A caller cannot branch on an English
# sentence, and `main` has to tell THIS refusal apart from a dead network to answer
# differently, so the marker is a single constant that the seam writes and `main`
# matches rather than a phrase each end spells for itself.
INCOMPLETE_PR_LIST = "REFUSING: incomplete pull-request list --"

# ONE QUERY, PAGED TO THE END, WITH THE REPOSITORY'S OWN COUNT BESIDE THE ROWS.
#
# `totalCount` sits on the SAME connection as `nodes`, so it counts exactly the set
# the pages walk -- which is what lets `pull_requests` derive "the read finished"
# instead of asserting it. Unfiltered, like the `--state all` it replaces.
#
# THE CURSOR VARIABLE IS `$endCursor` AND THE NAME IS LOAD-BEARING: `gh --paginate`
# injects the next cursor into that name and no other, and a misnamed one re-reads
# page 1 forever. The field names are also chosen to match `gh pr list --json
# number,author,headRefName,headRefOid,state,createdAt` one-for-one, so the rows the
# rule receives -- and any caller importing `attribute` -- keep the same shape and
# the same OPEN/CLOSED/MERGED vocabulary.
#
# MEASURED, 2026-09-01: 4 pages / 386 rows for `.github`, 15 pages / 1418 rows for
# `backend`, each run ending with rows == totalCount and no duplicate numbers.
PR_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: %d, after: $endCursor) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { number author { login } headRefName headRefOid state createdAt }
    }
  }
}
""" % PAGE_SIZE

SIGNALS = ("pr-exact", "pr", "first-commit", "unattributable")


class Attribution(NamedTuple):
    """`owner` is UNATTRIBUTABLE unless `signal` names the evidence that found it."""

    owner: str
    signal: str
    why: str


def _refuse(why: str) -> Attribution:
    return Attribution(UNATTRIBUTABLE, "unattributable", why)


def _authors(prs: list) -> "dict[str, list]":
    """login -> the PR rows carrying it. A row with no author is not a vote."""
    out: "dict[str, list]" = {}
    for pr in prs:
        login = ((pr.get("author") or {}).get("login") or "").strip()
        if login:
            out.setdefault(login, []).append(pr)
    return out


def _cite(prs: list) -> str:
    return ", ".join(f"#{pr.get('number')}" for pr in prs)


def attribute(
    branch: str,
    tip_sha: str,
    prs: list,
    first_commit_author: str = "",
    pr_list_problem: str = "",
    first_commit_problem: str = "",
) -> Attribution:
    """Who owns `branch`, from the PR author first and never from the tip commit.

    `prs` are the pull requests for THIS head ref, any state. `tip_sha` is the
    branch's current tip. `first_commit_author` is the git identity of the oldest
    commit on `branch` that is not on the default branch -- "" if there is none or
    it could not be read. `pr_list_problem`, if set, says why the PR list is not
    evidence of absence, and refuses every branch on the spot.
    `first_commit_problem` says why the caller did not measure the oldest commit,
    so a withheld signal is reported as withheld rather than as "no commits".

    There is deliberately no `tip_author` parameter. See the module docstring.
    """
    if pr_list_problem:
        return _refuse(f"the pull-request list is not evidence: {pr_list_problem}")

    stray = [pr for pr in prs if (pr.get("headRefName") or branch) != branch]
    if stray:
        return _refuse(
            f"the caller passed {len(stray)} pull request(s) for another head ref "
            f"({_cite(stray)}); refusing to attribute {branch} from them"
        )

    if prs:
        # A PR whose head oid IS the tip is the branch, not merely a branch that
        # once had this name. Strongest form, so it is decided on its own.
        if tip_sha:
            exact = [pr for pr in prs if (pr.get("headRefOid") or "") == tip_sha]
            if exact:
                by = _authors(exact)
                if len(by) == 1:
                    login, rows = next(iter(by.items()))
                    return Attribution(login, "pr-exact",
                                       f"PR {_cite(rows)} author, head oid matches the tip")
                if len(by) > 1:
                    return _refuse(
                        "two or more pull requests at this exact tip have different "
                        f"authors ({', '.join(sorted(by))}); refusing to pick one"
                    )
                return _refuse(
                    f"the pull request(s) at this exact tip ({_cite(exact)}) carry no "
                    "author -- a deleted account cannot be asked, so this needs a human"
                )

        by = _authors(prs)
        if len(by) == 1:
            login, rows = next(iter(by.items()))
            # SAY WHICH OF THE TWO IT IS. "The tip has moved past the PR head" is
            # a claim about the branch; with no tip in hand it is a claim about
            # nothing, and a caller reading it would believe the branch had been
            # pushed to since the PR. An absent tip is its own sentence.
            moved = ("the tip has moved past the PR head" if tip_sha
                     else "no tip was supplied, so the PR head could not be compared")
            return Attribution(login, "pr", f"PR {_cite(rows)} author; {moved}")
        if len(by) > 1:
            # Branch names get reused. Guessing here is how one person's cleanup
            # reaches another person's work.
            # SAME RULE AS THE `pr` ARM ABOVE: with no tip in hand, "none is at
            # the current tip" asserts a comparison that never ran (Bugbot).
            tail = ("and none is at the current tip" if tip_sha
                    else "and no tip was supplied to break the tie with")
            return _refuse(
                f"{len(prs)} pull requests share this head name with different authors "
                f"({', '.join(sorted(by))}) {tail}"
            )
        return _refuse(
            f"the pull request(s) for this head ({_cite(prs)}) carry no author -- "
            "a deleted account cannot be asked, so this needs a human"
        )

    if first_commit_author:
        return Attribution(first_commit_author, "first-commit",
                           "no pull request; author of the oldest commit not on the "
                           "default branch")

    # "NO COMMITS" AND "NOBODY LOOKED" ARE DIFFERENT ANSWERS, and only one of them
    # is a fact about the branch. A caller that withheld the signal -- because the
    # default branch could not be confirmed, so `default..branch` would have been
    # the wrong range -- says so here, and the refusal reports that instead of a
    # finding from a check that never ran (Bugbot).
    if first_commit_problem:
        return _refuse(
            "no pull request, and the oldest-commit signal was not measured: "
            f"{first_commit_problem}"
        )

    return _refuse(
        "no pull request, and no commit on this branch that is not already on the "
        "default branch, so there is nothing to attribute"
    )


# --------------------------------------------------------------------------
# The seams. Everything above is pure; everything below shells out, and the
# selftest replaces these two rather than the rule.
# --------------------------------------------------------------------------


def _run(args: list) -> "tuple[int, str]":
    """(exit code, stdout). A MISSING BINARY IS AN EXIT CODE, NOT A CRASH.

    `subprocess.run` raises FileNotFoundError when the program is not on PATH,
    and an uncaught traceback out of the gh seam is not the documented refusal --
    it is a tool that stopped without an answer, on the machine most likely to be
    missing gh. Every caller here already handles a non-zero rc by failing closed,
    so that is where an absent binary belongs.
    """
    try:
        proc = subprocess.run(args, capture_output=True, text=True)
    except OSError as exc:
        return 127, f"{args[0]}: {exc}"
    return proc.returncode, proc.stdout.strip()


def repo_identity(repo: str = "") -> "tuple[str, str, str]":
    """(owner, name, problem). The two halves the GraphQL read needs as variables.

    `gh pr list` accepted `owner/name` whole, or resolved the clone itself. GraphQL
    takes the halves separately, so the resolution gh did implicitly happens here --
    and, being a read that can fail, it refuses out loud rather than guessing at a
    repo. Splitting is deliberately strict: `owner/name/extra` is not a repository,
    and quietly reading a DIFFERENT repo's pull requests is a misattribution of the
    same shape as everything else this module refuses.
    """
    if repo:
        owner, slash, name = repo.partition("/")
        if not owner or not slash or not name or "/" in name:
            return "", "", (f"{repo!r} is not owner/name, so the pull-request list "
                            "could not be asked for")
        return owner, name, ""
    rc, out = _run(["gh", "repo", "view", "--json", "nameWithOwner",
                    "--jq", ".nameWithOwner"])
    if rc != 0 or not out:
        return "", "", ("this clone's repository could not be identified -- no gh, "
                        "no auth, no network, or not a repo. Absence from a list "
                        "that was never read proves nothing")
    owner, slash, name = out.partition("/")
    if not owner or not slash or not name:
        return "", "", (f"gh named this clone {out!r}, which is not owner/name")
    return owner, name, ""


def pull_requests(repo: str = "") -> "tuple[dict, str]":
    """(head ref -> [PR rows], problem). A non-empty problem refuses everything.

    THE WHOLE LIST OR A REFUSAL, NEVER A WINDOW (backend#2972). One paged query per
    repo, never one per branch -- a per-branch call burns the API rate limit on a
    repo with hundreds of branches -- read to the end by `gh --paginate` and then
    CHECKED against the repository's own `totalCount`. Three ways the read can be
    unsound, and each is a refusal rather than a short list:

      * fewer rows than `totalCount`  -- pagination stopped early
      * duplicate pull-request numbers -- the same page came back twice, which is
        what a broken cursor looks like on the near side of an infinite loop
      * `totalCount` disagreeing between pages -- the repo changed under the read,
        so no single answer is a fact about it
    """
    owner, name, problem = repo_identity(repo)
    if problem:
        return {}, problem
    where = f"{owner}/{name}"
    # `-f`, NEVER `-F`: `-F` types its value, so a numeric repo name arrives as an
    # int against a `String!` variable and the query is rejected outright --
    # measured, `-F name=123` comes back "Could not coerce value 123 to String"
    # while `-f name=123` resolves and reports the repo simply does not exist.
    rc, out = _run(["gh", "api", "graphql", "--paginate", "--slurp",
                    "-f", f"owner={owner}", "-f", f"name={name}",
                    "-f", f"query={PR_QUERY}"])
    if rc != 0 or not out:
        return {}, ("`gh api graphql` failed -- no gh, no auth, or no network. "
                    "Absence from a list that was never read proves nothing")
    try:
        pages = json.loads(out)
    except json.JSONDecodeError as exc:
        return {}, f"the pull-request list did not parse as JSON ({exc})"
    # `--slurp` wraps the pages in ONE array, so a bare object here means the shape
    # is not what was asked for -- not that there is one page.
    if not isinstance(pages, list):
        return {}, f"the pull-request list is a {type(pages).__name__}, not a list"

    rows: list = []
    totals = set()
    for page in pages:
        page = page if isinstance(page, dict) else {}
        # A GRAPHQL ERROR IS A 200. `errors[]` beside a null `data` is how this API
        # reports a bad field, a missing repo or a permissions problem, and reading
        # only the exit code turns all three into "no pull requests".
        errors = page.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else errors
            said = (first or {}).get("message") if isinstance(first, dict) else first
            return {}, (f"the pull-request query returned an error for {where} "
                        f"({said!r}), so nothing was read")
        conn = (((page.get("data") or {}).get("repository") or {})
                .get("pullRequests"))
        if not isinstance(conn, dict):
            return {}, (f"a page of {where}'s pull-request list carries no "
                        "repository.pullRequests, so the read cannot be trusted")
        nodes = conn.get("nodes")
        if not isinstance(nodes, list):
            return {}, (f"a page's `nodes` is a {type(nodes).__name__}, not a list, "
                        "so the read cannot be trusted")
        rows += nodes
        totals.add(conn.get("totalCount"))

    if len(totals) != 1 or not isinstance(next(iter(totals)), int):
        return {}, (f"{INCOMPLETE_PR_LIST} {where}'s pull-request count was "
                    f"{sorted(totals, key=repr)} across the pages read, so there is "
                    "no single number to check the list against -- either the repo "
                    "changed mid-read or the count was never returned")
    total = next(iter(totals))

    numbers = [row.get("number") for row in rows if isinstance(row, dict)]
    if len(set(numbers)) != len(numbers):
        return {}, (f"{INCOMPLETE_PR_LIST} {where} returned {len(numbers)} rows but "
                    f"only {len(set(numbers))} distinct pull requests -- a page came "
                    "back twice, which is what a broken `$endCursor` looks like, so "
                    "the read is not a list of the repo's pull requests")
    if len(rows) != total:
        return {}, (f"{INCOMPLETE_PR_LIST} {where} reports {total} pull request(s) "
                    f"but the paged read returned {len(rows)} -- {abs(total - len(rows))} "
                    "unaccounted for, so a branch missing from this list may simply "
                    "never have been read. This is a refusal, not an "
                    "'unattributable' finding")

    by_head: dict = {}
    for row in rows:
        by_head.setdefault(row.get("headRefName") or "", []).append(row)
    return by_head, ""


def first_commit_author(ref: str, default: str) -> "tuple[str, str]":
    """(git identity, problem) for the oldest commit on `ref` not on `default`.

    THE TWO EMPTIES ARE DIFFERENT ANSWERS. This returned a bare "" for both a
    FAILED `git log` and a branch that genuinely has no commits off the default
    branch, and `attribute` then reported the second one -- "no commit on this
    branch that is not already on the default branch" -- for both. That is a fact
    about the branch, asserted from a read that never ran: the same defect
    Saqlain and Bugbot caught in `remote_branches`, one seam over, found by
    auditing the other three rather than fixing only the one that was reported.

    NOT the tip, and the difference is the whole point: on the branch that
    prompted this ticket (`client fix/583-wire-ca-proxy`) the oldest commit is
    Shujaat's and the tip is Lukas's.

    `--reverse` with `-n 1` would still give the tip -- git applies the limit
    before reversing -- so the whole list is read and the first line taken.
    """
    rc, out = _run(["git", "log", "--reverse", "--format=%an <%ae>",
                    f"{default}..{ref}"])
    if rc != 0:
        return "", (f"`git log {default}..{ref}` failed, so the oldest-commit "
                    "signal could not be read")
    if not out:
        return "", ""
    return out.splitlines()[0].strip(), ""


def default_branch(repo: str = "") -> "tuple[str, str]":
    """(default ref, problem). A problem disables the FIRST-COMMIT signal only.

    `origin/HEAD` is a LOCAL CACHE written at clone time that `git fetch` never
    updates, so it goes stale the moment the remote's default moves -- measured on
    9 of this org's 19 clones, every one still pointing at `main` after the move
    to `develop` (see `scripts/git-reap`, which verifies it for the same reason).

    A stale default cannot touch signal 1: `gh pr list` here is not filtered by
    base, so the PR author is the PR author either way. It corrupts signal 2
    exactly, because `default..branch` then includes commits that ARE on the real
    default -- and the oldest of those belongs to whoever landed them, not to
    whoever created this branch. That is a misattribution of the same shape as
    the tip-author bug, one layer down, so it is refused rather than used.
    """
    # THE REPOSITORY IS POSITIONAL HERE, AND THAT IS MEASURED, NOT STYLE.
    # `gh repo view` HAS NO `--repo` FLAG -- `gh repo view [<repository>] [flags]` --
    # and the flag form exits 1 without asking anything:
    #
    #   $ gh repo view --json defaultBranchRef --repo tracebloc/backend
    #   unknown flag: --repo                                   (2026-09-01)
    #
    # This built the flag form, so THE AUTHORITATIVE LOOKUP WAS UNREACHABLE ON EVERY
    # `--repo` RUN -- including the `--repo tracebloc/backend` invocation the tool
    # exists for. It fell through to `origin/HEAD`, a clone-time cache, and the
    # first-commit signal was then withheld for every branch while the message said
    # the remote "could not be confirmed". The remote was never asked; the command
    # was malformed. The no-argument form still resolves from the clone, which is
    # the only reason the bug stayed invisible (backend#2972).
    args = ["gh", "repo", "view"]
    if repo:
        args.append(repo)
    args += ["--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"]
    rc, out = _run(args)
    if rc == 0 and out:
        # THE REMOTE'S ANSWER IS AUTHORITATIVE ABOUT THE NAME, NOT ABOUT THIS
        # CLONE. `gh` can name a default branch this checkout has never fetched,
        # and then every `default..branch` range fails -- which the first-commit
        # seam now reports honestly, but once per branch, blaming N ranges for one
        # missing ref. Diagnose it here instead, where it is one fact (Bugbot).
        ref = f"origin/{out}"
        vrc, _ = _run(["git", "rev-parse", "--verify", "--quiet", ref])
        if vrc != 0:
            return ref, (f"the remote's default branch is {out}, but this clone has "
                         f"no {ref} -- fetch it before the oldest-commit signal "
                         "can mean anything")
        return ref, ""

    rc, out = _run(["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"])
    if rc == 0 and out:
        return out, (f"the remote's default branch could not be confirmed, and {out} "
                     "is a clone-time cache that git never refreshes")
    for guess in ("origin/develop", "origin/main", "origin/master"):
        rc, _ = _run(["git", "rev-parse", "--verify", "--quiet", guess])
        if rc == 0:
            return guess, ("the remote's default branch could not be confirmed and "
                           f"origin/HEAD is unset, so {guess} is a guess")
    return "", "no default branch could be found at all"


def remote_branches(default: str) -> "tuple[list, str]":
    """([(short name, tip sha)], problem) for every origin/* branch but the
    integration ones.

    THE PROBLEM STRING IS THE POINT. An earlier version returned the bare list and
    collapsed a failed `for-each-ref` into `[]`, which `main` then printed as
    `0 branch(es)` and exited 0 -- a clean bill of health from a read that never
    happened, while the other two seams here refuse explicitly. "I could not read
    the branch list" and "this clone has no branches" are different answers and
    only one of them is a fact (Bugbot).
    """
    protected = {"develop", "staging", "main", "master", "gh-pages", "HEAD",
                 default.split("/", 1)[-1]}
    rc, out = _run(["git", "for-each-ref", "--format=%(refname:short)%09%(objectname)",
                    "refs/remotes/origin"])
    if rc != 0:
        return [], ("`git for-each-ref` failed, so this clone's branch list could "
                    "not be read -- and an empty inventory would read as "
                    "'nothing to attribute'")
    found = []
    for line in out.splitlines():
        ref, _, sha = line.partition("\t")
        # `refs/remotes/origin/HEAD` has the SHORT NAME `origin` -- no slash, so
        # stripping the remote prefix leaves the remote itself, and it lands in
        # the output as a branch called `origin`. That bogus row is not
        # hypothetical: it put one per repo into the 2026-08-14 inventory (1212
        # rows vs 1193 real branches) and a sweep built from it would try to
        # delete a branch named after the remote. A short name with no slash is
        # not a branch on `origin`, whatever it points at.
        if "/" not in ref or ref.endswith("/HEAD"):
            continue
        name = ref.split("/", 1)[1]
        if name in protected:
            continue
        found.append((name, sha))
    return found, ""


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Who owns a branch (backend#2365).")
    ap.add_argument("branch", nargs="*", help="branches to attribute (default: all)")
    ap.add_argument("--repo", default="", help="owner/name (default: this clone's)")
    ap.add_argument("--default", default="", help="default branch ref (default: derived)")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    if args.default:
        default, default_problem = args.default, ""
    else:
        default, default_problem = default_branch(args.repo)
    if not default:
        sys.stderr.write("branch_owner: cannot determine the default branch -- "
                         "refusing to guess\n")
        return 2

    prs, problem = pull_requests(args.repo)
    for note in (problem, default_problem):
        if note:
            sys.stderr.write(f"branch_owner: {note}\n")

    # AN INCOMPLETE PR LIST IS A REFUSAL, SO DO NOT SERVE IT AS ROWS (backend#2972).
    # Every row below would be `unattributable` for this one reason, and the tally
    # they add up to is character-for-character what a repo with genuinely
    # unattributable branches prints. Nothing is printed and the exit code carries
    # the refusal, so no caller can mistake a short read for the repo.
    if problem.startswith(INCOMPLETE_PR_LIST):
        return 2

    refs, refs_problem = remote_branches(default)
    if refs_problem:
        # REFUSE, do not report zero. Without the ref list there is no tip for any
        # branch and no way to tell whether a named one even exists, so every
        # answer below would be weaker than it looks and the summary line would
        # say so to nobody.
        sys.stderr.write(f"branch_owner: {refs_problem}\n")
        return 2
    known = dict(refs)
    wanted = [(b, known.get(b, "")) for b in args.branch] if args.branch \
        else sorted(known.items())

    # A branch named on the command line that this clone has never fetched still
    # gets a PR-author answer, which is sound -- but it has no tip and no history,
    # so say that rather than let the reader assume the weaker signals were tried.
    missing = [b for b in args.branch if b not in known]
    if missing:
        sys.stderr.write("branch_owner: not among this clone's origin refs, so no "
                         "tip and no commit history to fall back on: "
                         f"{', '.join(missing)}\n")

    out = []
    for name, sha in wanted:
        # The first-commit signal is withheld when EITHER the PR list or the
        # default branch is untrustworthy -- the first because "no PR" is then
        # unproven, the second because the "oldest unique commit" is then measured
        # against the wrong branch. Such a branch is reported unattributable.
        # The oldest-commit signal is only consulted when both the PR list and the
        # default branch are trustworthy -- the first because "no PR" is otherwise
        # unproven, the second because `default..branch` would be the wrong range.
        # When it IS consulted and the read fails, that failure is carried through
        # as its own reason rather than becoming "no commits".
        usable = not problem and not default_problem
        author, author_problem = (first_commit_author(f"origin/{name}", default)
                                  if usable else ("", ""))
        att = attribute(name, sha, prs.get(name, []), author,
                        problem, default_problem or author_problem)
        out.append({"branch": name, "owner": att.owner, "signal": att.signal,
                    "why": att.why})

    if args.as_json:
        print(json.dumps(out, indent=1))
    else:
        print("branch\towner\tsignal\twhy")
        for r in out:
            print(f"{r['branch']}\t{r['owner']}\t{r['signal']}\t{r['why']}")

    unattributed = sum(1 for r in out if r["signal"] == "unattributable")
    # SAY WHEN THE TALLY IS NOT A MEASUREMENT. The short-read refusal above never
    # reaches this line, but the other PR-list problems do, and they refuse every
    # row for one reason -- which a bare count hides just as completely. Carrying
    # the reason here is what stops the same misreading on the paths that do print.
    because = f" -- every row refused for one reason: {problem}" if problem else ""
    print(f"\n{len(out)} branch(es): {len(out) - unattributed} attributed, "
          f"{unattributed} unattributable{because}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
