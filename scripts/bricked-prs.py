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

# `gh pr list` truncates at --limit and says nothing about it, so a repo with
# more open PRs than this would be audited PARTIALLY and reported clean -- the
# fail-open shape this watcher exists to remove, in the watcher itself. The cap
# is high enough that no tracebloc repo approaches it (the busiest has ~10 open
# PRs against one base), and reaching it is treated as "could not audit" rather
# than raised, because a number nobody can justify is worse than a stated limit
# that refuses.
PR_LIST_LIMIT = 200

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
        "number,isDraft,title,mergeStateStatus,reviewDecision,statusCheckRollup,url,headRefOid",
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
        suites = None
    stamps = []
    if isinstance(suites, dict):
        for suite in suites.get("check_suites") or []:
            when = _parse((suite or {}).get("created_at"))
            if when:
                stamps.append(when)
    if stamps:
        return (datetime.now(timezone.utc) - min(stamps)).total_seconds() / 60.0

    try:
        commit = CD.gh_json(["api", f"repos/{org}/{name}/commits/{sha}"])
    except CD.GhError:
        return None
    when = _parse(((commit.get("commit") or {}).get("committer") or {}).get("date"))
    if when is None:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds() / 60.0


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
        if not required:
            continue

        try:
            prs = open_prs(org, name, branch)
        except CD.GhError as exc:
            errors.append(f"{name}/{branch}: PR list unreadable ({exc.detail})")
            continue

        for pr in prs:
            if pr.get("isDraft"):
                continue
            missing = sorted(required - present_contexts(pr))
            if missing:
                # Young head -> "has not reported YET", which is not this
                # watcher's finding. An unreadable age is treated as young: a
                # false "bricked" is what makes the report ignorable, and the
                # next run picks it up anyway.
                age = head_age_minutes(org, name, pr.get("headRefOid") or "")
                if age is None or age < MIN_HEAD_AGE_MINUTES:
                    continue
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
        for f in findings:
            label = ("CONFLICTED" if f["cause"] == "conflicted" else "BRICKED")
            print(f"{label} {f['repo']}#{f['number']} -> {f['branch']}")
            print(f"        missing: {', '.join(f['missing'])}")
            if f["cause"] == "conflicted":
                print("        cause: merge conflict — no merge commit, so no "
                      "pull_request run. Rebase; do not touch protection.")
            print(f"        {f['reviewDecision']} / {f['mergeStateStatus']}  {f['url']}")
            print(f"        {f['title']}")
        for e in errors:
            print(f"COULD NOT AUDIT {e}")
        if not findings and not errors:
            print(f"No PR is missing a required context ({len(names)} repos audited).")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            if findings:
                fh.write("## Bricked PRs — a required check that never reported\n\n")
                fh.write("| repo | PR | missing context(s) | cause | review | merge state |\n")
                fh.write("|---|---|---|---|---|---|\n")
                for f in findings:
                    cause = ("merge conflict — rebase, do not touch protection"
                             if f["cause"] == "conflicted" else "never reported")
                    fh.write(f"| `{f['repo']}` | [#{f['number']}]({f['url']}) | "
                             f"`{'`, `'.join(f['missing'])}` | {cause} | "
                             f"{f['reviewDecision']} | {f['mergeStateStatus']} |\n")
                fh.write("\nEach is approved-or-approvable with **nothing red to point at**, "
                         "and cannot merge until the context reports or stops being required.\n")
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
