#!/usr/bin/env python3
"""A citation inside a repo-inventory exemption reason must still be live.

WHY THIS EXISTS (tracebloc/backend#2449)
----------------------------------------
`repo-inventory.yml` entries carry a free-prose `reason`. `caller-drift.py`
enforces staleness on the ENTRY -- an exemption whose caller turned up is a
finding (`caller-drift.py:2309`) -- and NOTHING reads the reason. So the entry
stays legitimately exempt, the audit stays green, and the sentence a human reads
before deciding whether the exemption still applies can be false for as long as
nobody re-reads it.

Measured false twice on 2026-08-24, independently:

  * `customer_priority_bump_caller_missing` said the wiring was "sequenced
    behind" backend#1408. That issue closed COMPLETED on 2026-08-06. Three repos
    sat behind a sentence describing a ticket that had been shut for eighteen
    days; the wiring went ahead the moment somebody read the ticket instead of
    the reason.
  * `rfcs`' `advance-deploy-env.yml` reason said "this repo has no `develop`".
    It has one.

WHAT THIS CHECKS, AND WHAT IT DELIBERATELY DOES NOT
---------------------------------------------------
ONE of the three mechanisms backend#2449 sketches: **where a reason names an
issue, fail when that issue is CLOSED.** The reason may well still be valid -- a
closed citation is not proof of anything -- but it is exactly the case a human
has to re-read, and it is checkable from the citation alone. That alone would
have caught case 1 the day #1408 closed.

The other two are NOT built here, on purpose:

  (2) date expiry -- "a reason older than N days is a finding" -- needs a
      measurement date on every reason first, which is an edit to
      repo-inventory.yml and a schema decision, not a checker.
  (3) "assert the falsifiable half" -- decide whether a reason states a FACT or
      a JUDGEMENT and check the facts -- is a much larger design problem: it
      needs prose understood well enough to know which half is falsifiable, and
      the wrong answer either invents findings or teaches people to phrase
      reasons so the check cannot see them.

Splitting them keeps this one small enough to be obviously correct.

DERIVED, NEVER RESTATED (CLAUDE.md rule 1)
------------------------------------------
The citations come from PARSING the inventory: every `exempt:` string, every
`divergent:`'s `reason:`, and every anchor body in `shared_reasons:`. There is no
hand-written list here of which reason cites which ticket -- a checker holding
its own copy of the answer agrees with itself while disagreeing with reality, and
this very file warns about that in prose ("an anchor cannot state its own reach
-- trust `grep` over this sentence"). Write a new reason citing a new ticket and
it is covered the moment it lands.

COMMENTS ARE NOT REASONS. The scan runs over the PARSED YAML, so the ~40 ticket
numbers in this file's header comments are out of scope. They are documentation
about the file, not the written justification for an exemption, and nothing reads
them to decide whether an exemption still applies.

WHICH STATES ARE FINDINGS, AND WHY A MERGED PR IS NOT ONE
----------------------------------------------------------
GitHub answers "issue or pull request" in one field, so the distinction is READ,
not assumed:

  Issue OPEN          fine
  Issue CLOSED        FINDING -- re-read the reason (this is backend#2449 case 1)
  PullRequest OPEN    fine
  PullRequest MERGED  fine
  PullRequest CLOSED  FINDING -- a plan that never landed

A merged PR is the one closed thing whose terminal state is SUCCESS. Reasons cite
PRs as provenance ("remediated under model-zoo#115", "Bugbot, .github#196"), and
that sentence stays true forever. Flagging them would have made 9 of the 23 live
citations findings on day one for describing history correctly -- noise that
teaches people to stop reading the report, which is the failure mode a gate can
least afford. A CLOSED-unmerged PR is the opposite: the reason is leaning on
something that did not happen.

WHICH REPO A CITATION MEANS -- STATED, NOT ASSUMED
---------------------------------------------------
  `owner/repo#N`  as written.
  `repo#N`        `<org>/repo#N`, org read from this inventory's own `org:` key.
  `#N`            `<org>/<source_repo>#N` -- the repo this inventory LIVES IN,
                  which is what GitHub itself renders a bare `#N` as in a file in
                  that repo. Both values come from the inventory (`org:`,
                  `source_repo:`); neither is typed here.

That last rule is the one worth stating out loud, because guessing it wrong is a
defect this org has already shipped and fixed: `closing-ref-gate.py` resolved a
bare number against `backend` and so advised `Closes tracebloc/backend#N` on a
`release-train` PR (.github#314). So this file does not assume `backend`, and it
does not stay quiet either -- when `GITHUB_REPOSITORY` is set and disagrees with
`<org>/<source_repo>`, the premise of the bare rule is false and a bare citation
becomes "cannot tell" rather than a guess.

FAIL CLOSED (rule 3), AND "CANNOT TELL" IS A FINDING ABOUT THE CHECK
---------------------------------------------------------------------
Exit 2, never a pass: an unreadable or unparseable inventory, a missing `org:` or
`source_repo:`, a `gh` call that fails, a GraphQL response that is not JSON, a
citation the API will not resolve (404, 403, rate limit), and ZERO CITATIONS
FOUND. Zero is the important one and the reason it is checked at all: this file's
whole premise is that reasons cite tickets, so finding none means the MATCHER
broke, not that the inventory got clean.

Exit 1 is a real finding about the inventory. Both fail; they are separated so a
red run says whether to fix a reason or fix this script.

EXEMPTIONS ARE TEMPORARY, AND STALENESS IS A FINDING
-----------------------------------------------------
Same shape as `mint-scope.py`'s `EXEMPT`, for the same reason: this lands GREEN
over the citations that were ALREADY dead when it was written, rather than as a
red gate in a REQUIRED context that nobody can merge past (rule 4 -- never land a
red gate). And the other half is what stops the list becoming cover:
a row whose citation is no longer a finding -- reopened, or edited out of the
inventory -- is itself reported, so the list has to be pruned.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - guarded by `make guard-pyyaml`
    sys.stderr.write("::error::PyYAML is required: python3 -m pip install pyyaml\n")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[1]

# The suite drives this against fixture inventories. Same seam `mint-scope.py`
# uses for MINT_SCOPE_DIR: a guard that can only run against production state is
# a guard no test can pin.
INVENTORY = Path(os.environ.get("REASON_CITATIONS_INVENTORY") or (ROOT / "repo-inventory.yml"))

# Overridable for the same reason and ONLY that reason: a suite that had to name
# the real exempt rows would redden every time one is burnt down.
_EXEMPT_OVERRIDE = os.environ.get("REASON_CITATIONS_EXEMPT")

# WHICH INVENTORY KEYS CARRY A WRITTEN REASON. Taken from `caller-drift.py`'s
# schema, which accepts THREE spellings and this org uses all three:
#   `exempt: "<reason>"`               every family (`_reason_entry`)
#   `divergent: "<reason>"`            copies (`_reason_entry`)
#   `divergent: {reason: "...", ...}`  protection (`_protection_entry`)
# All three are read, so a reason cannot escape the scan by being written in
# another one of them -- which `divergent` nearly did: the first draft here read
# only `exempt` and `reason`, and the bare-string `divergent` at line 1159 of the
# inventory would have gone unscanned.
REASON_KEYS = ("exempt", "divergent", "reason")

# The anchor: a `#` followed by digits is somebody citing a ticket. Everything
# before it is then parsed as a repo, rather than the repo being part of the
# match -- so `owner/repo/extra#5` is REPORTED as malformed instead of quietly
# matching its tail, and a bare `#5` is seen rather than skipped.
ANCHOR_RE = re.compile(r"#(\d+)")

# The characters a citation prefix may be built from, scanned leftwards from the
# `#`. `.` is in the set because `.github` is a real repo name in this org and an
# alnum-first class silently dropped every citation to it.
PREFIX_CHARS = re.compile(r"[A-Za-z0-9._/-]")

# A fully-formed prefix: `owner/repo` or `repo`. GitHub owners are alnum + `-`;
# repo names additionally allow `.` and `_`. Anything else is malformed, INCLUDING
# a second slash -- this is also what keeps the repo name safe to interpolate into
# the GraphQL document below.
PREFIX_RE = re.compile(r"\A(?:([A-Za-z0-9][A-Za-z0-9-]*)/)?([A-Za-z0-9.][A-Za-z0-9._-]*)\Z")

# CITATIONS THAT WERE ALREADY CLOSED WHEN THIS GUARD LANDED (2026-08-24), each
# with what it is doing in the inventory. Burn this down; do not grow it.
#
# This is the state the check was written to stop GROWING, not to fix in the
# commit that adds the check -- backend#2449 says so explicitly, because
# re-reading these reasons is its own work and rewriting them here would bury the
# mechanism in a prose diff. Every row below is a citation to a CLOSED issue that
# a human still has to judge.
#
# NO TALLY IN THIS COMMENT, deliberately. `mint-scope.py` shipped with one and it
# said 13 in two places while the list held 12 (saadqbal, #287); the run prints
# the number from `len(_exempt())`, which is the only place it should exist.
#
# TWO OF THEM ARE THE TICKET'S OWN CASE 1, FOUND ON THE FIRST RUN, and they are
# marked UNREMEDIATED rather than explained away. Both lean FORWARD on a ticket
# that is shut -- the same shape as the backend#1408 sentence that held three
# repos for eighteen days. Do not let the row read as permission.
#
# The rest are cited in the PAST TENSE, as the ticket under which something
# already landed or was already decided. That distinction is a judgement about
# prose, which is why it is written here as a note to whoever burns these down
# and is NOT something this script tries to infer (see mechanism (3) in the
# header).
EXEMPT = {
    "tracebloc/backend#1680": (
        "PROVENANCE, re-read 2026-08-28 and still true. `.github`'s develop and "
        "staging protection reasons cite it for WHY this repo requires MORE than "
        "the fleet baseline -- #1680 is where arming `selftests` on top of it was "
        "decided. The epic was CLOSED COMPLETED at 07:51Z on 2026-08-28, which is "
        "what this guard flags, and that is right for a citation that might be a "
        "live dependency. This one is not: it is past-tense history, and the claim "
        "it supports is independently verifiable -- `selftests` IS a required "
        "context on BOTH branches today (measured, alongside `gate` on develop and "
        "`gate` + `gate / gate` on staging). Recorded here rather than reworded out "
        "of the reasons, because dropping the number to silence the guard would "
        "lose the traceability the reasons exist to give."
    ),
    "tracebloc/backend#2347": (
        "PROVENANCE, re-read 2026-08-26 and still true. `claude-skills`' fr-gate "
        "reason cites it for WHY the caller was removed -- #2347 decided the three "
        "non-train repos are working repos, and claude-skills#38 carried that out "
        "(+0/-15). The issue is CLOSED COMPLETED, which is what this guard flags, "
        "and that is exactly right for a citation that might be a live dependency. "
        "This one is not: it is past-tense history and stays true. Recorded here "
        "rather than reworded out of the reason, because dropping the number to "
        "silence the guard would lose the traceability the reason exists to give."
    ),
    "tracebloc/backend#1408": (
        "already re-read, and the prose says so: `stale_backlog_exemption_needs_redeciding` "
        "states that this ticket's basis is gone and records the exemption as UNDECIDED"
    ),
    "tracebloc/backend#1415": (
        "UNREMEDIATED -- `wip_limit_check_has_no_callers` defers to it in the FUTURE tense "
        "(\"the decision to wire it up or delete it is backend#1415 follow-up work\") and the "
        "ticket is closed. Backend#2449 case 1 exactly; found by this guard's first run"
    ),
    "tracebloc/backend#1729": (
        "UNREMEDIATED -- `blocked_gate_rollout_pending` says \"the callers follow in the "
        "rollout PR (backend#1729)\" and the ticket is closed, so the rollout it is waiting on "
        "is not tracked by anything open. Backend#2449 case 1 shape"
    ),
    "tracebloc/backend#1276": (
        "cited as the decision record its D-numbers are quoted from; closed-completed is a "
        "decision record's terminal state"
    ),
    "tracebloc/backend#1420": "cited in the past tense: the ticket two repos were remediated under",
    "tracebloc/backend#1816": "cited in the past tense: the ticket that made add-to-kanban v2.0.0 the fleet norm",
    "tracebloc/backend#1563": "cited in the past tense: the ticket version-bump-pr.yml was deleted under",
    "tracebloc/backend#1752": (
        "cited in the past tense, as the provenance of the two PRs blocked-gate would have reddened"
    ),
    "tracebloc/backend#1975": "cited in the past tense: the ticket that armed frontend-app's Vitest contexts",
    "tracebloc/backend#1976": "cited in the past tense: the ticket that raised the action-pins baseline",
    "tracebloc/backend#1979": "cited in the past tense: the ticket that removed stale-backlog's column-blindness",
    "tracebloc/backend#2243": "cited in the past tense: the ticket that flipped release-train to `required`",
    "tracebloc/client-runtime#192": (
        "THE DEADNESS IS THE CONTENT. `blocked_gate_rollout_pending` names this PR as one of "
        "two that blocked-gate would have reddened when the anchor was written. It was closed "
        "unmerged on 2026-08-26, and the reason now SAYS so -- it does not lean on the PR "
        "landing, it records that it did not. That is the opposite of the leaning-on-vapour "
        "shape this guard exists to catch, so the citation stays and keeps the provenance. "
        "If the reason is ever reworded back into the present tense, this exemption stops "
        "describing it and should be deleted with the sentence"
    ),
}

# The GraphQL field that answers "issue or PR?" in one read. `state` is
# OPEN/CLOSED on an Issue and OPEN/CLOSED/MERGED on a PullRequest, which is the
# whole reason a merged PR can be told apart from an abandoned one.
NODE_FIELDS = (
    "{ __typename ... on Issue { state } ... on PullRequest { state } }"
)


class Finding(Exception):
    """A malfunction of the check, or something it cannot tell. Always exit 2."""


def _exempt() -> dict:
    """The live exemption map, or the suite's override.

    Read through a function rather than mutating the module global, so the map
    above stays the single written-down answer and a case cannot leave it
    modified for the next one.
    """
    if _EXEMPT_OVERRIDE is None:
        return EXEMPT
    return {c.strip(): "test override" for c in _EXEMPT_OVERRIDE.split(",") if c.strip()}


# ------------------------------------------------------------------ parsing ---


def load_inventory(path: Path) -> dict:
    """The inventory as a mapping. Anything else is a finding about the check."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Finding(f"{path} could not be read ({exc}) -- refusing to report clean")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise Finding(f"{path} could not be parsed ({exc}) -- refusing to report clean")
    if not isinstance(doc, dict):
        raise Finding(f"{path} did not parse to a mapping -- refusing to report clean")
    return doc


