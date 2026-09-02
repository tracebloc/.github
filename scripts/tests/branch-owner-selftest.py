#!/usr/bin/env python3
"""Cases for the one branch-ownership rule (backend#2365).

THE CASE THIS FILE EXISTS FOR is the first one below, and it is not synthetic:
`client fix/bugbot-tier0-helm-prereqs` (PR #395) and `client fix/583-wire-ca-proxy`
(PR #592) are @shujaatTracebloc's branches with Lukas's fixup commit on the tip.
A tip-author list claimed both, and the next command in that workflow deletes.

No network, no git, no gh: the rule is pure and the two seams are stubbed.
"""
import contextlib
import io
import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from branch_owner import (  # noqa: E402
    INCOMPLETE_PR_LIST,
    PAGE_SIZE,
    PR_QUERY,
    SIGNALS,
    UNATTRIBUTABLE,
    attribute,
)
import branch_owner as _m  # noqa: E402

P, F = 0, 0


def ok(m):
    global P
    P += 1
    print(f"PASS  {m}")


def bad(m):
    global F
    F += 1
    print(f"FAIL  {m}")


def eq(what, got, want):
    if got == want:
        ok(f"{what}: {got!r}")
    else:
        bad(f"{what}: got {got!r}, want {want!r}")


def pr(number, login, head, oid="", state="MERGED"):
    """A `gh pr list --json ...` row. `login=None` is a deleted account."""
    return {"number": number, "author": None if login is None else {"login": login},
            "headRefName": head, "headRefOid": oid, "state": state,
            "createdAt": f"2026-0{number % 9 + 1}-01T00:00:00Z"}


# Everything `attribute` returns goes through here, so an invariant cannot be
# broken by a case that forgets to check it.
SEEN = []


def att(*a, **kw):
    got = attribute(*a, **kw)
    SEEN.append(got)
    return got


# --------------------------------------------------------------------------
# 1. THE MEASURED CASE. A branch whose PR author differs from its tip author
#    must report the PR AUTHOR.
# --------------------------------------------------------------------------
TIP = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

for name, number in (("fix/bugbot-tier0-helm-prereqs", 395), ("fix/583-wire-ca-proxy", 592)):
    got = att(name, TIP, [pr(number, "shujaatTracebloc", name, TIP)],
              first_commit_author="Lukas Wuttke <lukas@tracebloc.io>")
    eq(f"{name} is Shujaat's, not the tip pusher's", got.owner, "shujaatTracebloc")
    eq(f"{name} says which signal decided it", got.signal, "pr-exact")

# THE TIP AUTHOR IS UNREACHABLE BY CONSTRUCTION, not by convention. If a future
# edit adds it as a parameter, this reddens before any behaviour test has to.
params = list(inspect.signature(attribute).parameters)
eq("attribute() takes no tip-author parameter",
   [p for p in params if "tip" in p and "author" in p], [])
eq("attribute()'s parameters are the declared six", params,
   ["branch", "tip_sha", "prs", "first_commit_author", "pr_list_problem",
    "first_commit_problem"])

# ... and the fixup pusher's identity is nowhere in the answer, even though it
# was handed in as the first-commit fallback.
leak = [s for s in SEEN if "lukas" in (s.owner + s.why).lower()]
eq("the tip pusher's identity does not leak into the answer", leak, [])

# --------------------------------------------------------------------------
# 2. FAIL CLOSED. A PR list that was not read is not evidence of absence, and a
#    perfect PR row plus a perfect commit author must NOT rescue it.
# --------------------------------------------------------------------------
PROBLEM = "`gh pr list` failed -- no gh, no auth, or no network"
blocked = att("fix/123-thing", TIP, [pr(1, "saqlainsyed007", "fix/123-thing", TIP)],
              first_commit_author="Saqlain <saqlain@tracebloc.io>", pr_list_problem=PROBLEM)
eq("an unread PR list names nobody", blocked.owner, UNATTRIBUTABLE)
eq("an unread PR list is reported as unattributable", blocked.signal, "unattributable")
if PROBLEM in blocked.why:
    ok("the refusal quotes the specific problem, not a generic 'cannot tell'")
else:
    bad(f"the refusal does not quote the problem: {blocked.why!r}")

# --------------------------------------------------------------------------
# 3. NO PR -> the OLDEST commit not on the default branch. Never the tip.
# --------------------------------------------------------------------------
nopr = att("mssql-setup-readme", TIP, [],
           first_commit_author="saqlain <saqlain@Syeds-MacBook-Pro.local>")
eq("a no-PR branch is attributed to its first commit",
   (nopr.owner, nopr.signal),
   ("saqlain <saqlain@Syeds-MacBook-Pro.local>", "first-commit"))

# ... and with nothing to attribute at all, it says so rather than picking.
empty = att("develop-backup-20231120T094000GMT", TIP, [], first_commit_author="")
eq("no PR and no unique commit is unattributable",
   (empty.owner, empty.signal), (UNATTRIBUTABLE, "unattributable"))

# A WITHHELD SIGNAL IS NOT A FINDING. When the caller could not trust the default
# branch it passes no commit author -- and reporting "no commit not already on the
# default branch" would be a fact about the branch, asserted from a check nobody
# ran (Bugbot). The two refusals must read differently.
withheld = att("old/thing", TIP, [], first_commit_author="",
               first_commit_problem="the remote's default branch could not be confirmed")
