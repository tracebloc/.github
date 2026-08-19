#!/usr/bin/env python3
"""Find PRs bricked by a required check that never reports (backend#1721).

THE FAILURE MODE. A required status check that never runs on a PR leaves its
context at "Expected - waiting for status to be reported". The PR is then:

  * approved,
  * with nothing red to point at,
  * and permanently unmergeable.

It reads to the author as "approved but stuck". Nobody is notified, and no
reviewer sees a problem, because there is no failure -- only an absence. It is
the one CI failure mode with no red signal at all, which is why it needs a
watcher rather than a check.

Three causes, all observed on this fleet:

  * a required context produced by a PATH-FILTERED workflow, on a PR touching
    none of those paths (`client` / Source-of-truth drift bricked #651, #657,
    #660);
  * a check made required AFTER a branch was cut, so the PR's head never runs it
    (`tracebloc-website` / quality / action-pins bricked #472);
  * a required context NO workflow produces -- renamed job, deleted workflow,
    typo in protection;
  * a CONFLICTED PR. GitHub cannot compute a merge commit for a PR whose base
    has moved incompatibly, so `pull_request` workflows never run at all and
    EVERY required context stays absent. Found by this script on its first real
    run: release-train#67, 82 minutes after its last push, had 0 workflow runs
    on its head sha and only a Bugbot verdict (which reviews the diff, not via
    a pull_request trigger). The remedy is a rebase, NOT a protection change --
    which is why the report names it separately.

WHY THIS IS EMPIRICAL AND NOT STATIC. A static audit of "can this check ever
report here" must model path filters x matrix expansion x reusable-workflow
inputs x their defaults x `if:` expressions. Run across 19 repos it produced
three separate classes of false positive before it produced anything true, and
being wrong in the safe-looking direction is how a fleet gets reported healthy
while PRs sit stuck.

Comparing the branch's required contexts against the contexts actually PRESENT
on the PR needs none of that modelling. Whatever the cause, required-but-absent
is bricked.

Required contexts come from `caller_drift.read_protection`, which unions classic
protection with rulesets. That is imported rather than reimplemented on purpose:
a ruleset-only branch 404s on the classic API (backend#1276), and two copies of
that logic is how one of them silently stops reading half the picture.

Exit codes, mirroring caller-drift.py:
  0  every repo read, no PR is missing a required context
  1  at least one PR is bricked
  2  could not evaluate -- an unreadable repo, branch, or PR list
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# A head whose checks have not been CREATED yet looks exactly like a head whose
# required check will never report: both have the context absent. The only thing
# separating them is time. Measured while building this: release-train#67 was
# reported bricked on all three of its contexts three minutes after a push, and
# had them all a few minutes later.
#
# So a young head is not judged. Sixty minutes is well past any observed queue
# on this fleet (the slowest job here is ~30 min and it still REPORTS within
# seconds of the push), and a watcher that cries wolf on every fresh push is one
# nobody reads -- the failure mode of the always-red check one repo over.
MIN_HEAD_AGE_MINUTES = 60

# THE REVIEWER CAN GO MISSING THE SAME WAY A CHECK CAN (backend#2114).
#
# Cursor Bugbot's auto-trigger silently drops PRs. Measured 2026-08-17: five open
# PRs across cli, release-train and .github carried no `Cursor Bugbot` check at all,
# while PRs opened minutes before and hours after were reviewed normally. No
# discriminator survived -- not diff size (a 1-file/66-line PR was reviewed, a
# 1-file/29-line one was not), not file type, not repo, not time, not quota.
# Posting `bugbot run` started a review within a minute and both came back clean, so
# the reviews were not failing; they were never starting.
#
# This is the same shape as the required-check case above and belongs in the same
# watcher for the same reason: the PR shows a full green check list, and nothing
# distinguishes "Bugbot found nothing" from "Bugbot never ran". It was caught by a
# human noticing a missing row.
#
# A `skipped` Bugbot check COUNTS AS PRESENT and is deliberately not a finding: it
# ran and decided, which is a verdict. Only total absence is the silent drop.
BUGBOT_CONTEXT = "Cursor Bugbot"
# Bugbot does not review bot-authored PRs, and that is correct rather than a drop --
# Dependabot's absence was the one legitimate case in the measurement.
#
# KEYED ON `is_bot`, NOT ON THE LOGIN (Bugbot, .github#282). The first version tested
# `login.endswith("[bot]")`, which NEVER MATCHES: `gh pr list --json author` returns
# `{"is_bot": true, "login": "app/dependabot"}` -- the `owner[bot]` form is what the
# REST API and the web UI show, not what this command returns. So every Dependabot PR
# without a Bugbot review would have been reported UNREVIEWED.
#
# Worse, the selftest fixtured `dependabot[bot]` -- the shape I assumed rather than the
# shape I measured -- so the exemption test passed while the exemption could not fire.
# A fixture invented instead of measured is a test that asserts its author's belief,
# which is the failure this whole guard exists to catch, one level in.
#
# `is_bot` is in the same JSON payload already being fetched, so this costs nothing and
# cannot drift the way a name-matching rule would.
BOT_AUTHOR_FIELD = "is_bot"

# `gh pr list` truncates at --limit and says nothing about it, so a repo with
# more open PRs than this would be audited PARTIALLY and reported clean -- the
# fail-open shape this watcher exists to remove, in the watcher itself. The cap
# is high enough that no tracebloc repo approaches it (the busiest has ~10 open
# PRs against one base), and reaching it is treated as "could not audit" rather
# than raised, because a number nobody can justify is worse than a stated limit
# that refuses.
PR_LIST_LIMIT = 200

# `gh pr list --json statusCheckRollup` asks GraphQL for `contexts(first: 100)` and
# says nothing when a head has more than that -- the SAME silent-truncation shape as
# PR_LIST_LIMIT above, one level in, and it fails in the direction this file is about:
# a context dropped by pagination is indistinguishable from a context that never ran,
# so a truncated rollup reports `Cursor Bugbot` absent on a PR Bugbot reviewed fine.
#
# NOT hypothetical the way the cap on PR_LIST_LIMIT is. Measured 2026-08-18:
# client#746 carries 77 distinct contexts (81 check runs; the rollup is latest-per-
# context, so the two numbers differing is correct and not truncation). 77 is 23 away.
#
# I could not construct a >100 head to observe the truncation, so this constant is
# gh's page size and not something derived from a failing observation -- stated here
# rather than dressed up, and the selftest exercises the guard on a synthetic rollup
# so the branch is proven reachable even though the live input has not appeared yet.
ROLLUP_CONTEXT_CAP = 100

HERE = Path(__file__).resolve().parent


def _load_caller_drift():
    """Import caller-drift.py as a module.

    The filename has a hyphen, so it is not importable by name. Loading it by
    path is the price of NOT having a second copy of the protection reader --
    and a second copy is exactly the drift this file would otherwise create.
    """
    path = HERE / "caller-drift.py"
    spec = importlib.util.spec_from_file_location("caller_drift", path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable in repo
        sys.stderr.write(f"::error::cannot load {path}\n")
        raise SystemExit(2)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CD = _load_caller_drift()


def open_prs(org: str, name: str, base: str) -> "list[dict]":
    """Open, non-draft PRs targeting `base`, with the contexts on their head.

    A draft cannot merge anyway, so a missing context on one is not a brick --
    it is reported separately by the caller only if asked. `statusCheckRollup`
    carries both shapes: check runs (`name`) and legacy statuses (`context`).
    """
    raw = CD.gh([
        "pr", "list", "--repo", f"{org}/{name}", "--state", "open",
        "--base", base, "--limit", str(PR_LIST_LIMIT), "--json",
        "number,isDraft,title,mergeStateStatus,reviewDecision,statusCheckRollup,url,headRefOid,author",
    ])
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CD.GhError(None, f"pr list returned unparseable JSON: {exc}") from exc
    if len(prs) >= PR_LIST_LIMIT:
        raise CD.GhError(
            None,
            f"open PR list hit the {PR_LIST_LIMIT} cap, so the view is partial and "
            "a bricked PR could be outside it",
        )
    return prs


def _parse(stamp) -> "datetime | None":
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def head_age_minutes(org: str, name: str, sha: str) -> "float | None":
    """How long CI has had this head, in minutes; None if it cannot be dated.

    NOT the commit timestamp (Bugbot, .github#239). A commit's committer date is
    when it was WRITTEN, and a force-push can put a long-dated commit on a branch
    a second ago -- which would be judged immediately, producing exactly the
    false "bricked" the grace window exists to prevent.

    The honest clock is when CI first saw the head: the OLDEST check suite on the
    sha. GitHub creates a suite per app as soon as it has work for that head, so
    that timestamp is "CI has known about this for N minutes", which is the
    question being asked. Fall back to the commit date only when no suite exists
    at all -- for a conflicted PR there never will be one, and then the commit
    date is the only clock there is.

    Called ONLY for a PR that already looks bricked, so the fleet-wide cost is
    one or two API calls per candidate, not per PR.
    """
    try:
        suites = CD.gh_json(["api", f"repos/{org}/{name}/commits/{sha}/check-suites"])
    except CD.GhError:
        # An unreadable CI clock (502/403/rate-limit) is NOT "no suites" (Bugbot,
        # .github#239). Falling through to the commit date here would let a
        # force-pushed long-dated commit read as old and brick falsely -- the
        # exact failure this function exists to avoid. We cannot date the head,
        # so return None; the caller treats an undateable head as young and skips.
        return None
    stamps = []
    if isinstance(suites, dict):
        for suite in suites.get("check_suites") or []:
            when = _parse((suite or {}).get("created_at"))
            if when:
                stamps.append(when)
    if stamps:
        return (datetime.now(timezone.utc) - min(stamps)).total_seconds() / 60.0

    # No suite exists at all -- a SUCCESSFUL read that returned an empty list. A
    # conflicted PR never gets a suite, so the commit date is the only clock there
    # is. (A FAILED read was already handled above and never reaches here.)
    try:
        commit = CD.gh_json(["api", f"repos/{org}/{name}/commits/{sha}"])
    except CD.GhError:
        return None
    when = _parse(((commit.get("commit") or {}).get("committer") or {}).get("date"))
    if when is None:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds() / 60.0


def rollup_truncated(pr: dict) -> bool:
    """True when this PR's rollup may be missing contexts to pagination.

    `>=`, not `>`: at exactly the cap there is no way to tell a head with 100
    contexts from a head with 140, and "cannot tell" is a finding here rather
    than an assumption of the happier reading.
    """
    return len(pr.get("statusCheckRollup") or []) >= ROLLUP_CONTEXT_CAP


def present_contexts(pr: dict) -> "set[str]":
    out = set()
    for entry in pr.get("statusCheckRollup") or []:
        if not isinstance(entry, dict):
            continue
        for key in ("name", "context"):
            val = entry.get(key)
            if isinstance(val, str) and val:
                out.add(val)
    return out


def audit_repo(org: str, name: str, roles: "dict[str, str]") -> "tuple[list, list]":
    """Returns (findings, errors) for one repo."""
    findings: "list[dict]" = []
    errors: "list[str]" = []

    for role, branch in roles.items():
        prot = CD.read_protection(org, name, branch)
        if prot.error:
            # NEVER "clean". An unreadable branch is the fail-open shape this
            # watcher exists to eliminate, one level up from the PRs it audits.
            errors.append(f"{name}/{branch}: {prot.error}")
            continue
        required = set(prot.required_checks)
        # NO early `continue` on an empty required set (saadqbal + Bugbot, #282).
        # The reviewer check below does not depend on branch protection, and gating
        # it here made a missing review STRUCTURALLY UNREPORTABLE on exactly the
        # branches with the least protection -- the watcher going quiet where it is
        # needed most, and saying nothing about having skipped them. The
        # required-check block keeps its own `if required:` guard instead.
        try:
            prs = open_prs(org, name, branch)
        except CD.GhError as exc:
            errors.append(f"{name}/{branch}: PR list unreadable ({exc.detail})")
            continue

        for pr in prs:
            if pr.get("isDraft"):
                continue

            # REFUSE RATHER THAN GUESS (saadqbal, #282). Every conclusion below is
            # drawn from `present_contexts`, so a truncated rollup poisons both of
            # them -- and it poisons them SILENTLY, producing a confident UNREVIEWED
            # on a PR that was reviewed. An error keeps it visible and out of the
            # findings table, which is the same treatment an unreadable branch gets.
            if rollup_truncated(pr):
                errors.append(
                    f"{name}/{branch}#{pr.get('number')}: "
                    f"{len(pr.get('statusCheckRollup') or [])} rollup contexts at or "
                    f"over the {ROLLUP_CONTEXT_CAP} page size, so the context list may "
                    "be partial -- this PR was NOT audited"
                )
                continue

            # DECIDE WHAT LOOKS WRONG FIRST, DATE THE HEAD AFTER.
            #
            # Two review findings pulled in opposite directions here and the order is
            # what reconciles them. saadqbal (#282): calling `head_age_minutes` inside
            # each check below cost TWO uncached API calls for a PR missing both a
            # review and a required context. Hoisting it above both fixed that and
            # broke the other half -- Bugbot (#282): every HEALTHY PR then paid a
            # lookup whose answer nothing consumed, on the very shared credential
            # backend#2036 exists because it was measured exhausted.
            #
            # Classifying before dating satisfies both without a cache: at most one
            # lookup per PR, and none at all for a PR with nothing to report. It also
            # restores this loop's agreement with `head_age_minutes`'s own docstring
            # ("Called ONLY for a PR that already looks bricked"), which the hoist had
            # quietly made false.
            #
            # THE REVIEWER IS ITS OWN CAUSE. Absence of Bugbot has a remedy -- post
            # `bugbot run` -- that no other finding here would ever suggest, and it is
            # reported even when every required check is present, because that is
            # exactly the case that reads as a clean green PR (backend#2114).
            unreviewed = (not (pr.get("author") or {}).get(BOT_AUTHOR_FIELD)
                          and BUGBOT_CONTEXT not in present_contexts(pr))
            missing = sorted(required - present_contexts(pr)) if required else []
            if not unreviewed and not missing:
                continue

            age = head_age_minutes(org, name, pr.get("headRefOid") or "")

            # AN UNDATEABLE HEAD IS UNKNOWN, NOT YOUNG (Bugbot, #282). This used to
            # fold into `young` and drop the PR silently, which is the one shape this
            # watcher exists to remove: a candidate that looks wrong, cannot be
            # judged, and produces nothing at all. `head_age_minutes` returns None
            # only when a read FAILED (an empty suite list falls back to the commit
            # date), so this is a real API failure and not a quiet PR -- and if it is
            # persistent, the finding never surfaces. Same treatment as a truncated
            # rollup above: an error, visible, out of the findings table.
            if age is None:
                errors.append(
                    f"{name}/{branch}#{pr.get('number')}: "
                    f"{'no Bugbot review' if unreviewed else 'missing ' + ', '.join(missing)}"
                    ", but its head could not be dated, so young-vs-bricked is UNKNOWN"
                    " -- this PR was NOT audited"
                )
                continue

            # A young head has not reported YET, which is not this watcher's finding.
            if age < MIN_HEAD_AGE_MINUTES:
                continue

            if unreviewed:
                findings.append({
                    "cause": "bugbot-absent",
                    "repo": name,
                    "branch": branch,
                    "role": role,
                    "number": pr.get("number"),
                    "url": pr.get("url"),
                    "title": (pr.get("title") or "")[:70],
                    "missing": [BUGBOT_CONTEXT],
                    "mergeStateStatus": pr.get("mergeStateStatus"),
                    "reviewDecision": pr.get("reviewDecision") or "REVIEW_REQUIRED",
                    "headAgeMinutes": round(age),
                })

            if missing:
                findings.append({
                    # A conflict is a different diagnosis with a different fix,
                    # and conflating the two sends someone to edit branch
                    # protection when they needed to rebase.
                    "cause": ("conflicted"
                              if pr.get("mergeStateStatus") == "DIRTY"
                              else "never-reported"),
                    "repo": name,
                    "branch": branch,
                    "role": role,
                    "number": pr.get("number"),
                    "url": pr.get("url"),
                    "title": (pr.get("title") or "")[:70],
                    "missing": missing,
                    "mergeStateStatus": pr.get("mergeStateStatus"),
                    "reviewDecision": pr.get("reviewDecision") or "REVIEW_REQUIRED",
                    "headAgeMinutes": round(age),
                })
    return findings, errors


def resolve_roles(org: str, name: str) -> "tuple[dict[str, str], str | None]":
    """Which real branches to audit for this repo: develop, staging, and prod."""
    try:
        branches = {b["name"] for b in CD.gh_json_array(f"repos/{org}/{name}/branches")
                    if isinstance(b, dict) and isinstance(b.get("name"), str)}
    except CD.GhError as exc:
        return {}, f"{name}: branch list unreadable ({exc.detail})"

    roles: "dict[str, str]" = {}
    for role in ("develop", "staging", "prod"):
        # `prod` is whichever of main/master EXISTS -- resolved from the branch
        # list, never by probing, because GitHub follows rename redirects and a
        # probe of a renamed `master` answers 200 (caller-drift.py's own note).
        resolved = CD.resolve_role_branch(role, branches)
        if resolved:
            roles[role] = resolved
    return roles, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", default=str(HERE.parent / "repo-inventory.yml"))
    ap.add_argument("--org", default="tracebloc")
    ap.add_argument("--repo", action="append", default=None,
                    help="audit only these repos (repeatable); default is every "
                         "repo in the inventory")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    inv = CD.load_inventory(args.inventory)
    names = sorted((inv.get("repos") or {}).keys())
    if args.repo:
        unknown = [r for r in args.repo if r not in names]
        if unknown:
            sys.stderr.write(f"::error::not in the inventory: {', '.join(unknown)}\n")
            return 2
        names = [r for r in names if r in args.repo]

    findings: "list[dict]" = []
    errors: "list[str]" = []
    for name in names:
        roles, err = resolve_roles(args.org, name)
        if err:
            errors.append(err)
            continue
        f, e = audit_repo(args.org, name, roles)
        findings.extend(f)
        errors.extend(e)

    if args.json:
        print(json.dumps({"findings": findings, "errors": errors}, indent=2))
    else:
        # ONE LABEL PER CAUSE. `bugbot-absent` is NOT a bricked PR -- the PR can
        # merge perfectly well; what is missing is the REVIEW. Rendering it as
        # BRICKED would send the reader to branch protection for a problem fixed by
        # one comment, which is the true-count-false-name defect this file already
        # avoids for `conflicted` (backend#2114).
        LABELS = {"conflicted": "CONFLICTED", "bugbot-absent": "UNREVIEWED"}
        for f in findings:
            print(f"{LABELS.get(f['cause'], 'BRICKED')} {f['repo']}#{f['number']} -> {f['branch']}")
            print(f"        missing: {', '.join(f['missing'])}")
            if f["cause"] == "conflicted":
                print("        cause: merge conflict — no merge commit, so no "
                      "pull_request run. Rebase; do not touch protection.")
            elif f["cause"] == "bugbot-absent":
                print("        cause: Bugbot's auto-trigger dropped this PR — it was "
                      "never reviewed, which reads identically to a clean review.")
                print("        fix:   comment `bugbot run` on the PR.")
            print(f"        {f['reviewDecision']} / {f['mergeStateStatus']}  {f['url']}")
            print(f"        {f['title']}")
        for e in errors:
            print(f"COULD NOT AUDIT {e}")
        if not findings and not errors:
            print(f"No PR is missing a required context or a review "
                  f"({len(names)} repos audited).")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            if findings:
                fh.write("## PRs a human would read as fine\n\n")
                fh.write("| repo | PR | missing context(s) | cause | review | merge state |\n")
                fh.write("|---|---|---|---|---|---|\n")
                for f in findings:
                    cause = {"conflicted": "merge conflict — rebase, do not touch protection",
                             "bugbot-absent": "**never reviewed** — comment `bugbot run`",
                             }.get(f["cause"], "never reported")
                    fh.write(f"| `{f['repo']}` | [#{f['number']}]({f['url']}) | "
                             f"`{'`, `'.join(f['missing'])}` | {cause} | "
                             f"{f['reviewDecision']} | {f['mergeStateStatus']} |\n")
                fh.write("\nEach shows **nothing red to point at**. A bricked PR cannot merge "
                         "until the context reports or stops being required; an unreviewed one "
                         "merges perfectly well, which is the worse of the two.\n")
            if errors:
                fh.write("\n## Could not audit\n\n")
                for e in errors:
                    fh.write(f"- {e}\n")
                fh.write("\nUnreadable is **not** clean — these were not checked.\n")
            if not findings and not errors:
                fh.write(f"No PR is missing a required context ({len(names)} repos audited).\n")

    # An unreadable repo outranks a clean sweep: reporting "0 bricked" when part
    # of the fleet was never read is the fail-open this watcher exists to remove.
    if errors:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