def reason_strings(doc: dict) -> "list[tuple[str, str]]":
    """Every written reason in the inventory, as (where, text).

    Two sources, because a reason can be written in either place and the guard
    must not be escapable by choosing the other:

      * `shared_reasons:` -- the anchor bodies. Walking `repos:` alone would find
        every ALIASED one (safe_load resolves an alias to the same string), but
        an anchor defined and not yet referenced would be invisible, and that is
        the state a reason is in on the PR that introduces it.
      * `repos:` -- every `exempt:` string and every `divergent:`'s `reason:`, at
        whatever depth, so a new property FAMILY is covered without an edit here.
    """
    out = []
    shared = doc.get("shared_reasons")
    if isinstance(shared, dict):
        for name, text in shared.items():
            if isinstance(text, str):
                out.append((f"shared_reasons.{name}", text))

    def walk(node, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in REASON_KEYS and isinstance(value, str):
                    out.append((f"{where}.{key}", value))
                else:
                    walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{where}[{i}]")

    walk(doc.get("repos"), "repos")
    return out


class Citation:
    """One `#N` an author wrote, and what could be made of what precedes it.

    `legal` is False when a prefix WAS written and is not a valid `owner/repo` --
    reported as malformed rather than dropped, so a typo cannot make a citation
    invisible to the check. That is the difference between this and a regex that
    matches only well-formed citations: the well-formed regex is quiet about
    exactly the input a human needs to see.
    """

    __slots__ = ("owner", "repo", "number", "raw", "legal")

    def __init__(self, owner, repo, number, raw, legal):
        self.owner, self.repo, self.number = owner, repo, number
        self.raw, self.legal = raw, legal


def parse_citations(text: str) -> "list[Citation]":
    """Every ticket citation in one reason."""
    out = []
    for match in ANCHOR_RE.finditer(text):
        start = match.start()
        i = start
        while i > 0 and PREFIX_CHARS.match(text[i - 1]):
            i -= 1
        prefix = text[i:start]
        raw = prefix + match.group(0)
        number = int(match.group(1))
        if not prefix:
            out.append(Citation(None, None, number, raw, True))
            continue
        parsed = PREFIX_RE.match(prefix)
        if parsed is None:
            out.append(Citation(None, None, number, raw, False))
            continue
        out.append(Citation(parsed.group(1), parsed.group(2), number, raw, True))
    return out


def host(doc: dict) -> "tuple[str, str]":
    """(org, repo-this-inventory-lives-in), both READ from the inventory.

    `source_repo` is the inventory's own name for the repo that hosts the
    reusables, which is the repo this file sits in. Nothing here types
    "tracebloc" or ".github" -- move the inventory and the bare-citation rule
    moves with it, or fails loudly (see `resolve`).
    """
    org, src = doc.get("org"), doc.get("source_repo")
    if not isinstance(org, str) or not org.strip():
        raise Finding("the inventory declares no `org:` -- cannot resolve any citation")
    if not isinstance(src, str) or not src.strip():
        raise Finding(
            "the inventory declares no `source_repo:` -- cannot resolve a bare `#N`, "
            "and guessing one is the defect .github#314 fixed"
        )
    return org.strip(), src.strip()


def resolve(owner, repo, number: int, raw: str, org: str, source_repo: str) -> str:
    """Canonical `owner/repo#N`, or a Finding when it cannot be resolved."""
    if repo is None:
        # A bare `#N`. The rule is GitHub's own -- this repo -- and it is only
        # sound while this file actually lives in `<org>/<source_repo>`. When the
        # runner tells us otherwise, say so instead of resolving it anyway.
        here = os.environ.get("GITHUB_REPOSITORY")
        if here and here.strip().lower() != f"{org}/{source_repo}".lower():
            raise Finding(
                f"{raw!r} is a bare citation, which this check resolves against the repo the "
                f"inventory lives in ({org}/{source_repo} per `source_repo:`). GITHUB_REPOSITORY "
                f"says {here!r}, so that premise is false and the repo cannot be told. Write "
                "`owner/repo#N` in the reason."
            )
        repo = source_repo
    return f"{owner or org}/{repo}#{number}"


# --------------------------------------------------------------------- reads ---


def _run_gh(args, env):
    return subprocess.run(args, capture_output=True, text=True, env=env, check=False)


def fetch_states(keys: "list[str]", env=None, runner=_run_gh) -> "dict[str, tuple[str, str]]":
    """(typename, state) per citation, in ONE GraphQL call.

    One request rather than one per citation on purpose: this runs inside a
    REQUIRED context on every PR, and N sequential API calls is N chances for a
    blip to fail a merge closed.

    ANY failure raises. A partial read is not a clean read -- GitHub answers a
    bad alias with a null node AND an `errors[]` entry while still returning 200
    and data for the others, so a caller that only looked at the exit code would
    silently score an unresolvable citation as fine.
    """
    env = dict(os.environ if env is None else env)
    parts = []
    for i, key in enumerate(keys):
        repo_part, _, number = key.rpartition("#")
        owner, _, name = repo_part.partition("/")
        parts.append(
            f'c{i}: repository(owner: "{owner}", name: "{name}") '
            f"{{ issueOrPullRequest(number: {int(number)}) {NODE_FIELDS} }}"
        )
    query = "query {" + " ".join(parts) + "}"
    proc = runner(["gh", "api", "graphql", "-f", "query=" + query], env)
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError):
        raise Finding(
            f"the GraphQL read returned no JSON (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise Finding(
            f"the GraphQL response carried no data (exit {proc.returncode}): "
            f"{json.dumps(payload.get('errors'))[:300]}"
        )
    out = {}
    for i, key in enumerate(keys):
        repo = data.get(f"c{i}")
        node = repo.get("issueOrPullRequest") if isinstance(repo, dict) else None
        if not isinstance(node, dict) or not node.get("__typename") or not node.get("state"):
            # 404, 403, a rate limit, a renamed repo: all the same answer, which
            # is CANNOT TELL. Never "open".
            raise Finding(
                f"{key} could not be read (no issue or pull request came back). That is "
                "'cannot tell', not 'still open' -- fix the citation or the token's reach"
            )
        out[key] = (node["__typename"], node["state"])
    return out