eq("a withheld commit signal refuses", withheld.signal, "unattributable")
if "was not measured" in withheld.why and "could not be confirmed" in withheld.why:
    ok("a withheld commit signal is reported as unmeasured, with the reason")
else:
    bad(f"a withheld commit signal was reported as a finding: {withheld.why!r}")
if "no commit on this branch" not in withheld.why:
    ok("the withheld refusal does not also claim the branch has no unique commits")
else:
    bad(f"the withheld refusal asserts an unmade finding: {withheld.why!r}")

# ... and with the signal actually measured and genuinely empty, the other sentence
# is the right one. Same inputs but no problem, so the two arms are pinned apart.
measured = att("old/thing", TIP, [], first_commit_author="", first_commit_problem="")
if "no commit on this branch" in measured.why:
    ok("a measured-and-empty history says so, distinctly")
else:
    bad(f"the measured-empty refusal lost its own wording: {measured.why!r}")

# THE SAME DISTINCTION AT THE RULE LEVEL. A failed oldest-commit read reaches
# `attribute` as a problem, so the refusal must not claim the branch has no unique
# commits -- and the genuinely-empty case must still say exactly that.
logfail = att("old/thing", TIP, [], first_commit_author="",
              first_commit_problem="`git log origin/develop..origin/old/thing` failed")
eq("a failed history read refuses", logfail.signal, "unattributable")
if "was not measured" in logfail.why and "git log" in logfail.why:
    ok("a failed history read is reported as unmeasured, naming the command")
else:
    bad(f"a failed history read was reported as a finding: {logfail.why!r}")
if "no commit on this branch" not in logfail.why:
    ok("a failed history read does not claim the branch has no unique commits")
else:
    bad(f"a failed history read asserts an unmade finding: {logfail.why!r}")

# --------------------------------------------------------------------------
# 4. AMBIGUITY IS A REFUSAL, NOT A TIE-BREAK. Branch names get reused.
# --------------------------------------------------------------------------
reused = att("fix/typo", TIP,
             [pr(10, "waqaskhanroghani", "fix/typo", "0" * 40),
              pr(88, "aptracebloc", "fix/typo", "1" * 40)])
eq("two authors on one reused head name refuse", reused.signal, "unattributable")
for who in ("waqaskhanroghani", "aptracebloc"):
    if who in reused.why:
        ok(f"the refusal names {who} as a candidate")
    else:
        bad(f"the refusal hides candidate {who}: {reused.why!r}")

# AN ABSENT TIP IS ITS OWN SENTENCE, not "the tip has moved". A caller that could
# not read the tip -- a branch named on the command line that is not in this clone --
# must not be told the branch was pushed to since its PR.
notip = att("fix/typo", "", [pr(10, "waqaskhanroghani", "fix/typo", "0" * 40)])
eq("an absent tip still attributes from the PR", (notip.owner, notip.signal),
   ("waqaskhanroghani", "pr"))
if "no tip was supplied" in notip.why:
    ok("an absent tip is reported as absent, not as an overtaken head")
else:
    bad(f"an absent tip was reported as a moved tip: {notip.why!r}")

overtook = att("fix/typo", TIP, [pr(10, "waqaskhanroghani", "fix/typo", "0" * 40)])
if "moved past" in overtook.why:
    ok("a genuinely overtaken head says so")
else:
    bad(f"an overtaken head is not reported as such: {overtook.why!r}")

# ... and the SAME distinction on the multi-author arm. "None is at the current tip"
# is a comparison, so with no tip it is a claim about nothing (Bugbot).
notip_reuse = att("fix/typo", "", [pr(10, "waqaskhanroghani", "fix/typo", "0" * 40),
                                   pr(88, "aptracebloc", "fix/typo", "1" * 40)])
eq("an absent tip with two authors still refuses", notip_reuse.signal, "unattributable")
if "no tip was supplied" in notip_reuse.why:
    ok("the reuse refusal does not claim a tip comparison it never made")
else:
    bad(f"the reuse refusal claims an unmade comparison: {notip_reuse.why!r}")

# NOT "refuse anything with two PRs": one person, two PRs, is still that person.
twice = att("fix/typo", TIP,
            [pr(10, "waqaskhanroghani", "fix/typo", "0" * 40),
             pr(88, "waqaskhanroghani", "fix/typo", "1" * 40)])
eq("two PRs by the SAME person still attribute", (twice.owner, twice.signal),
   ("waqaskhanroghani", "pr"))
if "#10" in twice.why and "#88" in twice.why:
    ok("both PRs are cited as the evidence")
else:
    bad(f"the evidence does not cite both PRs: {twice.why!r}")

# An exact-tip match by two different people refuses too -- a DIFFERENT arm from
# the name-reuse one above, and it used to be reachable only through it.
split_tip = att("fix/typo", TIP,
                [pr(10, "waqaskhanroghani", "fix/typo", TIP),
                 pr(88, "aptracebloc", "fix/typo", TIP)])
eq("two different authors at the exact tip refuse", split_tip.signal, "unattributable")
if "exact tip" in split_tip.why:
    ok("the exact-tip refusal says it is the exact-tip case")
else:
    bad(f"the exact-tip refusal is indistinguishable: {split_tip.why!r}")

# THE EXACT MATCH WINS over an older PR for the same name by someone else. This is
# the one place a second author is present and the answer is still not a refusal.
overtaken = att("fix/typo", TIP,
                [pr(10, "waqaskhanroghani", "fix/typo", "0" * 40),
                 pr(88, "aptracebloc", "fix/typo", TIP)])
