#!/usr/bin/env python3
"""Make Cursor Bugbot's review a gate instead of advice (tracebloc/backend#2284).

WHAT WAS MEASURED FIRST, because the ticket offered a cheaper option and it had
to be ruled out rather than skipped.

  1. `Cursor Bugbot` is in NO required-status-context list anywhere in the org.
     Read 2026-08-22 from `branches/{b}/protection` AND from every repo's
     rulesets (rulesets are invisible to the protection endpoint, which returns
     404 "Branch not protected" for a ruleset-only branch) across
     client / backend / cli / release-train / frontend-app / tracebloc-engine /
     docs / design-system / .github, on develop + staging + prod. Zero hits.
     So its state is advisory everywhere -- CLAUDE.md rule 2, verbatim.

  2. The check name does NOT vary: exactly `Cursor Bugbot`, produced by the
     GitHub App with slug `cursor`, in all 16 train repos.

  3. Bugbot's conclusion vocabulary is TWO values, and `failure` is not one of
     them. Over 25 per-commit runs on client#779/#782/#787/#789 plus the merged
     heads of 48 PRs across 8 repos:

         conclusion=success  ->  0 findings, every time (9/9 sampled per-commit)
         conclusion=neutral  ->  findings, usually (13/16 sampled per-commit)

     `neutral` is also what it reports having found nothing in 3 of 16 cases, so
     neutral is not even a reliable findings signal in the direction that would
     help.

WHY OPTION 1 ("just add `Cursor Bugbot` to the required contexts") IS WORTHLESS.
It is worthless on BOTH horns of a dichotomy, which is why measuring 3 above was
worth doing before writing any code:

  * If `neutral` satisfies a required context -- GitHub's documented behaviour --
    then requiring it gates nothing, because `failure` never occurs. The org has
    no counter-example either way: a scan of the last 25 merged PRs per repo on
    seven repos found ZERO cases of any required context ever concluding
    `neutral` or `skipped`, so this half could not be settled from org history
    and rests on the documented semantics.

  * If `neutral` does NOT satisfy it, requiring the context permanently BRICKS
    every PR that ever received a finding. Bugbot re-runs only on a new push or
    an explicit `bugbot run`, so resolving a finding -- the org's own sanctioned
    disposition for one it will not fix ("file the finding, link the ticket on
    the thread, resolve, ship", release-train/CLAUDE.md) -- can never turn the
    check green again. Two prod promotions merged on 2026-08-21 in exactly that
    state: client#786 and frontend-app#863, both `staging -> main`, both
    `Cursor Bugbot = neutral` ON THE MERGED HEAD, each carrying one resolved
    Medium. Promotion PRs may not be pushed to by policy, so under option 1
    those two had no route to green at all.

Either horn is a bad outcome, so option 1 is not taken. That negative result is
the reason this file exists.

WHAT THIS GATE ACTUALLY CLAIMS, and it is deliberately narrow.

  (A) THE LOAD-BEARING CLAIM: a TERMINAL Bugbot check run must exist on the PR's
      CURRENT head sha. Nothing requires that today, and its absence is the
      ticket's quiet failure mode stated precisely -- "the PR merges with a High
      that nobody re-checked after the last push" IS Bugbot not having reviewed
      the last push. An absence never approves (release-train/CLAUDE.md).

      This is a RATCHET, not a new backlog: measured on the merged heads of 48
      PRs across 8 repos, 48/48 already had a completed `Cursor Bugbot` run on
      the exact merged head. So it is green today by measurement -- arm while
      green -- and what it stops is the case that has not happened yet.

  (B) The severity of every finding on the head is parsed and REPORTED, and an
      open finding at or above the threshold fails the gate.

WHAT (B) DOES *NOT* ADD, said here so nobody later "fixes" the omission.
`required_conversation_resolution` is `true` on every branch measured
(client / backend / cli / docs / design-system / frontend-app develop, plus
client and backend staging + main). That ALREADY blocks a merge on ANY open
thread, including a Low. So (B) is not stricter than protection for open
threads and must not be sold as if it were. What it buys is the NAME: GitHub
refuses such a merge "naming neither cause", which release-train/CLAUDE.md
records as a real diagnosis cost. A named check that says "High finding open"
is the whole of (B)'s value, and it is honest to say so.

FAIL CLOSED, AND "CANNOT TELL" IS A FINDING (CLAUDE.md rule 3). Every one of
these EXITS NONZERO rather than reporting clean:

  * the GraphQL read failing, or returning no pull request
  * a truncated rollup -- `contexts(first: 100)` says nothing when a head has
    more, the same silent-truncation shape `bricked-prs.py` names, and it fails
    in the direction that matters here: a lost context makes Bugbot look ABSENT
    on a head it reviewed. `>=` the cap, not `>`, because at exactly 100 there
    is no way to tell 100 from 140
  * a truncated reviewThreads page, same reasoning
  * a Bugbot finding whose severity token is not in the declared rank. The
    observed vocabulary is High and Medium (29 findings sampled); Bugbot may
    emit others, and an unrecognised token must not silently rank as harmless
  * no terminal Bugbot run on the head once the wait budget is spent

THE WAIT, AND WHY IT IS A WAIT. Claim (A) is unsatisfiable the instant a push
lands, because Bugbot has not started yet -- so the gate polls. Measured
started_at -> completed_at over 40 Bugbot runs: min 9s, p50 164s, p90 332s,
max 635s. The default budget is 900s, ~1.4x the observed max. The wait runs
CONCURRENTLY with Bugbot's own work (both begin at the push), so the gate's
wall-clock is Bugbot's latency and not an addition to it.

WHAT IS MATCHED, AND ON WHAT. Never on a display name where a machine-readable
marker exists:

  * the check run  -> the producing App's slug (`cursor`), not the string
    "Cursor Bugbot". A rename of the check does not blind this gate.
  * a finding      -> Bugbot's own `<!-- BUGBOT_BUG_ID: ... -->` marker in the
    thread's first comment. That is how Bugbot itself distinguishes a finding
    from any other comment it makes, so this cannot drift from what a finding is.
  * the severity   -> Bugbot's own `**<Level> Severity**` line. The RANK ORDER
    below is the one thing that must be declared rather than derived -- there is
    nowhere to read it from -- which is exactly why an unknown token is a
    failure instead of a default.
"""
import json
import os
import re
import subprocess
import sys
import time

