#!/usr/bin/env python3
"""Close stale BACKLOG issues -- and only backlog issues (backend#1979, #1597 item 1).

WHY THIS REPLACED actions/stale
-------------------------------
`actions/stale` exempts on LABELS ONLY. It has no concept of the board, so an item
in `North Stars`, `Ready for prod` or `In progress` went stale and auto-closed after
8 weeks of silence exactly like a Backlog item -- a strategic priority archived
because nobody commented on it. `kanban-reconcile.yml` already carries a "Shield
North Stars from the stale sweep" step, which is the workaround admitting the bug.

Board awareness cannot be expressed as a label, so it cannot be a config knob on an
action. That is the whole reason this is a script, and the reason the 16-way copy had
to become a reusable first (backend#1979): a fix that lives in code cannot be
maintained in sixteen byte-identical copies.

THE RULE, and it is narrower than "not exempt"
----------------------------------------------
An issue is eligible ONLY if its board Status is exactly `Backlog`. Everything else
-- any other column, no card at all, or a Status that could not be read -- is
skipped. `Backlog` is an ALLOW-list of one, not a deny-list of the columns we thought
of, because a column added next year would otherwise be eligible by default and
nobody would find out until something was closed.

FAIL CLOSED means SKIP here, not "act"
--------------------------------------
Every other guard in this repo fails closed by REFUSING TO REPORT CLEAN. This one
fails closed by refusing to ACT: the destructive direction is closing, so an
unreadable board, an unparseable response or an absent card must all mean "leave it
alone". `--strict` additionally exits non-zero so an outage is visible rather than
merely harmless -- silence that means "I could not tell" should not look like
silence that means "nothing was due".

USAGE
  stale-backlog.py --repo owner/name [--project 2] [--dry-run] [--strict]
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

STALE_LABEL = "stale"
EXEMPT_LABELS = {"keep-open", "blocked"}
ELIGIBLE_STATUS = "Backlog"
DAYS_TO_STALE = 42   # 6 weeks of silence -> warning
DAYS_TO_CLOSE = 14   # +2 weeks after the warning -> close
PAGE = 100
# The label and card pages. Both are TRUNCATION-CHECKED rather than assumed
# sufficient (Bugbot, #288): a `keep-open` label beyond the page would read as
# ABSENT, and this script's one destructive act is closing an issue -- so a
# truncated read must skip, never proceed. `totalCount` is requested for exactly
# that comparison; without it the query cannot tell a short list from a cut one.
LABEL_PAGE = 40
ITEM_PAGE = 10
# A run that would touch more than this is refused rather than executed: the only
# way to reach it is a bug in the eligibility rule or a board outage that made
# everything look like Backlog, and either way mass-closing is the wrong response.
MAX_ACTIONS = 40

# The status a truncated read reports, distinct from None. None means "no card / no
# Status", which is ordinary and expected; TRUNCATED means "we could not tell", which
# is a defect in the read. Collapsing them would make a query regression -- every
# label list cut, every issue skipped -- indistinguishable from an empty backlog
# (Bugbot, #288). A sentinel object rather than a string, so it can never equal
# ELIGIBLE_STATUS or any column somebody adds.
TRUNCATED = object()

STALE_COMMENT = (
    "This issue has had no activity for 6 weeks and is in `Backlog`.\n\n"
    "If it is still relevant, leave a comment with current context or assign "
    "someone. Otherwise it closes in 2 weeks.\n\n"
    "To exempt it permanently, add the `keep-open` label."
)
CLOSE_COMMENT = (
    "Closing after 8 weeks of inactivity in `Backlog`. Reopen with current "
    "context if it is still relevant."
)


class GhError(Exception):
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


def gh(args):
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise GhError((p.stderr or "").strip()[:200] or f"gh exited {p.returncode}")
    return p.stdout


ISSUES_Q = """
query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){
    issues(first:%d, states:OPEN, after:$cursor,
           orderBy:{field:UPDATED_AT, direction:ASC}){
      pageInfo{hasNextPage endCursor}
      nodes{
        number title updatedAt
        labels(first:%d){totalCount nodes{name}}
        projectItems(first:%d){totalCount nodes{
          project{number}
          fieldValueByName(name:"Status"){
            ... on ProjectV2ItemFieldSingleSelectValue{name}
          }
        }}
      }
    }
  }
}""" % (PAGE, LABEL_PAGE, ITEM_PAGE)


def fetch_issues(owner, name):
    """Every open issue with its labels and its board Status. Raises on any failure."""
    out, cursor = [], None
    while True:
        args = ["api", "graphql", "-f", f"query={ISSUES_Q}",
                "-F", f"owner={owner}", "-F", f"name={name}"]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        try:
            data = json.loads(gh(args))
        except json.JSONDecodeError as exc:
            raise GhError(f"issue list returned unparseable JSON: {exc}") from exc
        try:
            page = data["data"]["repository"]["issues"]
        except (KeyError, TypeError) as exc:
            raise GhError(f"issue list had no repository.issues: {exc}") from exc
        out += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


def truncated(conn, page):
    """True when a GraphQL connection returned fewer nodes than it has.

    `totalCount > len(nodes)` is the only honest test. Comparing len(nodes) to the
    page size alone would call a list of exactly `page` items truncated, and would
    miss nothing -- but it also cannot distinguish "exactly full" from "cut", and
    this script skips on truncation, so a false positive is a silently unswept
    issue. Both numbers are asked for; both are used.
    """
    if not isinstance(conn, dict):
        return True  # an unreadable connection is not a short one
    nodes = conn.get("nodes")
    total = conn.get("totalCount")
    if not isinstance(nodes, list) or not isinstance(total, int):
        # NO totalCount MEANS NO ANSWER. If the query stops asking for it, this
        # returns True and every issue is skipped -- loudly useless rather than
        # quietly closing things on a partial label list.
        return True
    return total > len(nodes)


def status_of(issue, project_number):
    """The issue's Status on the given project, or None if it cannot be established.

    None is returned for: no card, a card with no Status value, and a Status whose
    name is not a string. All three are UNKNOWN, and unknown is not Backlog. The
    caller must not distinguish them -- treating "no card" as more actionable than
    "unreadable" is how a board outage becomes a closing spree.
    """
    conn = issue.get("projectItems")
    for item in ((conn or {}).get("nodes") or []):
        if not isinstance(item, dict):
            continue
        if ((item.get("project") or {}).get("number")) != project_number:
            continue
        val = (item.get("fieldValueByName") or {}).get("name")
        return val if isinstance(val, str) and val else None
    # NO CARD FOUND -- but was the list complete? A truncated card page means the
    # target project's card may exist beyond it, so "no card" is not established.
    # This already lands on None and therefore on a SKIP, so unlike the label case
    # it was never destructive; it is checked so the two connections are handled by
    # the same rule rather than one by accident (Bugbot, #288).
    if truncated(conn, ITEM_PAGE):
        return None
    return None
    return None


def age_days(stamp, now):
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return (now - when).total_seconds() / 86400.0


def decide(issue, project_number, now):
    """(action, reason, status) -- action is None when nothing is due.

    `status` is returned so the caller can count UNREADABLE ones without
    re-querying and without string-matching the human-readable reason. An earlier
    version did match on the reason text, which couples a counter to prose that
    exists to be reworded -- the same defect this repo keeps finding in guards.
    """
    label_conn = issue.get("labels")
    labels = {n.get("name") for n in ((label_conn or {}).get("nodes") or [])
              if isinstance(n, dict)}
    status = status_of(issue, project_number)

    # TRUNCATION IS FAIL-OPEN ON THE DESTRUCTIVE PATH, so it is checked FIRST
    # (Bugbot, #288). An exempt label past the page reads as absent, and the
    # consequence is closing an issue somebody labelled `keep-open`. Unlike an
    # unreadable Status -- which lands on "not Backlog" and skips anyway -- this one
    # would have proceeded.
    if truncated(label_conn, LABEL_PAGE):
        n = len((label_conn or {}).get("nodes") or [])
        t = (label_conn or {}).get("totalCount")
        # TRUNCATED, not merely None. The caller counts these separately and
        # `--strict` fails on them: a skip nobody counts is a clean-looking sweep
        # (Bugbot, #288). See the note above the counters in main().
        return None, (f"label list truncated ({n} of {t}) -- cannot rule out an "
                      "exempt label"), TRUNCATED

    if labels & EXEMPT_LABELS:
        return None, f"exempt label ({', '.join(sorted(labels & EXEMPT_LABELS))})", status

    if status != ELIGIBLE_STATUS:
        # THE FIX. Everything that is not exactly Backlog is skipped, including
        # unknown. An allow-list of one, so a new column is safe by default.
        return None, f"status is {status!r}, not {ELIGIBLE_STATUS!r}", status

    age = age_days(issue.get("updatedAt"), now)
    if age is None:
        return None, "updatedAt unreadable", status

    if STALE_LABEL in labels:
        # The warning bumped updatedAt, so the clock since then is the grace period.
        # Any human activity also bumps it, which correctly RESETS the countdown --
        # the same behaviour actions/stale had, achieved by the same signal.
        return ("close", f"stale for {age:.0f}d", status) if age >= DAYS_TO_CLOSE \
            else (None, f"warned {age:.0f}d ago, closes at {DAYS_TO_CLOSE}d", status)

    return ("stale", f"idle {age:.0f}d", status) if age >= DAYS_TO_STALE \
        else (None, f"idle {age:.0f}d, warns at {DAYS_TO_STALE}d", status)


def apply(repo, number, action, dry_run):
    if dry_run:
        return True, "dry-run"
    try:
        if action == "stale":
            gh(["issue", "edit", str(number), "--repo", repo, "--add-label", STALE_LABEL])
            gh(["issue", "comment", str(number), "--repo", repo, "--body", STALE_COMMENT])
        else:
            gh(["issue", "comment", str(number), "--repo", repo, "--body", CLOSE_COMMENT])
            gh(["issue", "close", str(number), "--repo", repo, "--reason", "not planned"])
        return True, "ok"
    except GhError as exc:
        return False, exc.detail


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--project", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero when the board could not be read")
    a = ap.parse_args()
    owner, _, name = a.repo.partition("/")
    if not owner or not name:
        print(f"::error::--repo must be owner/name, got {a.repo!r}")
        return 2

    now = datetime.now(timezone.utc)
    try:
        issues = fetch_issues(owner, name)
    except GhError as exc:
        # SKIP, not act. Nothing is closed on a read we could not make.
        print(f"::error::could not read {a.repo}'s issues: {exc.detail}. "
              "Nothing was staled or closed.")
        return 2

    # THREE COUNTERS, because "skipped" was hiding a defect inside a normal outcome
    # (Bugbot, #288). A truncated label read is not the same event as an issue that
    # simply is not in Backlog: the first means the sweep COULD NOT TELL. Before this,
    # a query that stopped asking for `totalCount` skipped every issue, printed
    # `0 due` and exited 0 -- the same signal as a genuinely empty backlog, on an
    # unattended weekly cron.
    due, skipped, unknown, cut = [], [], 0, 0
    for issue in issues:
        action, reason, status = decide(issue, a.project, now)
        if status is TRUNCATED:
            cut += 1
        elif status is None:
            unknown += 1
        if action:
            due.append((issue, action, reason))
        else:
            skipped.append((issue, reason))

    print(f"{a.repo}: {len(issues)} open issue(s), {len(due)} due, {len(skipped)} skipped"
          f"{f', {unknown} with no readable Status' if unknown else ''}"
          f"{f', {cut} with a TRUNCATED label read' if cut else ''}")

    # LOUD EVEN WITHOUT --strict, because this one is not an environment problem the
    # operator can shrug at: it means the query and this script disagree. Every issue
    # cut is an issue whose `keep-open` we could not rule out.
    if cut:
        print(f"::warning::{cut} issue(s) had a truncated label read and were skipped. "
              f"Raise LABEL_PAGE (currently {LABEL_PAGE}) or check that ISSUES_Q still "
              "requests `totalCount` -- without it every label list reads as cut.")

    if len(due) > MAX_ACTIONS:
        print(f"::error::{len(due)} issues are due, over the MAX_ACTIONS={MAX_ACTIONS} "
              "ceiling. That is a bug in the eligibility rule or a board outage that "
              "made everything read as Backlog, not real drift. Refusing to act.")
        return 2

    failed = 0
    for issue, action, reason in due:
        ok, detail = apply(a.repo, issue["number"], action, a.dry_run)
        mark = "[DRY] " if a.dry_run else ""
        print(f"  {mark}{action.upper():5} #{issue['number']} ({reason}) "
              f"{issue.get('title','')[:54]}" + ("" if ok else f"  FAILED: {detail}"))
        failed += 0 if ok else 1

    if failed:
        print(f"::error::{failed} action(s) failed. Re-run once the cause is fixed.")
        return 1
    # `cut` FAILS --strict TOO. It was outside the strict test entirely, so the one
    # mode that exists to make an incomplete sweep visible could not see the most
    # complete way for a sweep to be incomplete.
    if cut and a.strict:
        print(f"::error::--strict: {cut} issue(s) had a truncated label read, so an "
              "exempt label could not be ruled out. They were skipped, which is safe, "
              "but the sweep did not establish eligibility for them.")
        return 2
    if unknown and a.strict:
        print(f"::error::--strict: {unknown} issue(s) had no readable board Status, so "
              "their eligibility is UNKNOWN. They were skipped, which is safe, but the "
              "sweep did not see the whole repo.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
