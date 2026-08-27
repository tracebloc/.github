#!/usr/bin/env python3
"""Make a merge-conflicted PR read RED on the PR (tracebloc/backend#2637).

THE PLATFORM BEHAVIOUR, which is not a bug in any of our YAML. GitHub cannot
compute a merge ref for a PR whose base has moved incompatibly, and a
`pull_request` workflow run is keyed on that merge ref. So a conflicted PR
dispatches NO `pull_request` jobs at all -- not the drift guards, not the
installer tests, not `Source-of-truth drift`, not `make drift`, not
`gen-manifest.sh --check`. Every one of them is silently inactive, and no
trigger tweak can change that: there is nothing for them to run against.

The result reads as HEALTH. Measured on `model-zoo#206`, 2026-08-27, while it
was `DIRTY` against `develop`:

  * `actions/runs?head_sha=e7465ea` -> `total_count: 0`. Nothing dispatched.
  * its `statusCheckRollup` carried exactly ONE entry -- `Cursor Bugbot`,
    `SUCCESS`. Bugbot reviews the diff rather than via a `pull_request` trigger,
    so it is the one voice that still speaks, and it says green.
  * `model-zoo/develop` requires SEVEN contexts -- `ruff`, `test-pytorch`,
    `test-sklearn`, `test-survival`, `quality / action-pins`,
    `quality / gitleaks`, `quality / house-rules`. None was present.
  * PRs #204 and #205, opened either side of it, each got their full three-to-
    four workflow matrix. The difference was the conflict, nothing else.

So the rollup showed one green check and no red. "Nothing ran" and "everything
passed" are the same picture, which is the fail-open this file removes.

AND THERE IS A SECOND, WORSE SHAPE -- the one that defeats the watcher. A PR that
was pushed BEFORE its base moved keeps the checks it already earned, because a
status is attached to the head sha and the head sha did not change. Measured on
`backend#2257` the same day, `mergeable=CONFLICTING`:

  * 8 workflow runs on its head sha, and a rollup of SIXTEEN entries;
  * ALL ELEVEN of backend/develop's required contexts present and SUCCESS.

That PR reads perfectly, unanimously green while being conflicted, and every one
of those green checks was computed against a merge base that no longer exists. It
is the `client#847` shape from the ticket: checks that reported before the
conflict appeared, and a `version-bump-gate` failure that stayed invisible for
three hours.

THIS IS WHY `bricked-prs.py` IS NOT ALREADY THE ANSWER. That watcher infers a
problem from a required context being ABSENT (`missing = required - present`).
Here nothing is missing -- the required set is fully present and green -- so
`missing` is empty and its `conflicted` cause, which only ever attaches to a
`missing` finding, cannot fire. A conflicted PR whose checks all pre-date the
conflict is invisible to it by construction. Only asking about mergeability
DIRECTLY, as this file does, sees it.

WHAT THIS DOES. It sweeps every open PR in the inventory and writes a commit
STATUS onto the PR's head sha:

  conflicted   -> failure   the PR now has something red to point at
  clear        -> success   the context clears itself, so it can never brick a
                            healthy PR (see "why success matters" below)
  undetermined -> pending   "cannot tell" is neither, and says so

WHY A STATUS AND NOT A JOB. A job is the thing that cannot run. A status is
written FROM OUTSIDE the PR against its head sha, so it needs no merge ref and
no checkout of the merge commit. It is the only signal that can reach a
conflicted PR at all.

WHY NOT `pull_request`: that is the defect. WHY NOT `pull_request_target`: it
would need no merge ref either, but NOTHING in this org uses it -- measured
2026-08-27, `actions/runs?event=pull_request_target` returns `total_count: 0`
fleet-wide -- so its dispatch behaviour on a conflicted PR is unverified here.
Building a fail-closed guard on an unmeasured platform claim is the mistake
CLAUDE.md rule 8 names, so this uses a trigger whose behaviour is already
proven by the other org-wide crons in this repo.

NO GRACE WINDOW, unlike `bricked-prs.py`, and the reason is the difference
between the two files. That watcher infers brickedness from an ABSENCE (a
required context that is not there), and an absence is indistinguishable from a
young head, so it must wait 60 minutes. A conflict is an AFFIRMATIVE answer:
`mergeable == CONFLICTING` is GitHub stating there are conflicts. There is
nothing to wait for, so a conflict is reported on the first sweep that sees it.

RELATIONSHIP TO `bricked-prs.py`. That file already names the conflicted-PR cause
and reports it, and this does not replace it -- they answer different questions
and neither subsumes the other. That one is a fleet WATCHER reasoning from an
ABSENT required context, into a step summary every four hours, for a human
sweeping the org; as shown above it is blind to the green-but-conflicted shape,
and its necessary 60-minute grace window means it says nothing about a fresh one
either. This asks GitHub about mergeability directly and writes the answer onto
the PR ITSELF, where the reviewer is looking. A finding in a cron log in another
repo is advice; a red row on the PR is what someone actually reads.

WHY `success` MATTERS AS MUCH AS `failure`. A context that only ever appears
when something is wrong cannot be required, and a required context that never
reports leaves a PR at "Expected -- waiting for status" forever. That is
precisely the brick `bricked-prs.py` exists to hunt, and shipping it here would
plant the bug next door to its own watcher. So every PR swept gets a status,
including the healthy ones.

Exit codes, mirroring the other audits in this repo:
  0  every repo read, no PR is conflicted
  1  at least one PR is conflicted
  2  could not evaluate -- an unreadable PR list, or a mergeability GitHub
     would not state
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# THE INPUT DOMAIN IS DERIVED FROM THE PRODUCER, NOT HAND-WRITTEN (CLAUDE.md
# rules 1 and 6). These are GitHub's own GraphQL enums, read out of the schema
# on 2026-08-27:
#
#   gh api graphql -f query='{ __type(name:"MergeableState"){enumValues{name}} }'
#   gh api graphql -f query='{ __type(name:"MergeStateStatus"){enumValues{name}} }'
#
# Reproduce that command rather than editing these by hand. Writing the list from
# memory is how a vocabulary gap gets in: the first draft of this file carried a
# `DRAFT` member of MergeStateStatus, which the schema does not have, and mutation
# coverage cannot see a value that is simply absent from the test inputs.
MERGEABLE_STATES = frozenset({"MERGEABLE", "CONFLICTING", "UNKNOWN"})
MERGE_STATE_STATUSES = frozenset({
    "DIRTY", "UNKNOWN", "BLOCKED", "BEHIND", "UNSTABLE", "HAS_HOOKS", "CLEAN",
})

# The two ways GitHub says "there are conflicts". Either alone is sufficient:
# they are the same fact read off two fields, and requiring both to agree would
# make one field's UNKNOWN suppress the other's affirmative answer.
CONFLICT_EVIDENCE = (("mergeable", "CONFLICTING"), ("mergeStateStatus", "DIRTY"))

CONFLICTED = "conflicted"
CLEAR = "clear"
UNDETERMINED = "undetermined"

# The status context. STABLE FOREVER once anything requires it: a renamed context
# does not stop being required, it stops being REPORTED, which bricks every open
# PR until someone edits protection.
CONTEXT = "conflict-gate / mergeable"

STATE_FOR = {
    CONFLICTED: "failure",
    CLEAR: "success",
    # NOT `failure`. `pending` blocks a merge exactly as `failure` does if this
    # context is ever required, so it is no less fail-closed -- but it does not
    # assert a conflict that was never observed. The distinction is the whole
    # point of having three verdicts instead of two: a reader must be able to
    # tell "you have a conflict" from "GitHub would not tell me".
    UNDETERMINED: "pending",
}

DESCRIPTION_FOR = {
    CONFLICTED: "Merge conflict: no merge ref, so NO pull_request check ran on this head",
    CLEAR: "No conflict with the base",
    UNDETERMINED: "Mergeability could not be determined - this PR was NOT judged",
}

# `gh pr list` truncates at --limit and says nothing about it, so a repo with
# more open PRs than this would be swept PARTIALLY and reported clean. Same cap
# and same reasoning as bricked-prs.py: reaching it is "could not evaluate",
# not a clean sweep.
PR_LIST_LIMIT = 200

# GitHub computes mergeability LAZILY. A PR read moments after a push routinely
# answers `UNKNOWN` on both fields, and the read itself is what schedules the
# computation -- so re-asking a moment later is the documented way to get an
# answer, not a workaround. Without this every sweep would paint `pending` over
# whichever PRs were freshly pushed.
DEFAULT_RETRIES = 3
DEFAULT_RETRY_SLEEP = 2.0


def _load_caller_drift():
    """Import caller-drift.py for its `gh` wrappers and inventory loader.

    Imported rather than reimplemented: `gh()` normalises the HTTP status out of
    stderr and closes stdin, and a second copy of that is how one of them
    silently stops handling a 502.
    """
    path = HERE / "caller-drift.py"
    spec = importlib.util.spec_from_file_location("caller_drift_for_conflict", path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable in repo
        sys.stderr.write(f"::error::cannot load {path}\n")
        raise SystemExit(2)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CD = _load_caller_drift()


def classify(pr: dict) -> "tuple[str, str]":
    """Verdict for one PR payload, as (verdict, why).

    PURE, and the only place the rule lives. The mutation harness edits this
    function and re-runs the real suite against it (CLAUDE.md rule 9) -- there is
    no second copy of these conditions anywhere in the tests.

    Order matters, and each step earns its place:

      1. A value GitHub did not declare is a VOCABULARY DRIFT, not a conflict and
         not health. If GitHub adds a member to either enum, this file's belief
         about the domain is stale and it says so instead of guessing which side
         the new value falls on.
      2. Affirmative conflict evidence outranks an UNKNOWN in the other field.
         `mergeable: CONFLICTING` with `mergeStateStatus: UNKNOWN` is a conflict;
         letting the UNKNOWN win would suppress the finding this file exists for.
      3. `mergeable: MERGEABLE` is GitHub answering the actual question -- "are
         there conflicts" -- so it is what CLEAR is keyed on. It is deliberately
         NOT keyed on `mergeStateStatus: CLEAN`: BLOCKED, BEHIND, UNSTABLE and
         HAS_HOOKS are all normal states for a perfectly unconflicted PR (BLOCKED
         is the ordinary "still needs its review"), and treating them as unclean
         would paint most of the fleet red.
      4. Anything left is an UNKNOWN nobody resolved.
    """
    mergeable = pr.get("mergeable")
    state = pr.get("mergeStateStatus")

    for field, value, domain in (("mergeable", mergeable, MERGEABLE_STATES),
                                 ("mergeStateStatus", state, MERGE_STATE_STATUSES)):
        if value is None:
            return UNDETERMINED, f"{field} absent from the PR payload"
        if not isinstance(value, str) or value not in domain:
            return UNDETERMINED, (
                f"{field}={value!r} is not one of GitHub's declared "
                f"{field} values, so this file's vocabulary is stale"
            )

    for field, value in CONFLICT_EVIDENCE:
        if pr.get(field) == value:
            return CONFLICTED, f"{field}={value}"

    if mergeable == "MERGEABLE":
        return CLEAR, f"mergeable=MERGEABLE (mergeStateStatus={state})"

    return UNDETERMINED, (
        f"mergeable={mergeable}, mergeStateStatus={state} -- GitHub has not "
        "computed mergeability"
    )


def plan(prs: "list[dict]") -> "list[dict]":
    """Turn PR payloads into the statuses to write. PURE.

    DRAFTS ARE SWEPT TOO, deliberately, where `bricked-prs.py` skips them. That
    watcher is asking "can this PR merge", and a draft cannot regardless. This is
    writing a context that may become required, and a draft marked ready for
    review does NOT get a fresh sweep of its own -- so skipping it would leave it
    with no status at the moment it starts needing one.
    """
    out = []
    for pr in prs:
        verdict, why = classify(pr)
        out.append({
            "number": pr.get("number"),
            "sha": pr.get("headRefOid"),
            "url": pr.get("url"),
            "title": (pr.get("title") or "")[:70],
            "base": pr.get("baseRefName"),
            "isDraft": bool(pr.get("isDraft")),
            "verdict": verdict,
            "why": why,
            "state": STATE_FOR[verdict],
            "description": DESCRIPTION_FOR[verdict],
        })
    return out


def open_prs(org: str, name: str) -> "list[dict]":
    """Every open PR in one repo, with the two mergeability fields.

    NOT filtered by base. This file does not care what the base requires -- a
    conflict is a conflict against whatever it targets -- and filtering by role
    branch would silently drop PRs against any other base.
    """
    raw = CD.gh([
        "pr", "list", "--repo", f"{org}/{name}", "--state", "open",
        "--limit", str(PR_LIST_LIMIT), "--json",
        "number,title,isDraft,mergeable,mergeStateStatus,headRefOid,baseRefName,url",
    ])
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CD.GhError(None, f"pr list returned unparseable JSON: {exc}") from exc
    if not isinstance(prs, list):
        raise CD.GhError(None, f"pr list returned {type(prs).__name__}, not a list")
    if len(prs) >= PR_LIST_LIMIT:
        raise CD.GhError(
            None,
            f"open PR list hit the {PR_LIST_LIMIT} cap, so the view is partial "
            "and a conflicted PR could be outside it",
        )
    return prs


def reread(org: str, name: str, number: int) -> "dict | None":
    """Re-read one PR, to let GitHub finish computing mergeability."""
    try:
        return CD.gh_json([
            "pr", "view", str(number), "--repo", f"{org}/{name}", "--json",
            "number,title,isDraft,mergeable,mergeStateStatus,headRefOid,baseRefName,url",
        ])
    except CD.GhError:
        return None


def resolve_undetermined(org: str, name: str, prs: "list[dict]", retries: int,
                         sleep_for: float, sleeper=time.sleep) -> "list[dict]":
    """Re-read the PRs whose mergeability came back UNKNOWN.

    Only those: a fleet-wide re-read would double the API cost to re-confirm
    answers GitHub already gave. `sleeper` is injected so the suite does not
    spend real seconds proving the retry happens.
    """
    if retries <= 0:
        return prs
    resolved = list(prs)
    for _ in range(retries):
        pending = [i for i, pr in enumerate(resolved)
                   if classify(pr)[0] == UNDETERMINED]
        if not pending:
            break
        sleeper(sleep_for)
        for i in pending:
            fresh = reread(org, name, resolved[i].get("number"))
            if fresh:
                resolved[i] = fresh
    return resolved


def post_status(org: str, name: str, sha: str, state: str, description: str,
                target_url: "str | None") -> None:
    """Write one commit status. Raises GhError."""
    args = [
        "api", "--method", "POST", f"repos/{org}/{name}/statuses/{sha}",
        "-f", f"state={state}",
        "-f", f"context={CONTEXT}",
        # GitHub truncates a description over 140 chars; ours are well under, and
        # the cap is stated here so a future edit does not discover it in prod.
        "-f", f"description={description[:140]}",
    ]
    if target_url:
        args += ["-f", f"target_url={target_url}"]
    CD.gh(args)


def sweep_repo(org: str, name: str, retries: int, sleep_for: float,
               dry_run: bool, sleeper=time.sleep) -> "tuple[list[dict], list[str]]":
    """Sweep one repo. Returns (statuses, errors)."""
    errors: "list[str]" = []
    try:
        prs = open_prs(org, name)
    except CD.GhError as exc:
        # NEVER "clean". An unreadable PR list is the fail-open shape this gate
        # exists to remove, one level up from the PRs it sweeps.
        return [], [f"{name}: PR list unreadable ({exc.detail})"]

    prs = resolve_undetermined(org, name, prs, retries, sleep_for, sleeper)
    statuses = plan(prs)

    for st in statuses:
        st["repo"] = name
        if st["verdict"] == UNDETERMINED:
            errors.append(
                f"{name}#{st['number']}: {st['why']} -- this PR was NOT judged"
            )
        if not st["sha"]:
            errors.append(f"{name}#{st['number']}: no head sha, so no status could be written")
            st["written"] = False
            continue
        if dry_run:
            st["written"] = False
            continue
        try:
            post_status(org, name, st["sha"], st["state"], st["description"], st["url"])
            st["written"] = True
        except CD.GhError as exc:
            st["written"] = False
            # A STATUS THAT DID NOT LAND IS THE WHOLE FAILURE, RE-ARMED. The PR
            # still reads empty-green and nothing said so, which is why this is an
            # error and not a logged warning.
            errors.append(
                f"{name}#{st['number']}: could not write the {st['state']} status "
                f"({exc.detail})"
            )
    return statuses, errors


def render(statuses: "list[dict]", errors: "list[str]", repo_count: int,
           dry_run: bool) -> None:
    conflicted = [s for s in statuses if s["verdict"] == CONFLICTED]
    for s in conflicted:
        mark = "would mark" if dry_run else "marked"
        print(f"CONFLICTED {s['repo']}#{s['number']} -> {s['base']}  ({mark} {s['state']})")
        print(f"        {s['why']}")
        print("        no pull_request check can run on this head until the "
              "conflict is resolved")
        print(f"        {s['url']}")
        print(f"        {s['title']}")
    for e in errors:
        print(f"COULD NOT EVALUATE {e}")
    if not conflicted and not errors:
        print(f"No open PR is conflicted ({len(statuses)} PRs across "
              f"{repo_count} repos).")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as fh:
        if conflicted:
            fh.write("## Conflicted PRs — every `pull_request` check is inactive\n\n")
            fh.write("| repo | PR | base | status written |\n|---|---|---|---|\n")
            for s in conflicted:
                fh.write(f"| `{s['repo']}` | [#{s['number']}]({s['url']}) | "
                         f"`{s['base']}` | `{s['state']}` |\n")
            fh.write("\nGitHub cannot compute a merge ref for these, so **no "
                     "`pull_request` workflow ran on the head sha** — the drift and "
                     "source-of-truth guards are silently inactive. Merge the base "
                     "in; do not touch branch protection.\n")
        if errors:
            fh.write("\n## Could not evaluate\n\n")
            for e in errors:
                fh.write(f"- {e}\n")
            fh.write("\nUnreadable is **not** unconflicted — these were not judged.\n")
        if not conflicted and not errors:
            fh.write(f"No open PR is conflicted ({len(statuses)} PRs across "
                     f"{repo_count} repos).\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", default=str(HERE.parent / "repo-inventory.yml"))
    ap.add_argument("--org", default="tracebloc")
    ap.add_argument("--repo", action="append", default=None,
                    help="sweep only these repos (repeatable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify and report, write no status")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help="re-reads for a PR whose mergeability is UNKNOWN")
    ap.add_argument("--retry-sleep", type=float, default=DEFAULT_RETRY_SLEEP)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    inv = CD.load_inventory(args.inventory)
    names = sorted((inv.get("repos") or {}).keys())
    if args.repo:
        unknown = [r for r in args.repo if r not in names]
        if unknown:
            sys.stderr.write(f"::error::not in the inventory: {', '.join(unknown)}\n")
            return 2
        names = [r for r in names if r in args.repo]

    statuses: "list[dict]" = []
    errors: "list[str]" = []
    for name in names:
        s, e = sweep_repo(args.org, name, args.retries, args.retry_sleep,
                          args.dry_run)
        statuses.extend(s)
        errors.extend(e)

    if args.json:
        print(json.dumps({"statuses": statuses, "errors": errors}, indent=2))
    else:
        render(statuses, errors, len(names), args.dry_run)

    # An unevaluable PR outranks a clean sweep, for the same reason bricked-prs.py
    # ranks them that way: reporting "nothing conflicted" when part of the fleet
    # was never judged is the fail-open being removed.
    if errors:
        return 2
    return 1 if any(s["verdict"] == CONFLICTED for s in statuses) else 0


if __name__ == "__main__":
    raise SystemExit(main())