# The App that produces the check run. Measured: `checkSuite.app.slug == "cursor"`
# on all 16 train repos. Matching the producer rather than the check's display
# name is what makes a Bugbot rename harmless instead of silently fail-open.
BUGBOT_APP_SLUG = "cursor"

# The bot that authors finding threads. Measured: `author.login == "cursor"`,
# `__typename == "Bot"`.
BUGBOT_LOGIN = "cursor"

# Bugbot's own machine-readable "this comment is a finding" marker.
FINDING_MARKER = "BUGBOT_BUG_ID"

# Bugbot's own severity line.
SEVERITY_RE = re.compile(r"\*\*([A-Za-z]+)\s+Severity\*\*")

# THE ONE DECLARED THING. Ascending. Anything Bugbot emits that is not in here
# is a failure, not a low-ranking default -- see the fail-closed list above.
SEVERITY_RANK = ["low", "medium", "high", "critical"]

# `contexts(first: 100)` / `reviewThreads(first: 100)` are what the query below
# asks for, and GraphQL says nothing when there is more. Same constant and same
# `>=` treatment as bricked-prs.py's ROLLUP_CONTEXT_CAP, for the same reason.
PAGE_CAP = 100

# A check run is only evidence once it has stopped moving.
TERMINAL_STATUSES = {"COMPLETED"}

# THE VERDICT VOCABULARY. `evaluate` returns one of these, and `main` decides
# whether to keep polling by READING IT -- never by matching the prose in the
# report. An earlier draft of this file sniffed its own message strings to
# decide what was worth waiting for, which is CLAUDE.md rule 9 one level in: the
# decision was a COPY of the rule, so editing a sentence in the report would
# silently have changed which failures poll. Only PENDING is waitable; an open
# finding will not clear itself, so polling on it burns the budget and then
# reports the same thing.
PASS = "pass"        # the gate is satisfied
PENDING = "pending"  # Bugbot has not delivered a terminal verdict on this head
FAIL = "fail"        # Bugbot reviewed the head and something is open

QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      isDraft
      headRefOid
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              contexts(first: 100) {
                totalCount
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    detailsUrl
                    checkSuite { app { slug } }
                  }
                }
              }
            }
          }
        }
      }
      reviewThreads(first: 100) {
        totalCount
        nodes {
          isResolved
          isOutdated
          comments(first: 1) {
            nodes {
              author { login }
              body
              url
            }
          }
        }
      }
    }
  }
}
"""


class Unreadable(Exception):
    """The gate could not establish the facts. Never a pass."""


def _run_gh(args, env):
    return subprocess.run(
        args, capture_output=True, text=True, env=env, check=False
    )


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


def bugbot_check(pr):
    """The Bugbot check run on the PR's CURRENT head, or None.

    Raises on a truncated rollup: a context lost to pagination is
    indistinguishable from Bugbot never having run, and that is the direction
    this gate must not guess in.
    """
    commits = (pr.get("commits") or {}).get("nodes") or []
    if not commits:
        raise Unreadable("PR reported no commits, so there is no head to check")
    commit = commits[0]["commit"]
    head = commit.get("oid")
    if head != pr.get("headRefOid"):
        # commits(last:1) is the head by construction; if it is not, the read is
        # inconsistent and "cannot tell" is a finding.
        raise Unreadable(
            "last commit %s is not headRefOid %s -- inconsistent read"
            % (head, pr.get("headRefOid"))
        )
    rollup = commit.get("statusCheckRollup")
    if rollup is None:
        # No checks at all on the head. Not truncation, and not Bugbot either.
        return None
    contexts = rollup.get("contexts") or {}
    total = contexts.get("totalCount")
    if total is None:
        raise Unreadable("rollup had no totalCount, so truncation cannot be ruled out")
    if total >= PAGE_CAP:
        raise Unreadable(
            "head has %d check contexts and the query asks for %d -- the rollup "
            "may be truncated, which would report Bugbot absent on a head it "
            "reviewed. Refusing to guess." % (total, PAGE_CAP)
        )
    for node in contexts.get("nodes") or []:
        if node.get("__typename") != "CheckRun":
            continue
        slug = (((node.get("checkSuite") or {}).get("app") or {}) or {}).get("slug")
        if slug == BUGBOT_APP_SLUG:
            return node
    return None


def severity_of(body):
    """Bugbot's declared severity for a finding, lowercased.

    Returns None when the body carries no severity line -- which the caller
    treats as a failure, not as a harmless finding.
    """
    match = SEVERITY_RE.search(body or "")
    return match.group(1).lower() if match else None


def findings(pr):
    """Every Bugbot finding thread on the PR.

    Raises on a truncated thread page, same reasoning as the rollup.
    """
    threads = pr.get("reviewThreads") or {}
    total = threads.get("totalCount")
    if total is None:
        raise Unreadable("reviewThreads had no totalCount, so truncation cannot be ruled out")
    if total >= PAGE_CAP:
        raise Unreadable(
            "PR has %d review threads and the query asks for %d -- the thread "
            "page may be truncated, so an open finding could be invisible. "
            "Refusing to guess." % (total, PAGE_CAP)
        )
    out = []
    for thread in threads.get("nodes") or []:
        comments = (thread.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        first = comments[0]
        login = ((first.get("author") or {}) or {}).get("login")
        body = first.get("body") or ""
        if login != BUGBOT_LOGIN or FINDING_MARKER not in body:
            continue
        out.append(
            {
                "severity": severity_of(body),
                "resolved": bool(thread.get("isResolved")),
                "outdated": bool(thread.get("isOutdated")),
                "title": (body.strip().splitlines() or ["(no title)"])[0].lstrip("# ").strip(),
                "url": first.get("url") or "",
            }
        )
    return out


def evaluate(pr, min_severity):
    """Decide the gate.

    Returns (verdict, lines) where verdict is PASS / PENDING / FAIL. Raises
    Unreadable when the facts could not be established -- never a pass.
    """
    if min_severity not in SEVERITY_RANK:
        raise Unreadable(
            "threshold %r is not one of %s" % (min_severity, ", ".join(SEVERITY_RANK))
        )
    floor = SEVERITY_RANK.index(min_severity)
    lines = []

    # A DRAFT PASSES, and this is the one deliberate fail-open in the file.
    # Two reasons, and the second is why it is safe: a draft cannot merge, so a
    # missing verdict on one is not a merge risk; and a caller's trigger list
    # must include `ready_for_review` (the reusable's header says so), so the
    # gate re-evaluates the moment the draft stops being one. Same treatment and same reasoning as bricked-prs.py, which
    # excludes drafts because "a draft cannot merge anyway, so a missing context
    # on one is not a brick". Bugbot's own behaviour on drafts is its business,
    # not something to encode here.
    if pr.get("isDraft"):
        return PASS, [
            "Draft PR -- not evaluated. A draft cannot merge, and this gate "
            "re-runs on `ready_for_review`."
        ]

    found = findings(pr)

    unknown = [f for f in found if f["severity"] not in SEVERITY_RANK]
    if unknown:
        raise Unreadable(
            "Bugbot reported %d finding(s) with a severity this gate does not "
            "recognise (%s). The declared rank is %s. An unrecognised severity "
            "is not a harmless one."
            % (
                len(unknown),
                ", ".join(sorted({repr(f["severity"]) for f in unknown})),
                ", ".join(SEVERITY_RANK),
            )
        )

    check = bugbot_check(pr)
    if check is None:
        return PENDING, [
            "No Bugbot check run on head %s." % (pr.get("headRefOid") or "?")[:12],
            "Bugbot has not reviewed the current head, so nothing has looked at "
            "the last push. An absence never approves.",
        ]
    if check.get("status") not in TERMINAL_STATUSES:
        return PENDING, [
            "Bugbot is still running on head %s (status %s)."
            % ((pr.get("headRefOid") or "?")[:12], check.get("status")),
            "A verdict that has not arrived is not a clean one.",
        ]

    lines.append(
        "Bugbot reviewed head %s -- check %r, status %s, conclusion %s."
        % (
            (pr.get("headRefOid") or "?")[:12],
            check.get("name"),
            check.get("status"),
            check.get("conclusion"),
        )
    )
    lines.append(
        "That conclusion is REPORTED, not used: `neutral` is what Bugbot emits "
        "when it HAS findings and it never emits `failure`, so this gate is "
        "derived from the threads below (backend#2284)."
    )

    blocking = [
        f
        for f in found
        if not f["resolved"] and SEVERITY_RANK.index(f["severity"]) >= floor
    ]
    if found:
        lines.append("")
        lines.append("Findings on this PR (%d):" % len(found))
        for f in found:
            lines.append(
                "  - %-8s %-8s %s %s"
                % (
                    f["severity"],
                    "resolved" if f["resolved"] else "OPEN",
                    "(outdated)" if f["outdated"] else "          ",
                    f["title"][:90],
                )
            )
    else:
        lines.append("No Bugbot findings on this PR.")

    if blocking:
        lines.append("")
        lines.append(
            "%d open finding(s) at or above %s. Either fix them, or reply on the "
            "thread with the ticket and resolve it -- then RE-RUN THIS CHECK. Do "
            "not push an empty commit to clear it: this gate reads the threads, "
            "and a re-run is enough. (`required_conversation_resolution` is true "
            "on every branch measured, so an open thread of ANY severity already "
            "blocks the merge independently of this check; what this line adds is "
            "naming which finding and how bad it is.)"
            % (len(blocking), min_severity)
        )
        return FAIL, lines
    return PASS, lines


def _emit(verdict, lines, hard_error=None):
    body = []
    if hard_error is not None:
        body.append("**Cannot tell, so this is a finding.**")
        body.append("")
        body.append("```")
        body.append(str(hard_error))
        body.append("```")
    else:
        body.append(
            "**%s**"
            % ("Bugbot review gate: pass" if verdict == PASS else "Bugbot review gate: FAIL")
        )
        body.append("")
        body.extend(lines)
    text = "\n".join(body)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write("## Bugbot review gate\n\n" + text + "\n")
        except OSError:
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    repo = os.environ.get("REPO", "")
    number = os.environ.get("PR_NUMBER", "")
    min_severity = (os.environ.get("MIN_SEVERITY") or "high").strip().lower()
    try:
        wait_seconds = int(os.environ.get("WAIT_SECONDS") or "900")
        poll_seconds = int(os.environ.get("POLL_SECONDS") or "20")
    except ValueError as exc:
        _emit(FAIL, [], "WAIT_SECONDS/POLL_SECONDS not integers: %s" % exc)
        return 2
    if "/" not in repo or not number.isdigit():
        _emit(FAIL, [], "REPO must be owner/name and PR_NUMBER a number; got %r / %r" % (repo, number))
        return 2
    owner, name = repo.split("/", 1)
    number = int(number)

    sleeper = time.sleep
    clock = time.monotonic
    deadline = clock() + max(0, wait_seconds)
    attempt = 0
    while True:
        attempt += 1
        try:
            pr = fetch(owner, name, number)
            verdict, lines = evaluate(pr, min_severity)
        except Unreadable as exc:
            _emit(FAIL, [], exc)
            return 2
        if verdict == PASS:
            _emit(PASS, lines)
            return 0
        if verdict != PENDING or clock() >= deadline:
            if verdict == PENDING:
                lines.append("")
                lines.append(
                    "Waited %ds over %d attempt(s) and Bugbot never reported a "
                    "terminal verdict on this head. Measured Bugbot latency is "
                    "p50 164s / max 635s over 40 runs, so this is not slowness."
                    % (wait_seconds, attempt)
                )
            _emit(verdict, lines)
            return 1
        remaining = deadline - clock()
        sleeper(min(poll_seconds, max(1, int(remaining))))


if __name__ == "__main__":
    sys.exit(main())
