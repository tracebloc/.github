#!/usr/bin/env python3
"""Cases for the one branch-ownership rule (backend#2365).

THE CASE THIS FILE EXISTS FOR is the first one below, and it is not synthetic:
`client fix/bugbot-tier0-helm-prereqs` (PR #395) and `client fix/583-wire-ca-proxy`
(PR #592) are @shujaatTracebloc's branches with Lukas's fixup commit on the tip.
A tip-author list claimed both, and the next command in that workflow deletes.

No network, no git, no gh: the rule is pure and the two seams are stubbed.
"""
import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from branch_owner import (  # noqa: E402
    PR_LIMIT,
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

    _m._run = stub(1, "")
    heads, problem = _m.pull_requests()
    eq("a failed gh call yields no heads", heads, {})
    if problem and "proves nothing" in problem:
        ok("a failed gh call yields a problem that explains itself")
    else:
        bad(f"a failed gh call did not fail closed: {problem!r}")

    _m._run = stub(0, "not json at all")
    heads, problem = _m.pull_requests()
    if problem and "JSON" in problem:
        ok("an unparseable list is a problem, not an empty result")
    else:
        bad(f"an unparseable list was accepted: {problem!r}")

    _m._run = stub(0, '{"headRefName": "x"}')
    heads, problem = _m.pull_requests()
    if problem and "not a list" in problem:
        ok("a list that is not a list is a problem")
    else:
        bad(f"a non-list was accepted: {problem!r}")

    # THE SILENT-TRUNCATION CASE. Exactly PR_LIMIT rows means the window may be
    # partial, so absence from it proves nothing -- derived from PR_LIMIT, not
    # from a hand-typed number.
    import json as _json
    capped = _json.dumps([pr(i, "x", f"b{i}", "0" * 40) for i in range(PR_LIMIT)])
    _m._run = stub(0, capped)
    heads, problem = _m.pull_requests()
    if problem and str(PR_LIMIT) in problem:
        ok(f"a list of exactly {PR_LIMIT} rows is reported as possibly truncated")
    else:
        bad(f"the --limit cap was not detected: {problem!r}")

    under = _json.dumps([pr(i, "x", f"b{i}", "0" * 40) for i in range(PR_LIMIT - 1)])
    _m._run = stub(0, under)
    heads, problem = _m.pull_requests()
    eq(f"a list of {PR_LIMIT - 1} rows is not truncated", problem, "")
    eq("rows are grouped by head ref", len(heads), PR_LIMIT - 1)

    _m._run = stub(0, _json.dumps([pr(1, "a", "same", "0" * 40),
                                   pr(2, "b", "same", "1" * 40)]))
    heads, problem = _m.pull_requests()
    eq("two PRs on one head land in one group", len(heads.get("same", [])), 2)

    # `--repo` is only added when asked for, so the tool works in a bare clone.
    calls.clear()
    _m._run = stub(0, "[]")
    _m.pull_requests("tracebloc/client")
    eq("--repo is passed through", "tracebloc/client" in calls[-1], True)
    calls.clear()
    _m.pull_requests()
    eq("--repo is absent by default", "--repo" in calls[-1], False)

    # THE OLDEST COMMIT, NOT THE TIP. git prints oldest-first under --reverse, so
    # the first line is the one to take -- and the request must say --reverse and
    # must NOT cap the count, or git limits before reversing and returns the tip.
    calls.clear()
    _m._run = stub(0, "First Person <first@example.com>\nLater Pusher <later@example.com>")
    eq("the oldest commit's author is taken",
       _m.first_commit_author("origin/b", "origin/develop"),
       "First Person <first@example.com>")
    args = calls[-1]
    eq("the request is reversed", "--reverse" in args, True)
    eq("the request does not cap the count",
       [a for a in args if a in ("-1", "-n") or a.startswith("--max-count")], [])
    eq("the request is a range against the default branch",
       "origin/develop..origin/b" in args, True)

    _m._run = stub(1, "")
    eq("an unreadable history yields no author, not a guess",
       _m.first_commit_author("origin/b", "origin/develop"), "")
    _m._run = stub(0, "")
    eq("a branch with no unique commits yields no author",
       _m.first_commit_author("origin/b", "origin/develop"), "")

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
    calls.clear()
    _m.default_branch("tracebloc/client")
    eq("--repo reaches the default-branch query", "tracebloc/client" in calls[0], True)

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
finally:
    _m._run = _real

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