eq("the PR at the exact tip decides it", (overtaken.owner, overtaken.signal),
   ("aptracebloc", "pr-exact"))

# --------------------------------------------------------------------------
# 5. A DELETED ACCOUNT IS NOT AN ABSENCE. `author: null` must not fall through
#    to the commit author -- the PR exists, so signal 2 does not apply.
# --------------------------------------------------------------------------
#    The two arms are asserted APART. Both refuse and both say "deleted account",
#    so a test that checked only that would pass with the exact-tip arm deleted --
#    control would fall through to the name-reuse arm and refuse for the wrong
#    reason. A refusal test that cannot say WHICH refusal is a coin toss.
for oid, label, phrase in ((TIP, "at the exact tip", "at this exact tip"),
                           ("0" * 40, "behind the tip", "for this head")):
    gone = att("old/thing", TIP, [pr(7, None, "old/thing", oid)],
               first_commit_author="Someone <someone@example.com>")
    eq(f"a PR with no author {label} refuses", gone.signal, "unattributable")
    if "deleted account" in gone.why and phrase in gone.why:
        ok(f"the no-author refusal {label} is the {label} arm, and says why")
    else:
        bad(f"the no-author refusal {label} did not come from its own arm: {gone.why!r}")

# --------------------------------------------------------------------------
# 6. A CALLER PASSING ANOTHER BRANCH'S PRs IS REFUSED, not quietly absorbed --
#    that is how a per-person table gets built out of the wrong rows.
# --------------------------------------------------------------------------
crossed = att("fix/mine", TIP, [pr(9, "saadqbal", "fix/theirs", TIP)])
eq("PRs for another head ref refuse", (crossed.owner, crossed.signal),
   (UNATTRIBUTABLE, "unattributable"))
if "another head ref" in crossed.why and "#9" in crossed.why:
    ok("the cross-ref refusal names the offending PR")
else:
    bad(f"the cross-ref refusal is unspecific: {crossed.why!r}")

# A row with no headRefName at all is treated as this branch's, not as a stray:
# `--json headRefName` always returns it, so absence means the caller built the
# row by hand, and refusing there would break the pure-function use.
lean = att("fix/mine", TIP, [{"number": 4, "author": {"login": "saadqbal"},
                              "headRefOid": TIP}])
eq("a hand-built row without headRefName is accepted", lean.owner, "saadqbal")

# --------------------------------------------------------------------------
# 7. INVARIANTS OVER EVERY ANSWER PRODUCED ABOVE (rule 6: the domain is the
#    producer's declared surface). Every signal in SIGNALS must be reachable,
#    no answer may carry a signal outside it, and owner/signal must agree.
# --------------------------------------------------------------------------
# WRITTEN DOWN INDEPENDENTLY OF THE MODULE'S OWN VALUE. Every other assertion
# here compares an owner against the imported UNATTRIBUTABLE, which agrees with
# itself whatever the module sets it to -- so changing it to a plausible-looking
# "unknown" passed the whole suite. What matters is that it is FALSY: a caller who
# forgets to check the signal gets nothing, never a name-shaped string.
eq("UNATTRIBUTABLE is the empty string", UNATTRIBUTABLE, "")
eq("a refusal's owner is falsy",
   [s for s in SEEN if s.signal == "unattributable" and s.owner], [])

reached = {s.signal for s in SEEN}
eq("every declared signal is exercised", sorted(reached), sorted(SIGNALS))
eq("no answer carries an undeclared signal", sorted(reached - set(SIGNALS)), [])
eq("a refusal never names an owner",
   [s for s in SEEN if s.signal == "unattributable" and s.owner != UNATTRIBUTABLE], [])
eq("an attribution always names an owner",
   [s for s in SEEN if s.signal != "unattributable" and not s.owner], [])
eq("every answer explains itself", [s for s in SEEN if not s.why.strip()], [])