# ------------------------------------------------------------------ verdicts ---

# (typename, state) pairs that are NOT a finding. Written down as the whole
# allowed set rather than as "not closed", so a state GitHub adds later lands in
# the unknown branch below and is refused rather than silently passing.
LIVE = {
    ("Issue", "OPEN"),
    ("PullRequest", "OPEN"),
    ("PullRequest", "MERGED"),
}
DEAD = {
    ("Issue", "CLOSED"): "the issue is CLOSED -- re-read the reason that cites it",
    ("PullRequest", "CLOSED"): "the pull request was CLOSED WITHOUT MERGING -- the reason "
                               "leans on something that never landed",
}


def classify(state: "tuple[str, str]") -> "str | None":
    """None when the citation is live, else why it is a finding."""
    if state in LIVE:
        return None
    if state in DEAD:
        return DEAD[state]
    raise Finding(
        f"unrecognised citation state {state!r}. GitHub returned something this check has "
        "no verdict for; refusing to guess whether it is live"
    )


def audit(env=None, runner=_run_gh):
    """Returns (findings, malformed, citations, reasons_scanned).

    `findings` and `malformed` are lists of (key, why, [where, ...]).
    """
    doc = load_inventory(INVENTORY)
    org, source_repo = host(doc)
    reasons = reason_strings(doc)

    seen: "dict[str, list[str]]" = {}
    malformed: "dict[str, list[str]]" = {}
    for where, text in reasons:
        for c in parse_citations(text):
            # `#0` is nobody's issue: GitHub numbers from 1. A citation that
            # cannot name a real ticket is a defect in the prose, reported rather
            # than sent to the API to 404.
            if not c.legal or c.number < 1:
                malformed.setdefault(c.raw, []).append(where)
                continue
            key = resolve(c.owner, c.repo, c.number, c.raw, org, source_repo)
            seen.setdefault(key, []).append(where)

    if not seen and not malformed:
        # The premise of this file is that reasons cite tickets. None found means
        # the matcher broke -- a schema change, a key rename, a reason moved --
        # not that the inventory got clean.
        raise Finding(
            f"no ticket citation found in any of the {len(reasons)} written reason(s) in "
            f"{INVENTORY.name}. Either the schema moved or the matcher is broken; a check "
            "that finds nothing to check must not report success"
        )

    states = fetch_states(sorted(seen), env=env, runner=runner) if seen else {}
    findings = []
    for key in sorted(seen):
        why = classify(states[key])
        if why:
            findings.append((key, why, sorted(set(seen[key]))))
    bad = [(raw, "not a legal `owner/repo#N`, `repo#N` or `#N` citation", sorted(set(w)))
           for raw, w in sorted(malformed.items())]
    return findings, bad, seen, len(reasons)


