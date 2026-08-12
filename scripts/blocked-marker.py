#!/usr/bin/env python3
"""Decide whether a pull request declares itself blocked (backend#1729).

WHY THIS EXISTS
---------------
data-ingestors#468 was titled:

    sec(#1528): require DB_USER/DB_PASSWORD, drop the edgeuser fallback (D10)
    [blocked on S2]

It collected THREE approvals and was merged by its author 105 seconds after S2
merged. The author did wait for the blocker they had written down — but "S2
merged" was never the real precondition ("SERVICE_DB_ACCOUNTS on fleet-wide"
was), and nothing anywhere could tell the difference. dev and staging ingestion
broke within hours and stayed broken for a day (backend#1752).

Two other PRs are open right now carrying the same shape in their titles:
`client#490` ("HOLD until v0.8.0 image") and `client-runtime#192`
("DO NOT MERGE: ...").

So: a PR that announces its own blocker should not be mergeable. That is not a
judgement call, it is a string the author already wrote.

WHY A SCRIPT AND NOT A GREP IN THE WORKFLOW
--------------------------------------------
The matching is the whole risk. "unblocked", "blocker", "threshold" and "wipe"
must not fire, or the gate becomes noise and gets switched off — the failure
mode `house-rules.sh` names in its own design constraints. Logic that decides
whether a merge is allowed belongs somewhere it can be unit-tested, not in a
YAML heredoc (backend#1746 is the same lesson from the other direction).

PRECISION OVER RECALL, DELIBERATELY
------------------------------------
This does not try to detect every way a human might say "not yet". It matches
the small set the team actually writes, each anchored so its common
false-friend cannot trigger it. A missed marker costs what we have today; a
false positive costs the gate's credibility, which is worth more.

The LABEL is the precise half: `blocked` already exists org-wide (it is one of
the two stale-exempt labels), it is unambiguous, and it has no false positives
at all. The title patterns are the half that catches the case nobody thought to
label — which is exactly what #468 was.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import List, Tuple

# The label that means this deliberately, already used org-wide.
BLOCKED_LABEL = "blocked"

# Title markers, each with the false friend it must not match.
#
#   blocked on/by/until   NOT "unblocked", NOT "blocker", NOT "unblocks"
#   [blocked]             the bracketed form #468 used
#   do not merge          -
#   hold until/for        NOT "threshold", NOT "holder", NOT "household"
#   wip                   NOT "wipe", NOT "swipe"
#
# `(?<![a-z])` before each keyword is what kills the un- prefixes: `unblocked`
# has `n` immediately before `blocked`, so the lookbehind refuses it. Written
# as an explicit table so a new marker arrives with its own test.
_TITLE_MARKERS: List[Tuple[str, str]] = [
    ("blocked-on", r"(?<![a-z])blocked\s+(?:on|by|until)(?![a-z])"),
    ("blocked-bracket", r"\[\s*blocked\b[^\]]*\]"),
    ("do-not-merge", r"(?<![a-z])do\s+not\s+merge(?![a-z])"),
    ("hold-until", r"(?<![a-z])hold\s+(?:until|for|pending)(?![a-z])"),
    ("wip", r"(?<![a-z])wip(?![a-z])"),
]

_COMPILED = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in _TITLE_MARKERS]


def title_markers(title: str) -> List[str]:
    """Every marker name the title trips, in table order. Empty = clean."""
    return [name for name, rx in _COMPILED if rx.search(title or "")]


def evaluate(title: str, labels: List[str]) -> Tuple[bool, List[str]]:
    """`(blocked, reasons)` for one pull request.

    Labels are matched case-insensitively and whitespace-trimmed: GitHub allows
    `Blocked` and ` blocked ` to be distinct labels, and a gate that only knows
    one spelling is a gate that can be stepped around by accident.
    """
    reasons: List[str] = []
    normalised = {str(name).strip().lower() for name in (labels or [])}
    if BLOCKED_LABEL in normalised:
        reasons.append(f"the {BLOCKED_LABEL!r} label is applied")
    for marker in title_markers(title):
        reasons.append(f"the title matches the {marker!r} marker")
    return bool(reasons), reasons


def _from_event(path: str) -> Tuple[str, List[str]]:
    with open(path, encoding="utf-8") as fh:
        event = json.load(fh)
    pr = event.get("pull_request") or {}
    labels = [lbl.get("name", "") for lbl in (pr.get("labels") or [])
              if isinstance(lbl, dict)]
    return str(pr.get("title") or ""), labels


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--title", default=None, help="PR title (else read the event)")
    ap.add_argument("--label", action="append", default=[], dest="labels")
    ap.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"))
    args = ap.parse_args(argv)

    if args.title is None:
        if not args.event_path:
            print("::error::no --title and no GITHUB_EVENT_PATH — nothing to check",
                  file=sys.stderr)
            return 2
        title, labels = _from_event(args.event_path)
    else:
        title, labels = args.title, args.labels

    blocked, reasons = evaluate(title, labels)
    if not blocked:
        print(f"not blocked: {title!r}")
        return 0

    joined = "; ".join(reasons)
    print(f"::error title=This PR declares itself blocked::{joined}. "
          "Remove the marker when the blocker is genuinely resolved — and check "
          "the real precondition, not the sentence: data-ingestors#468 merged "
          "105 seconds after the PR it named, with the actual precondition "
          "still unmet (backend#1752).")
    for reason in reasons:
        print(f"  - {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
