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

FAIL CLOSED, AND SAY SO
-----------------------
"No PR was found for this branch" is only a fact when the PR list was actually
read, and completely. If `gh` is missing, unauthenticated, failing, or returned a
list that hit its own `--limit` cap, then absence proves nothing -- and falling
through to the commit-author path in that state is precisely the misattribution
this module exists to prevent, arrived at from a clean read of the wrong thing.
So a PR-list problem makes EVERY branch `unattributable`, before any other
evidence is weighed. Same posture, and the same reasoning, as `scripts/git-reap`.

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

# `gh pr list` truncates at --limit SILENTLY. A miss against a partial window is
# not an absence, and the old branches this tool is for sort out of a newest-first
# window first. Reaching the cap is therefore reported as a problem, not a result.
PR_LIMIT = 1000

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
) -> Attribution:
    """Who owns `branch`, from the PR author first and never from the tip commit.

    `prs` are the pull requests for THIS head ref, any state. `tip_sha` is the
    branch's current tip. `first_commit_author` is the git identity of the oldest
    commit on `branch` that is not on the default branch -- "" if there is none or
    it could not be read. `pr_list_problem`, if set, says why the PR list is not
    evidence of absence, and refuses every branch on the spot.

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
            return _refuse(
                f"{len(prs)} pull requests share this head name with different authors "
                f"({', '.join(sorted(by))}) and none is at the current tip"
            )
        return _refuse(
            f"the pull request(s) for this head ({_cite(prs)}) carry no author -- "
            "a deleted account cannot be asked, so this needs a human"
        )

    if first_commit_author:
        return Attribution(first_commit_author, "first-commit",
                           "no pull request; author of the oldest commit not on the "
                           "default branch")

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


def pull_requests(repo: str = "") -> "tuple[dict, str]":
    """(head ref -> [PR rows], problem). A non-empty problem refuses everything.

    One query per repo, never one per branch: a per-branch call burns the API
    rate limit on a repo with hundreds of branches.
    """
    args = ["gh", "pr", "list", "--state", "all", "--limit", str(PR_LIMIT),
            "--json", "number,author,headRefName,headRefOid,state,createdAt"]
    if repo:
        args += ["--repo", repo]
    rc, out = _run(args)
    if rc != 0 or not out:
        return {}, ("`gh pr list` failed -- no gh, no auth, or no network. "
                    "Absence from a list that was never read proves nothing")
    try:
        rows = json.loads(out)
    except json.JSONDecodeError as exc:
        return {}, f"the pull-request list did not parse as JSON ({exc})"
    if not isinstance(rows, list):
        return {}, f"the pull-request list is a {type(rows).__name__}, not a list"
    if len(rows) >= PR_LIMIT:
        return {}, (f"the pull-request list hit its --limit {PR_LIMIT} cap, so a "
                    "branch missing from it may simply be past the window")
    by_head: dict = {}
    for row in rows:
        by_head.setdefault(row.get("headRefName") or "", []).append(row)
    return by_head, ""


def first_commit_author(ref: str, default: str) -> str:
    """git identity of the oldest commit on `ref` that is not on `default`.

    NOT the tip, and the difference is the whole point: on the branch that
    prompted this ticket (`client fix/583-wire-ca-proxy`) the oldest commit is
    Shujaat's and the tip is Lukas's.

    `--reverse` with `-n 1` would still give the tip -- git applies the limit
    before reversing -- so the whole list is read and the first line taken.
    """
    rc, out = _run(["git", "log", "--reverse", "--format=%an <%ae>",
                    f"{default}..{ref}"])
    if rc != 0 or not out:
        return ""
    return out.splitlines()[0].strip()


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
    args = ["gh", "repo", "view", "--json", "defaultBranchRef",
            "--jq", ".defaultBranchRef.name"]
    if repo:
        args += ["--repo", repo]
    rc, out = _run(args)
    if rc == 0 and out:
        return f"origin/{out}", ""

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


def remote_branches(default: str) -> "list[tuple[str, str]]":
    """(short name, tip sha) for every origin/* branch except integration ones."""
    protected = {"develop", "staging", "main", "master", "gh-pages", "HEAD",
                 default.split("/", 1)[-1]}
    rc, out = _run(["git", "for-each-ref", "--format=%(refname:short)%09%(objectname)",
                    "refs/remotes/origin"])
    if rc != 0:
        return []
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
    return found


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

    known = dict(remote_branches(default))
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
        usable = not problem and not default_problem
        att = attribute(name, sha, prs.get(name, []),
                        first_commit_author(f"origin/{name}", default) if usable else "",
                        problem)
        if att.signal == "unattributable" and default_problem and not problem:
            att = att._replace(why=f"{att.why}; also, {default_problem}")
        out.append({"branch": name, "owner": att.owner, "signal": att.signal,
                    "why": att.why})

    if args.as_json:
        print(json.dumps(out, indent=1))
    else:
        print("branch\towner\tsignal\twhy")
        for r in out:
            print(f"{r['branch']}\t{r['owner']}\t{r['signal']}\t{r['why']}")

    unattributed = sum(1 for r in out if r["signal"] == "unattributable")
    print(f"\n{len(out)} branch(es): {len(out) - unattributed} attributed, "
          f"{unattributed} unattributable", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