def stale_exemptions(findings) -> "list[str]":
    """Exempted citations that are no longer a finding.

    An exemption list nobody prunes stops being a burn-down and becomes cover: a
    reason rewritten to cite a NEWLY closed ticket would be admitted by a row
    written years earlier about a different one. So a row that is no longer
    needed is reported too.
    """
    return sorted(set(_exempt()) - {key for key, _, _ in findings})


def main() -> int:
    try:
        findings, malformed, citations, reasons = audit()
    except Finding as exc:
        sys.stderr.write(f"::error::{exc}\n")
        return 2

    exempt = _exempt()
    offenders = [f for f in findings if f[0] not in exempt] + \
                [m for m in malformed if m[0] not in exempt]
    stale = stale_exemptions(findings)

    print(f"reason-citations: {len(citations)} distinct citation(s) across "
          f"{reasons} written reason(s) in {INVENTORY.name}")
    print(f"  {len(findings)} dead, {len(malformed)} malformed, {len(exempt)} exempted, "
          f"{len(offenders)} finding(s)")

    rc = 0
    for key, why, where in offenders:
        sys.stderr.write(
            f"::error file={INVENTORY.name}::{key} is cited by a repo-inventory reason and "
            f"{why}. Re-read the reason and either restate it against something still true, "
            f"or add {key} to EXEMPT in scripts/reason-citations.py with what it is doing "
            f"there. Cited by: {', '.join(where[:4])}"
            f"{' (+%d more)' % (len(where) - 4) if len(where) > 4 else ''}\n"
        )
        rc = 1
    for key in stale:
        sys.stderr.write(
            f"::error::{key} is listed in EXEMPT but is no longer a finding -- it reopened, or "
            "no reason cites it any more. Remove the row: a stale exemption is cover for the "
            "next dead citation that lands in the same reason.\n"
        )
        rc = 1
    if rc == 0:
        print("  no findings (every citation is live, and every exemption still applies)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
