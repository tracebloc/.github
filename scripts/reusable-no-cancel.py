#!/usr/bin/env python3
"""A REUSABLE workflow must not set `concurrency.cancel-in-progress: true`.

THE RULE, AND WHY IT IS ABOUT REUSABLES SPECIFICALLY
---------------------------------------------------
`statusCheckRollup.state` aggregates WORST-OF across every check run on a
commit. It does not dedupe to the newest per context -- `gh pr checks` does,
which is where the contrary belief comes from. So one CANCELLED run sits beside
three SUCCESS runs of the same name and the rollup reports FAILURE. That is what
the PR's red X renders from, and what every tool reading the rollup sees,
including our own ship.py.

Cancelling is harmless when the event that supersedes a run also changes the
head sha: the cancelled run then belongs to the OLD commit, and the rollup is
per-commit. It is harmful when the superseding event leaves the sha alone --
`edited`, `labeled`, `unlabeled`, `ready_for_review`, `reopened` -- because then
the corpse lands on the same commit as the winner.

A top-level workflow declares its own `on.pull_request.types` and can therefore
reason about which of those it is exposed to. **A reusable cannot.** Its
`concurrency` block applies to runs driven by callers in other repos, whose
trigger lists it cannot see and does not control. It has no way to tell whether
the event that superseded it changed the sha, so under RFC-1405 property 2 the
only safe answer is to queue: with `cancel-in-progress: false` a second run goes
PENDING until the first finishes and the newer evaluation still writes last.

That buys ORDERING WITHIN THE GROUP, and only that (@saadqbal on #388).
Cancelling also TRUNCATED the superseded run; queuing guarantees it completes.
Both `if: state == 'open'` and the draft read come off the FROZEN event payload,
so a superseded run landing after the closure router has moved a merged card can
still drag it back. Cancelling narrowed that window by accident rather than by
design, and charged a red X on every superseded run for it. Queuing is the better
trade, not a free one.

Measured on `.github#387` (2026-08-30): the release train creates a PR, then
edits its body; `edited` cancelled the in-flight `opened` run, and the PR read
`mergeStateStatus=BLOCKED  rollup=FAILURE` with set-status/closing-ref CANCELLED
as the only non-green contexts. Two reviewers withheld approval over this shape
of stale red on 2026-08-28 (.github#369, client#890) and both were reading the
evidence correctly.

FAIL CLOSED
-----------
"Cannot tell" is a finding, never a pass:
  * a workflow file that will not parse
  * `cancel-in-progress` set to a `${{ }}` expression on a reusable -- its value
    depends on the caller's event, which is exactly the thing a reusable cannot
    see
  * scanning zero workflows at all (a moved directory must not read as clean)
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - guard-pyyaml covers this in the Makefile
    print("::error::pyyaml is required", file=sys.stderr)
    raise SystemExit(1)


def workflow_dir(root: Path) -> Path:
    return root / ".github" / "workflows"


def _on_block(doc: dict):
    """`on:` is parsed by PyYAML as the BOOLEAN True (YAML 1.1 says so)."""
    if True in doc:
        return doc[True]
    return doc.get("on")


def _declares(on, event: str) -> bool:
    """Whether `on:` declares `event`, in ANY of the three legal spellings.

    GitHub accepts all of these, and only the first was recognised (Bugbot on
    .github#388) -- so a reusable written either short way skipped the check
    entirely and reported clean with `cancel-in-progress: true`:

        on: workflow_call            # a bare string
        on: [workflow_call]          # a list
        on:                          # a mapping
          workflow_call:

    A guard that only understands the verbose form is not checking the rule, it
    is checking a formatting convention -- and the short forms are exactly what a
    small reusable is most likely to use.
    """
    if isinstance(on, str):
        return on == event
    if isinstance(on, list):
        return event in on
    if isinstance(on, dict):
        return event in on
    return False


def findings_for(name: str, text: str) -> list[str]:
    """Return this workflow's findings. Pure, so the suite and the mutations
    both drive the real rule rather than a second copy of it (CLAUDE.md rule 9).
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"{name}: does not parse, so its concurrency cannot be read ({exc.__class__.__name__})"]
    if not isinstance(doc, dict):
        return [f"{name}: is not a YAML mapping, so its concurrency cannot be read"]

    on = _on_block(doc)
    if not _declares(on, "workflow_call"):
        return []

    conc = doc.get("concurrency")
    if not isinstance(conc, dict):
        return []
    if "cancel-in-progress" not in conc:
        return []

    value = conc["cancel-in-progress"]
    if isinstance(value, str) and "${{" in value:
        return [
            f"{name}: reusable, and cancel-in-progress is the expression {value!r}. "
            "It resolves against the CALLER's event, which this workflow cannot see -- "
            "'cannot tell' is a finding, not a pass."
        ]
    if value is True:
        return [
            f"{name}: reusable, and sets cancel-in-progress: true. A caller triggering on "
            "edited/labeled/unlabeled/ready_for_review/reopened supersedes a run WITHOUT "
            "changing the head sha, so the cancelled run lands on the same commit as the "
            "winner and statusCheckRollup (worst-of) turns the PR red. Use false: the run "
            "queues and the newer evaluation still writes last. That is ordering within the "
            "group, not a free win -- a queued run completes where a cancelled one was "
            "truncated, so a superseded run can still act on a frozen payload."
        ]
    return []


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    d = workflow_dir(root)
    if not d.is_dir():
        print(f"::error::{d} is not a directory — refusing to report clean from a read I could not make")
        return 1

    files = sorted(d.glob("*.yml")) + sorted(d.glob("*.yaml"))
    if not files:
        print(f"::error::no workflow files under {d} — refusing to report clean on an empty scan")
        return 1

    findings: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as exc:
            findings.append(f"{f.name}: unreadable ({exc.__class__.__name__})")
            continue
        findings.extend(findings_for(f.name, text))

    for finding in findings:
        print(f"::error::{finding}")
    print(f"reusable-no-cancel: scanned {len(files)} workflows, {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
