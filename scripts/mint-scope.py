#!/usr/bin/env python3
"""Every `create-github-app-token` mint must request explicit permissions.

WHY THIS EXISTS (backend#2157)
------------------------------
`actions/create-github-app-token` with no `permission-*` inputs mints the App's
FULL installation grant. For `tracebloc-release-train` that is contents +
pull-requests + issues + organization-projects write across every installed repo --
and the App holds `bypass_reviews` on staging and prod fleet-wide, so the blast
radius of a leaked or misused token is MERGE PAST REVIEW, not merely write.

Nine workflows acquired a full-permission mint in a single day because the first
one did and the rest were copied from it. Least privilege applied by hand is a
state; this is the part that makes it a PROPERTY. Without a check, the twentieth
mint arrives the same way the first nineteen did.

WHAT IT CHECKS, AND WHERE
-------------------------
Only the workflows in THIS repo, and that is the whole surface rather than a
convenient subset: the reusables live here, and the one per-repo COPY
(`add-to-kanban.yml`) is compared byte-for-byte against this repo's copy by
`caller-drift.py`. So a scoped canonical here means scoped everywhere, by
construction rather than by a second sweep. If a second copied workflow ever mints
a token, that reasoning has to be re-checked -- `copies:` in repo-inventory.yml is
where to look.

DERIVED, NEVER RESTATED
-----------------------
The workflow list is the directory. The mint steps are found by parsing YAML and
matching the `uses:` value, not by grepping for names someone typed here: a guard
holding its own copy of the answer agrees with itself while disagreeing with
reality (backend#1729). Add a workflow and it is covered; rename one and nothing
silently stops being checked.

FAIL CLOSED
-----------
An unparseable workflow, an unreadable directory, or ZERO mints found are all
findings. Zero is the important one: this file's whole premise is that mints exist,
so finding none means the matcher broke, not that the fleet got clean.

EXEMPTIONS ARE TEMPORARY, AND STALENESS IS A FINDING
----------------------------------------------------
The reusables that already carry a full grant are exempted BY NAME with a reason,
so this lands GREEN rather than as a red gate nobody can merge past (the rule that
keeps a tier credible).

NO TALLY IN THIS PROSE, and the first draft had one -- it said 13 in two places
while `EXEMPT` held 12 (saadqbal, #287). The 13 was the mint STEP count the audit
reports; 12 of those steps are unscoped and one is already scoped, so the two are
different populations and one of them drifts the moment a row is burnt down. A
hardcoded tally sitting directly above the list it counts is the exact pattern
backend#1729 is cited for, in the file that cites it. The run prints the number
from `len(_exempt())`; that is the only place it should exist. But an exemption for a workflow that no longer
needs one is ALSO reported -- otherwise the list becomes permanent, and a
re-introduced full-grant mint hides behind a row that was written for a different
reason years earlier.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - guarded by `make guard-pyyaml`
    sys.stderr.write("::error::PyYAML is required: python3 -m pip install pyyaml\n")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[1]
# MINT_SCOPE_DIR lets the suite drive this against fixtures; defaults to the real
# directory. Same seam `retro-parent-check.sh` uses for RETRO_FILE -- a guard that
# can only be run against production state is a guard no test can pin.
WORKFLOWS = Path(os.environ.get("MINT_SCOPE_DIR") or (ROOT / ".github" / "workflows"))

# EXEMPT is overridable for the same reason, and ONLY for that reason: a suite that
# had to name real workflows would redden every time one is fixed.
_EXEMPT_OVERRIDE = os.environ.get("MINT_SCOPE_EXEMPT")

# The action whose unscoped use is the finding. Matched on the `uses:` value up to
# the `@`, so a version bump does not silently stop being checked.
MINT_ACTION = "actions/create-github-app-token"

# The input prefix that constitutes "asked for something specific".
SCOPE_PREFIX = "permission-"

# WORKFLOWS THAT MAY STILL CARRY A FULL GRANT, each with the reason it is not fixed
# yet. Burn this down; do not grow it. A row here that is no longer needed is
# reported as a finding (see `stale_exemptions`).
#
# These rows are the pre-existing state this guard was written to stop growing, not
# to fix in one commit. Ten became six (advance-deploy-env, customer-priority-bump,
# fr-pass-comment, kanban-closure-router -- derivable from their call sites), six
# became four, four became two, and two became one.
#
# THE TWO JUST BURNT DOWN WERE THE ONES THAT COULD NOT BE DERIVED, and the reason
# they could not is worth keeping (backend#2157, measured 2026-08-24). Their rows
# said a narrower token may return LESS rather than erroring -- a 200 with a field
# missing, which a checker reads as "not configured" on a green run. That was the
# right worry, so it was measured rather than argued: token scopes minted against
# the live endpoints, then each workflow's OWN script run at the candidate scope and
# compared to the full grant, then each permission removed one at a time to prove it
# was load-bearing. What came back was not what either row predicted:
#
#   bricked-prs -- branch protection does NOT degrade silently. Without
#     `administration: read` it answers 403, which read_protection() reports as an
#     error, so the audit fails closed exactly as designed. It narrowed to five READ
#     scopes, output identical to the full grant.
#   merge-settings-drift -- reads no branch protection at all; the row was wrong
#     about which endpoint it even used. It reads `GET /repos/{o}/{r}`, and THAT is
#     the endpoint that degrades silently: the four merge-setting fields are
#     returned only to a caller with PUSH access, absent otherwise, 200 either way.
#     `contents: write` is the one grant it measurably cannot lose -- the same grant
#     this ticket set out to delete everywhere.
#
# So the silent-degradation risk was real, but attached to the other workflow. That
# is the argument for measuring rather than reasoning, and it is why the four below
# say what they say.
#
# The ONE left is left for a STATED reason, not for lack of time:
#
#   standards-sync -- a contents:write sweep across the fleet, and the only one of
#     the four whose risky mode cannot be rehearsed without opening 19 real PRs.
#     It needs its own window so one bad scope does not redden the whole fleet at
#     once. set-pr-status, kanban-reconcile and fr-gate were the other three and
#     are now scoped; their windows were taken.
#
# The distinction matters because it is the difference between an exemption that is
# a to-do and one that is a decision. Do not fold them back together.
EXEMPT = {
    "standards-sync.yml": "writes CLAUDE.md across the fleet; contents:write is real here",
}


def _exempt() -> dict:
    """The live exemption map, or the suite's override.

    Read through a function rather than mutating the module global, so the
    production dict below stays the single written-down answer and a test cannot
    leave it modified for the next case.
    """
    if _EXEMPT_OVERRIDE is None:
        return EXEMPT
    return {name.strip(): "test override" for name in _EXEMPT_OVERRIDE.split(",") if name.strip()}


class Finding(Exception):
    pass


def mint_steps(doc) -> "list[dict]":
    """Every step in `doc` whose `uses:` is the mint action, at any job depth."""
    out = []
    if not isinstance(doc, dict):
        return out
    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            uses = step.get("uses")
            if isinstance(uses, str) and uses.split("@", 1)[0].strip() == MINT_ACTION:
                out.append(step)
    return out


def scoped(step: dict) -> bool:
    """True when the step names at least one `permission-*` input.

    Presence, not correctness. Whether the scopes are the RIGHT ones is a question
    only the workflow's own API calls can answer, and a guard that pretended to
    know would be restating rather than deriving. What this can decide -- and what
    was actually going wrong -- is "asked for nothing at all".
    """
    with_ = step.get("with")
    if not isinstance(with_, dict):
        return False
    return any(isinstance(k, str) and k.startswith(SCOPE_PREFIX) for k in with_)


def audit() -> "tuple[list[str], list[str], int]":
    if not WORKFLOWS.is_dir():
        raise Finding(f"{WORKFLOWS} is not a directory -- refusing to report clean")
    files = sorted(p for p in WORKFLOWS.iterdir() if p.suffix in (".yml", ".yaml"))
    if not files:
        raise Finding(f"no workflow files under {WORKFLOWS} -- refusing to report clean")

    unscoped, minted = [], 0
    for path in files:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            # NOT a skip. An unreadable workflow is a workflow we cannot clear.
            raise Finding(f"{path.name} could not be parsed ({exc}) -- refusing to report clean")
        for step in mint_steps(doc):
            minted += 1
            if not scoped(step):
                unscoped.append(path.name)

    if minted == 0:
        # The premise of this file is that mints exist. None found means the matcher
        # broke -- an action rename, a `uses:` moved behind an expression -- not that
        # the fleet got clean. Reporting green here is the failure this guard exists
        # to prevent, one level in.
        raise Finding(
            f"no `{MINT_ACTION}` step found in any workflow. Either the action was "
            "renamed or the matcher is broken; a guard that finds nothing to check "
            "must not report success")

    return sorted(set(unscoped)), sorted(_exempt()), minted


def stale_exemptions(unscoped: "list[str]") -> "list[str]":
    """Exempted workflows that no longer carry an unscoped mint.

    An exemption list nobody prunes stops being a burn-down and becomes cover: a
    full-grant mint re-introduced into an exempted workflow would be admitted by a
    row written for a different reason. So a row that is no longer needed is
    reported too.
    """
    return sorted(set(_exempt()) - set(unscoped))


def main() -> int:
    try:
        unscoped, _, minted = audit()
    except Finding as exc:
        sys.stderr.write(f"::error::{exc}\n")
        return 2

    offenders = [w for w in unscoped if w not in _exempt()]
    stale = stale_exemptions(unscoped)

    print(f"mint-scope: {minted} `{MINT_ACTION}` step(s) across "
          f"{len(sorted(WORKFLOWS.iterdir()))} workflow file(s)")
    print(f"  {len(unscoped)} unscoped, {len(_exempt())} exempted, {len(offenders)} finding(s)")

    rc = 0
    for w in offenders:
        sys.stderr.write(
            f"::error file=.github/workflows/{w}::{w} mints an App token with no "
            f"`{SCOPE_PREFIX}*` inputs, so it carries the App's FULL installation "
            "grant. Name the scopes the workflow's own API calls need, or add it to "
            "EXEMPT in scripts/mint-scope.py with a reason.\n")
        rc = 1
    for w in stale:
        sys.stderr.write(
            f"::error::{w} is listed in EXEMPT but no longer mints an unscoped "
            "token. Remove the row -- a stale exemption is cover for the next "
            "unscoped mint in that file.\n")
        rc = 1
    if rc == 0:
        print("  no findings (exemptions all still apply)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
