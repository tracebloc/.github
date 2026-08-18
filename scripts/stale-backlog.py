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
# A run that would touch more than this is refused rather than executed: the only
# way to reach it is a bug in the eligibility rule or a board outage that made
# everything look like Backlog, and either way mass-closing is the wrong response.
MAX_ACTIONS = 40

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
        labels(first:40){nodes{name}}
        projectItems(first:10){nodes{
          project{number}
          fieldValueByName(name:"Status"){
            ... on ProjectV2ItemFieldSingleSelectValue{name}
          }
        }}
      }
    }
  }
}""" % PAGE


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


def status_of(issue, project_number):
    """The issue's Status on the given project, or None if it cannot be established.

    None is returned for: no card, a card with no Status value, and a Status whose
    name is not a string. All three are UNKNOWN, and unknown is not Backlog. The
    caller must not distinguish them -- treating "no card" as more actionable than
    "unreadable" is how a board outage becomes a closing spree.
    """
    for item in ((issue.get("projectItems") or {}).get("nodes") or []):
        if not isinstance(item, dict):
            continue
        if ((item.get("project") or {}).get("number")) != project_number:
            continue
        val = (item.get("fieldValueByName") or {}).get("name")
        return val if isinstance(val, str) and val else None
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
    labels = {n.get("name") for n in ((issue.get("labels") or {}).get("nodes") or [])
              if isinstance(n, dict)}
    status = status_of(issue, project_number)

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

    due, skipped, unknown = [], [], 0
    for issue in issues:
        action, reason, status = decide(issue, a.project, now)
        if status is None:
            unknown += 1
        if action:
            due.append((issue, action, reason))
        else:
            skipped.append((issue, reason))

    print(f"{a.repo}: {len(issues)} open issue(s), {len(due)} due, {len(skipped)} skipped"
          f"{f', {unknown} with no readable Status' if unknown else ''}")

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
    if unknown and a.strict:
        print(f"::error::--strict: {unknown} issue(s) had no readable board Status, so "
              "their eligibility is UNKNOWN. They were skipped, which is safe, but the "
              "sweep did not see the whole repo.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