# --------------------------------------------------------------------------
# 8. THE SEAMS. `pull_requests` must turn every unreadable answer into a
#    problem, and `first_commit_author` must ask for the OLDEST commit.
# --------------------------------------------------------------------------
_real = _m._run
try:
    calls = []

    def stub(rc, out):
        def run(args):
            calls.append(args)
            return rc, out
        return run

    import json as _json

    REPO = "tracebloc/backend"

    def graphql_pages(rows, total=None, per_page=None):
        """A `gh api graphql --paginate --slurp` body: `rows` split into pages.

        `total` defaults to len(rows) -- a COMPLETE read. Passing a LARGER one is
        how a short read is constructed: the repository says it has N and the pages
        carry fewer, which is exactly the shape pagination stopping early leaves
        behind, and nothing about it has to be assumed about gh's internals.
        """
        per_page = PAGE_SIZE if per_page is None else per_page
        total = len(rows) if total is None else total
        chunks = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]
        return _json.dumps([
            {"data": {"repository": {"pullRequests": {
                "totalCount": total,
                "pageInfo": {"hasNextPage": i < len(chunks) - 1,
                             "endCursor": f"cursor{i}"},
                "nodes": chunk}}}}
            for i, chunk in enumerate(chunks)])

    # THE CURSOR VARIABLE'S NAME IS THE TRAP, so it is a machine check on the query
    # text rather than a comment asking the next editor to be careful. `gh
    # --paginate` injects the next cursor into `$endCursor` and no other name;
    # misname it and gh re-requests page 1 forever, and the secondary rate limit
    # that loop trips does not show up in `gh api rate_limit` -- so the failure does
    # not even look like a failure. Asserted on BOTH halves: the declaration and
    # the use, because renaming either one alone is enough to break it.
    eq("the paged query declares the cursor variable gh will inject",
       "$endCursor: String" in PR_QUERY, True)
    eq("the paged query actually pages on that variable",
       "after: $endCursor" in PR_QUERY, True)
    eq("no other cursor variable name is used",
       [w for w in PR_QUERY.split() if w.startswith("$cursor")], [])
    eq("the page size asked for is GitHub's connection maximum", PAGE_SIZE, 100)
    eq("the query asks the repository for its own count, to check the read against",
       "totalCount" in PR_QUERY, True)

    # ... and the read must actually ASK to be paged and slurped, or the query above
    # returns exactly one page and every completeness check below is vacuous.
    calls.clear()
    _m._run = stub(0, graphql_pages([]))
    _m.pull_requests(REPO)
    args = calls[-1]
    eq("the read pages to the end", "--paginate" in args, True)
    eq("the pages come back as one document", "--slurp" in args, True)
    eq("the repo is passed as the query's two halves",
       [a for a in args if a.startswith(("owner=", "name="))],
       ["owner=tracebloc", "name=backend"])
    # `-f` NOT `-F`: `-F` types its value, and a numeric repo name then arrives as
    # an int against a `String!` variable. Measured 2026-09-01: `-F name=123` is
    # refused with "Could not coerce value 123 to String", `-f name=123` is not.
    eq("the variables are sent as strings, so a numeric repo name still resolves",
       [a for a in args if a == "-F"], [])

    _m._run = stub(1, "")
    heads, problem = _m.pull_requests(REPO)
    eq("a failed gh call yields no heads", heads, {})
    if problem and "proves nothing" in problem:
        ok("a failed gh call yields a problem that explains itself")
    else:
        bad(f"a failed gh call did not fail closed: {problem!r}")

    _m._run = stub(0, "not json at all")
    heads, problem = _m.pull_requests(REPO)
    if problem and "JSON" in problem:
        ok("an unparseable list is a problem, not an empty result")
    else:
        bad(f"an unparseable list was accepted: {problem!r}")

    # `--slurp` wraps the pages in ONE array, so a bare object is a shape problem
    # and not "a single page".
    _m._run = stub(0, '{"headRefName": "x"}')
    heads, problem = _m.pull_requests(REPO)
    if problem and "not a list" in problem:
        ok("a body that is not a list of pages is a problem")
    else:
        bad(f"a non-list was accepted: {problem!r}")

    # A GRAPHQL ERROR IS AN HTTP 200. Reading only the exit code turns a bad field,
    # a missing repo and a permissions failure alike into "no pull requests".
    _m._run = stub(0, _json.dumps([{"data": None, "errors": [
        {"message": "Could not resolve to a Repository with the name 'x/y'."}]}]))
    heads, problem = _m.pull_requests(REPO)
    eq("an errors[] payload at exit 0 yields no heads", heads, {})
    if problem and "Could not resolve" in problem:
        ok("an errors[] payload at exit 0 is a refusal that quotes the error")
    else:
        bad(f"a GraphQL error at exit 0 was accepted: {problem!r}")

    # --- THE REGRESSION THIS TICKET IS FOR ---------------------------------
    #
    # A repo with MORE PULL REQUESTS THAN ONE PAGE STILL ATTRIBUTES. Under the old
    # `--limit 1000` the largest repo in the org refused outright -- 108 branches,
    # 0 attributed -- so "the read spans pages and the answer still comes back" is
    # the case that has to hold, not merely "a big read is refused politely".
    # PAGE_SIZE * 2 + 50 rows, so the last page is a PARTIAL one: an off-by-one in
    # the paging would land exactly here.
    many = [pr(i, "saqlainsyed007", f"b{i}", f"{i:040d}")
            for i in range(PAGE_SIZE * 2 + 50)]
    _m._run = stub(0, graphql_pages(many))
    heads, problem = _m.pull_requests(REPO)
    eq(f"a {len(many)}-pull-request repo is read across pages without refusing",
       problem, "")
    eq("every row from every page arrives", sum(len(v) for v in heads.values()),
       len(many))
    eq("rows are grouped by head ref", len(heads), len(many))
    # ... and the rows still carry what the RULE reads, so paging did not change
    # the shape `attribute` was written against.
    last = heads[f"b{len(many) - 1}"][0]
    got = att(last["headRefName"], last["headRefOid"], [last])
    eq("a branch from the last page attributes from its PR author",
       (got.owner, got.signal), ("saqlainsyed007", "pr-exact"))

    # --- AND THE BACKSTOP STILL REFUSES ------------------------------------
    #
    # Pagination CAN stop early, and then the list is short. Derived from the
    # repository's own totalCount, so this is two measured numbers disagreeing
    # rather than a ceiling somebody picked -- and it is a MARKED refusal, because
    # `main` exits non-zero on this and on nothing else.
    short = graphql_pages(many[:PAGE_SIZE], total=len(many))
    _m._run = stub(0, short)
    heads, problem = _m.pull_requests(REPO)
    eq("a short read yields no heads at all", heads, {})
    eq("a short read is a marked refusal", problem.startswith(INCOMPLETE_PR_LIST),
       True)
    eq("the refusal names the repo it is about", REPO in problem, True)
    for what, number in (("what the repo says it has", len(many)),
                         ("what the read returned", PAGE_SIZE),
                         ("the shortfall", len(many) - PAGE_SIZE)):
        eq(f"the refusal names {what}", str(number) in problem, True)
    if "not a" in problem and "unattributable" in problem:
        ok("the refusal says outright it is not an 'unattributable' finding")
    else:
        bad(f"the refusal does not distinguish itself: {problem!r}")

    # A PAGE THAT CAME BACK TWICE is what a broken cursor looks like on the near
    # side of the infinite loop, and it would otherwise pass the count check by
    # accident once the duplicates make the totals line up.
    dupes = many[:PAGE_SIZE] + many[:PAGE_SIZE]
    _m._run = stub(0, graphql_pages(dupes, total=len(dupes)))
    heads, problem = _m.pull_requests(REPO)
    eq("a duplicated page is a marked refusal",
       (heads, problem.startswith(INCOMPLETE_PR_LIST)), ({}, True))
    if "distinct" in problem and "endCursor" in problem:
        ok("the duplicate refusal names the cursor as the likely cause")
    else:
        bad(f"a duplicated page was not diagnosed: {problem!r}")

    # A COUNT THAT DISAGREES WITH ITSELF between pages is a repo that changed
    # mid-read: there is then no single number to check against, and "cannot tell"
    # is a refusal rather than a pass.
    pages = _json.loads(graphql_pages(many))
    pages[-1]["data"]["repository"]["pullRequests"]["totalCount"] = len(many) + 1
    _m._run = stub(0, _json.dumps(pages))
    heads, problem = _m.pull_requests(REPO)
    eq("a totalCount that disagrees across pages is a marked refusal",
       (heads, problem.startswith(INCOMPLETE_PR_LIST)), ({}, True))

    # ... and so is a count that never came back at all, which would otherwise
    # compare a list against None and take the mismatch for a short read.
    pages = _json.loads(graphql_pages(many))
    for page in pages:
        del page["data"]["repository"]["pullRequests"]["totalCount"]
    _m._run = stub(0, _json.dumps(pages))
    eq("a missing totalCount is a marked refusal",
       _m.pull_requests(REPO)[1].startswith(INCOMPLETE_PR_LIST), True)

    # --- THE MARKER MUST BE EXCLUSIVE --------------------------------------
    #
    # `main` exits non-zero on the marked refusal and on no other problem, so a
    # bare non-zero would leave a cap-hit and a dead network indistinguishable --
    # which is where this ticket started. That is a claim about the seam's WHOLE
    # problem domain, so it is asserted against every other problem it can produce
    # rather than about the one string above.
    for label, answer in (("a failed gh call", (1, "")),
                          ("a missing gh", (127, "gh: not found")),
                          ("an unparseable body", (0, "not json at all")),
                          ("a body that is not a list", (0, '{"a": 1}')),
                          ("a page with no pullRequests", (0, '[{"data": {}}]')),
                          ("an errors[] payload",
                           (0, '[{"errors": [{"message": "nope"}]}]'))):
        _m._run = stub(*answer)
        _, other = _m.pull_requests(REPO)
        eq(f"{label} is a problem, but NOT the incomplete-list refusal",
           (bool(other), other.startswith(INCOMPLETE_PR_LIST)), (True, False))

    _m._run = stub(0, graphql_pages([pr(1, "a", "same", "0" * 40),
                                     pr(2, "b", "same", "1" * 40)]))
    heads, problem = _m.pull_requests(REPO)
    eq("two PRs on one head land in one group", len(heads.get("same", [])), 2)

    # --- WHICH REPO IS BEING READ -----------------------------------------
    #
    # GraphQL has no `{owner}/{repo}` placeholder, so the resolution `gh pr list`
    # did implicitly now happens in the open -- and can fail, which is a refusal.
    eq("owner/name is split for the query", _m.repo_identity("tracebloc/client"),
       ("tracebloc", "client", ""))
    for bad_repo in ("tracebloc", "/client", "tracebloc/", "a/b/c", ""):
        if bad_repo == "":
            continue
        owner, name, why = _m.repo_identity(bad_repo)
        eq(f"{bad_repo!r} is not a repository and is refused, not guessed at",
           (owner, name, bool(why)), ("", "", True))
    # ... and with nothing passed, the clone is asked -- by a command that has no
    # `--repo` flag to get wrong (see `default_branch`).
    calls.clear()
    _m._run = stub(0, "tracebloc/.github")
    eq("a bare clone resolves itself", _m.repo_identity(),
       ("tracebloc", ".github", ""))
    eq("the clone is asked for nameWithOwner", "nameWithOwner" in calls[-1], True)
    eq("no --repo flag is handed to `gh repo view`", "--repo" in calls[-1], False)
    _m._run = stub(1, "")
    owner, name, why = _m.repo_identity()
    eq("a clone that cannot be identified is a refusal", (owner, name), ("", ""))
    if why and "proves nothing" in why:
        ok("an unidentifiable clone fails closed rather than reading some other repo")
    else:
        bad(f"an unidentifiable clone did not fail closed: {why!r}")

    # THE OLDEST COMMIT, NOT THE TIP. git prints oldest-first under --reverse, so
    # the first line is the one to take -- and the request must say --reverse and
    # must NOT cap the count, or git limits before reversing and returns the tip.
    calls.clear()
    _m._run = stub(0, "First Person <first@example.com>\nLater Pusher <later@example.com>")
    eq("the oldest commit's author is taken",
       _m.first_commit_author("origin/b", "origin/develop"),
       ("First Person <first@example.com>", ""))
    args = calls[-1]
    eq("the request is reversed", "--reverse" in args, True)
    eq("the request does not cap the count",
       [a for a in args if a in ("-1", "-n") or a.startswith("--max-count")], [])
    eq("the request is a range against the default branch",
       "origin/develop..origin/b" in args, True)

    # THE TWO EMPTIES, PINNED APART -- asserted by WHICH one came back, not merely
    # that something empty did. Both used to return a bare "", so `attribute`
    # reported "no commit not already on the default branch" for a git log that
    # never ran (Saqlain's finding, generalised to this seam by audit).
    _m._run = stub(1, "")
    author, why = _m.first_commit_author("origin/b", "origin/develop")
    eq("a FAILED history read yields no author", author, "")
    if why and "could not be read" in why and "git log" in why:
        ok("a FAILED history read names the failed command as the reason")
    else:
        bad(f"a failed history read looked like an empty branch: {why!r}")

    _m._run = stub(0, "")
    eq("a branch with GENUINELY no unique commits yields no author and no problem",
       _m.first_commit_author("origin/b", "origin/develop"), ("", ""))

    # --- the ref list -----------------------------------------------------
    #
    # `refs/remotes/origin/HEAD` shortens to bare `origin`, which is why the
    # 2026-08-14 inventory carried one branch called `origin` per repo. A sweep
    # built on that list tries to delete a branch named after the remote.
    _m._run = stub(0, "origin\tdeadbeef\n"
                      "origin/HEAD\tcafe1234\n"
                      "origin/develop\t11111111\n"
                      "origin/main\t22222222\n"
                      "origin/fix/1-a\t33333333\n"
                      "origin/feat/2-b\t44444444")
    got, refs_problem = _m.remote_branches("origin/develop")
    eq("a good read reports no problem", refs_problem, "")
    eq("origin/HEAD's bare short name is not a branch",
       [n for n, _ in got if "/" not in n], [])
    eq("only real feature branches are listed", sorted(n for n, _ in got),
       ["feat/2-b", "fix/1-a"])
    eq("the tip sha comes back with the name", dict(got)["fix/1-a"], "33333333")

    # A non-default integration branch is excluded too, and so is the repo's own
    # default when it is not `develop` -- derived from the argument, not listed.
    _m._run = stub(0, "origin/master\t1\norigin/release/9\t2")
    eq("the passed-in default is excluded by name",
       sorted(n for n, _ in _m.remote_branches("origin/master")[0]), ["release/9"])

    # A FAILED READ THAT PRINTED SOMETHING is the case worth pinning: rc=1 with an
    # empty stdout yields [] whether or not the code checks rc, so asserting only
    # that would leave the guard untested. A partial list is what must not become
    # a sweep list.
    #
    # AND IT MUST NOT LOOK LIKE AN EMPTY CLONE. Returning a bare [] made `main`
    # print "0 branch(es)" and exit 0 -- a clean report from a read that failed,
    # while the other two seams refused explicitly (Bugbot). The problem string is
    # what separates "could not read" from "nothing there".
    _m._run = stub(1, "origin/fix/partial\tabc123")
    refs, refs_problem = _m.remote_branches("origin/develop")
    eq("a failed ref read yields nothing rather than a partial sweep", refs, [])
    if refs_problem and "could not be read" in refs_problem:
        ok("a failed ref read is a problem, not an empty inventory")
    else:
        bad(f"a failed ref read looked like an empty clone: {refs_problem!r}")

    # A genuinely empty remote is NOT a problem -- the two must not collapse, or
    # the refusal above becomes "refuse whenever there are no branches".
    _m._run = stub(0, "")
    refs, refs_problem = _m.remote_branches("origin/develop")
    eq("a genuinely empty remote yields no branches and no problem",
       (refs, refs_problem), ([], ""))

    # --- the default branch, which signal 2 is measured against -----------
    #
    # `origin/HEAD` was stale on 9 of this org's 19 clones. It is a cache, so it
    # may be READ but never TRUSTED: an answer that comes from it carries a
    # problem string, and the caller withholds the first-commit signal.
    calls.clear()
    _m._run = stub(0, "develop")
    eq("the remote's answer is authoritative and clean",
       _m.default_branch(), ("origin/develop", ""))
    # WHICH QUESTION WAS ASKED, not just what came back: a stub answers whatever
    # it is handed, so without this the remote query could be replaced by any
    # command at all and every case above would still pass.
    eq("the authority asked is the remote, via gh",
       ("gh" in calls[0] and "defaultBranchRef" in calls[0]), True)
    # ... and the name it gives is checked against THIS clone, because gh can name
    # a branch that was never fetched here (Bugbot).
    eq("the named ref is verified to exist locally",
       any("rev-parse" in c and "origin/develop" in c for c in calls), True)

    # --- WITH A REPO NAMED, THE REMOTE MUST ACTUALLY BE ASKED --------------
    #
    # `gh repo view` HAS NO `--repo` FLAG. It takes the repository positionally
    # (`gh repo view [<repository>] [flags]`) and the flag form exits 1 with
    # `unknown flag: --repo` -- measured 2026-09-01. This seam built the flag form,
    # so on EVERY `--repo` run the authoritative lookup failed unasked, the answer
    # fell through to `origin/HEAD` (a clone-time cache), the first-commit signal
    # was withheld for every branch, and the message blamed a remote that was never
    # queried. On `tracebloc/backend` that alone cost 14 of 108 attributions
    # (backend#2972).
    #
    # ASSERTING THE ARGUMENT SHAPE IS THE WEAK TEST, and its weakness is why this
    # survived: a permissive stub answers a malformed command as happily as a good
    # one, so a case named "--repo reaches the default-branch query" passed
    # throughout. This stub instead MODELS gh's real accepted surface -- it refuses
    # an unknown flag the way gh does -- and the assertion is that the problem
    # string comes back EMPTY, which only a command gh would accept can achieve.
    def gh_like(answer):
        def run(args):
            calls.append(args)
            if args[:3] == ["gh", "repo", "view"] and "--repo" in args:
                return 1, ""          # exactly what gh does: unknown flag
            return answer
        return run

    calls.clear()
    _m._run = gh_like((0, "develop"))
    eq("a named repo reaches the AUTHORITATIVE default-branch lookup",
       _m.default_branch("tracebloc/client"), ("origin/develop", ""))
    eq("the repo is named to the command in the form that command accepts",
       "tracebloc/client" in calls[0], True)

    # ... and the same surface, applied to the other seam that calls `gh repo view`.
    calls.clear()
    _m._run = gh_like((0, "tracebloc/client"))
    eq("resolving a bare clone reaches the authoritative lookup too",
       _m.repo_identity(), ("tracebloc", "client", ""))
    seq2 = [(0, "develop"), (1, "")]          # gh answers; the local ref is absent
    _m._run = lambda args: seq2.pop(0)
    ref, why = _m.default_branch()
    eq("an unfetched default is still returned", ref, "origin/develop")
    if why and "no origin/develop" in why and "fetch" in why:
        ok("an unfetched default is one reported fact, not N failed ranges")
    else:
        bad(f"an unfetched default was reported as trustworthy: {why!r}")

    seq = [(1, ""), (0, "origin/main")]
    _m._run = lambda args: seq.pop(0)
    ref, why = _m.default_branch()
    eq("a cached origin/HEAD is still returned", ref, "origin/main")
    if why and "cache" in why:
        ok("a cached origin/HEAD is returned WITH a problem, not silently")
    else:
        bad(f"a cached origin/HEAD was returned as trustworthy: {why!r}")

    seq = [(1, ""), (1, ""), (0, "")]
    _m._run = lambda args: seq.pop(0)
    ref, why = _m.default_branch()
    eq("an unset origin/HEAD falls back to a guess", ref, "origin/develop")
    if why and "guess" in why:
        ok("the fallback says it is a guess")
    else:
        bad(f"the fallback did not admit to guessing: {why!r}")

    _m._run = stub(1, "")
    ref, why = _m.default_branch()
    eq("nothing found means nothing returned", ref, "")
    eq("nothing found is a problem", bool(why), True)

    # --- A MISSING BINARY IS AN EXIT CODE, NOT A TRACEBACK ----------------
    #
    # This is the machine most likely to lack gh, and the documented behaviour is
    # a refusal. `_run` is exercised for real here (no stub) so the OSError path
    # is the actual one.
    _m._run = _real
    rc, out = _m._run(["definitely-not-a-real-binary-2365"])
    eq("a missing binary returns a non-zero code", rc != 0, True)
    eq("a missing binary names itself rather than raising",
       "definitely-not-a-real-binary-2365" in out, True)
    _m._run = lambda args: (127, "gh: not found")
    heads, problem = _m.pull_requests()
    eq("a missing gh yields no heads", heads, {})
    if problem and "proves nothing" in problem:
        ok("a missing gh fails closed with the same refusal as a failed call")
    else:
        bad(f"a missing gh did not fail closed: {problem!r}")
    # --- main(), because the SEAM-TO-RULE WIRING is where the fix pays off ----
    #
    # Every case above tests `attribute` or a seam in isolation. `main` is what
    # carries a seam's problem string into the rule, and dropping that one
    # argument reverts the whole fix while every isolated case stays green -- which
    # is exactly what a mutation run showed. So this drives the real entry point.
    def run_main(answers, argv=()):
        """(exit code, stdout, stderr) with `_run` answering per command.

        STDERR IS RETURNED, NOT DISCARDED: every refusal this entry point makes is
        written there, so a case reading only the exit code cannot say WHICH refusal
        it got -- and a short read and a dead network would look alike again.
        """
        def fake(args):
            joined = " ".join(args)
            for needle, reply in answers:
                if needle in joined:
                    return reply
            return 0, ""
        _m._run = fake
        buf, ebuf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(ebuf):
            code = _m.main(list(argv))
        return code, buf.getvalue(), ebuf.getvalue()

    # NEEDLES ARE MATCHED AGAINST THE JOINED COMMAND, so `gh repo view` has to be
    # disambiguated BY THE FIELD IT ASKS FOR: two seams call it now, and a bare
    # "repo view" needle answers whichever one asks first -- a fake that agrees with
    # itself instead of with the module.
    GOOD_HEAD = [("defaultBranchRef", (0, "develop")),
                 ("nameWithOwner", (0, "tracebloc/backend")),
                 ("graphql", (0, graphql_pages([]))),
                 ("for-each-ref", (0, "origin/feat/x\tabc123"))]

    code, out, _ = run_main([*GOOD_HEAD, ("git log", (128, ""))])
    eq("main exits 0 having reported the branch", code, 0)
    if "was not measured" in out and "git log" in out:
        ok("main carries a FAILED history read through to the row as unmeasured")
    else:
        bad(f"main lost the failed-history reason: {out.strip()[:160]!r}")
    if "no commit on this branch" not in out:
        ok("main does not render a failed history read as 'no unique commits'")
    else:
        bad(f"main reported an unmade finding: {out.strip()[:160]!r}")

    # ... and with the same shape but a SUCCESSFUL empty history, the other
    # sentence is the right one. Pinned apart end-to-end, not only at the seam.
    code, out, _ = run_main([*GOOD_HEAD, ("git log", (0, ""))])
    eq("main exits 0 on a genuinely empty history", code, 0)
    if "no commit on this branch" in out and "was not measured" not in out:
        ok("main renders a genuinely empty history as exactly that")
    else:
        bad(f"main confused an empty history with a failed one: {out.strip()[:160]!r}")

    # A FAILED ENUMERATION IS A NON-ZERO EXIT, not a report of zero branches --
    # Saqlain's finding, asserted at the entry point he was reading.
    code, out, _ = run_main([*GOOD_HEAD[:3], ("for-each-ref", (128, ""))])
    eq("main refuses when the branch list could not be read", code, 2)
    eq("main prints no rows when it refuses", out.strip(), "")

    # ... while a genuinely empty remote is a clean, zero-row success.
    code, out, _ = run_main([*GOOD_HEAD[:3], ("for-each-ref", (0, ""))])
    eq("main exits 0 on a genuinely empty remote", code, 0)

    # --- MORE PULL REQUESTS THAN ONE PAGE, END TO END ----------------------
    #
    # THE CASE THE TICKET WAS FILED FOR (backend#2972), driven through the entry
    # point rather than described. `tracebloc/backend`'s 1418 pull requests against
    # the old `--limit 1000` made the seam fail closed, and `main` printed
    #
    #     108 branch(es): 0 attributed, 108 unattributable
    #
    # and exited 0 -- indistinguishable from a repo whose branches genuinely cannot
    # be attributed, in the repo where 102 of the 108 do attribute. A seam that
    # pages correctly buys nothing if `main` cannot carry a multi-page answer, so
    # this asserts the ANSWER, not merely the absence of a refusal.
    spread = [pr(i, "shujaatTracebloc", f"old/{i}", f"{i:040d}")
              for i in range(PAGE_SIZE * 2 + 7)]
    spread.append(pr(9999, "waqaskhanroghani", "feat/x", "abc123"))
    code, out, err = run_main(
        [*GOOD_HEAD[:2], ("graphql", (0, graphql_pages(spread))),
         ("for-each-ref", (0, "origin/feat/x\tabc123")),
         ("git log", (0, "First Person <first@example.com>"))],
        argv=("--repo", "tracebloc/backend"))
    eq(f"main attributes across a {len(spread)}-pull-request repo", code, 0)
    eq("the owner comes from the PR author on a paged read",
       [ln for ln in out.splitlines() if ln.startswith("feat/x\t")],
       ["feat/x\twaqaskhanroghani\tpr-exact\tPR #9999 author, head oid matches the tip"])
    eq("the tally is a measurement, with nothing appended to it",
       err.strip().endswith("1 branch(es): 1 attributed, 0 unattributable"), True)

    # --- AND AN INCOMPLETE READ IS A REFUSAL, NOT A ZERO-ROW ANSWER --------
    #
    # The backstop survives the fix: pagination stopping early is now a real
    # "could not read it all" rather than the routine condition it used to be, and
    # it must not come back as rows.
    code, out, err = run_main(
        [*GOOD_HEAD[:2],
         ("graphql", (0, graphql_pages(spread[:PAGE_SIZE], total=len(spread)))),
         ("for-each-ref", (0, "origin/feat/x\tabc123"))],
        argv=("--repo", "tracebloc/backend"))
    eq("main refuses an incomplete PR list instead of reporting rows", code, 2)
    eq("main prints no rows out of a short read", out.strip(), "")
    # NOT A BARE NON-ZERO: that is also what a dead network returns, and a caller
    # who cannot tell them apart is back where this ticket started.
    eq("main's refusal is the incomplete-list one specifically",
       INCOMPLETE_PR_LIST in err, True)
    eq("main's refusal names the repo whose read fell short",
       "tracebloc/backend" in err, True)
    eq("main's refusal names both counts",
       (str(len(spread)) in err, str(PAGE_SIZE) in err), (True, True))
    # ... and prints NO tally at all, because a tally is the shape of an answer.
    eq("main prints no branch tally for a short read", "branch(es):" in err, False)

    # ... while the PR-list problems that are NOT a short read still report their
    # rows -- with the one reason they were all refused for carried into the tally,
    # since `0 attributed, N unattributable` alone is the same misreading one path
    # over. Pinned in both directions, or "refuse on a short read" degrades into
    # "refuse whenever the PR list is imperfect".
    code, out, err = run_main([*GOOD_HEAD[:2], ("graphql", (1, "")),
                               ("for-each-ref", (0, "origin/feat/x\tabc123"))],
                              argv=("--repo", "tracebloc/backend"))
    eq("a PR list that could not be read still reports its rows", code, 0)
    eq("its rows are all refused for the unread list",
       out.count("the pull-request list is not evidence"), 1)
    if "every row refused for one reason" in err and "proves nothing" in err:
        ok("the tally carries the one reason every row was refused for")
    else:
        bad(f"the tally hid the reason behind a bare count: {err.strip()[-200:]!r}")
finally:
    _m._run = _real

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
