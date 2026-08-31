#!/usr/bin/env python3
"""Suite for scripts/reusable-no-cancel.py (tracebloc/backend#2756).

The rule: a REUSABLE workflow must not cancel in-progress runs, because it
cannot see whether its callers' trigger events change the head sha, and a
cancelled run on the SAME sha turns statusCheckRollup (worst-of) red.

FIXTURES, NOT THE LIVE WORKFLOWS. A suite that asserted against
.github/workflows/ would redden every time an unrelated workflow gained a
concurrency block, and would silently stop testing the rule the day the last
offender was fixed. The live tree is checked by `make reusable-no-cancel`; this
checks that the RULE catches.

INPUTS ARE WRITTEN DOWN INDEPENDENTLY OF THE MATCHER (CLAUDE.md rule 9's
corollary). Every fixture below is a literal. Generating them from the guard's
own constants would be self-consistent and therefore blind.

Each case pins a behaviour a mutation would break; reusable-no-cancel-mutations.py
breaks each and asserts this suite reddens.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE.parent / "reusable-no-cancel.py"

spec = importlib.util.spec_from_file_location("reusable_no_cancel", GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

RESULTS = []


def record(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))


def case(name: str, text: str, *, expect_finding: bool, must_mention: str = "") -> None:
    found = guard.findings_for("fixture.yml", text)
    ok = bool(found) == expect_finding
    if ok and must_mention:
        ok = any(must_mention in f for f in found)
    record(
        ok,
        name,
        f"expected {'a finding' if expect_finding else 'clean'}, got {len(found)}: "
        + ("; ".join(f[:110] for f in found) or "clean"),
    )


# --- the offender ----------------------------------------------------------
case(
    "a reusable that cancels is a finding",
    """
name: Set PR status
on:
  workflow_call:
concurrency:
  group: set-pr-status-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  x:
    runs-on: ubuntu-latest
""",
    expect_finding=True,
    must_mention="cancel-in-progress: true",
)

# --- the fix ---------------------------------------------------------------
case(
    "a reusable that queues is clean",
    """
name: Set PR status
on:
  workflow_call:
concurrency:
  group: set-pr-status-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false
jobs:
  x:
    runs-on: ubuntu-latest
""",
    expect_finding=False,
)

# --- the distinction the rule rests on -------------------------------------
# A top-level workflow OWNS its `types:` list, so it can reason about whether an
# event changes the sha. Flagging it here would be a different (and much larger)
# rule, and asserting the boundary keeps a later broadening honest.
case(
    "a NON-reusable that cancels is NOT this rule's business",
    """
name: Actionlint
on:
  pull_request:
    types: [opened, synchronize, ready_for_review]
concurrency:
  group: actionlint-${{ github.ref }}
  cancel-in-progress: true
jobs:
  x:
    runs-on: ubuntu-latest
""",
    expect_finding=False,
)

case(
    "a reusable with no concurrency block is clean",
    """
name: Blocked gate
on:
  workflow_call:
jobs:
  x:
    runs-on: ubuntu-latest
""",
    expect_finding=False,
)

case(
    "a reusable whose concurrency omits cancel-in-progress is clean",
    """
name: Blocked gate
on:
  workflow_call:
concurrency:
  group: g
jobs:
  x:
    runs-on: ubuntu-latest
""",
    expect_finding=False,
)

case(
    "a reusable that also accepts other triggers is still a reusable",
    """
name: Mixed
on:
  workflow_call:
  workflow_dispatch:
concurrency:
  group: g
  cancel-in-progress: true
jobs:
  x:
    runs-on: ubuntu-latest
""",
    expect_finding=True,
)

# --- the short `on:` forms (Bugbot on .github#388) -------------------------
# GitHub accepts three spellings and only the verbose one was recognised, so a
# reusable written either short way skipped the check and reported clean. These
# are literals rather than generated from the guard's own parser, for rule 9's
# corollary: a fixture derived from the matcher agrees with it by construction.
case(
    "a reusable declared as a bare STRING is still checked",
    """
name: Set PR status
on: workflow_call
concurrency:
  group: g
  cancel-in-progress: true
""",
    expect_finding=True,
    must_mention="cancel-in-progress: true",
)

case(
    "a reusable declared in a LIST is still checked",
    """
name: Set PR status
on: [workflow_call]
concurrency:
  group: g
  cancel-in-progress: true
""",
    expect_finding=True,
    must_mention="cancel-in-progress: true",
)

case(
    "a LIST form that queues is clean",
    """
name: Set PR status
on: [workflow_call, workflow_dispatch]
concurrency:
  group: g
  cancel-in-progress: false
""",
    expect_finding=False,
)

case(
    "a bare-STRING non-reusable trigger is not this rule's business",
    """
name: Something
on: push
concurrency:
  group: g
  cancel-in-progress: true
""",
    expect_finding=False,
)

case(
    "a LIST form without workflow_call is not this rule's business",
    """
name: Something
on: [push, pull_request]
concurrency:
  group: g
  cancel-in-progress: true
""",
    expect_finding=False,
)

# --- fail closed -----------------------------------------------------------
case(
    "an EXPRESSION on a reusable is a finding, because it resolves on the caller",
    """
name: Set PR status
on:
  workflow_call:
concurrency:
  group: g
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
jobs:
  x:
    runs-on: ubuntu-latest
""",
    expect_finding=True,
    must_mention="cannot see",
)

case(
    "a workflow that does not parse is a finding, not a pass",
    "name: broken\non:\n  workflow_call:\nconcurrency:\n  group: [unclosed\n",
    expect_finding=True,
    must_mention="does not parse",
)

case(
    "a non-mapping document is a finding, not a pass",
    "- just\n- a\n- list\n",
    expect_finding=True,
)

# `on:` is the YAML 1.1 boolean True once parsed. If the guard ever reads the
# string key "on" instead, every workflow silently reads as non-reusable and the
# whole rule passes vacuously -- the inert-verification shape backend#1729 is about.
case(
    "the boolean-True `on:` key is what gets read",
    """
on:
  workflow_call:
concurrency:
  group: g
  cancel-in-progress: true
""",
    expect_finding=True,
)


# --- main(): the scan itself must fail closed ------------------------------
def _main_on(dirpath: Path) -> int:
    return guard.main(["reusable-no-cancel.py", str(dirpath)])


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    record(_main_on(root) == 1, "a missing .github/workflows dir exits 1, never clean")

    (root / ".github" / "workflows").mkdir(parents=True)
    record(_main_on(root) == 1, "an EMPTY workflow dir exits 1, never clean")

    (root / ".github" / "workflows" / "ok.yml").write_text(
        "on:\n  workflow_call:\nconcurrency:\n  group: g\n  cancel-in-progress: false\n"
    )
    record(_main_on(root) == 0, "a clean tree exits 0")

    (root / ".github" / "workflows" / "bad.yml").write_text(
        "on:\n  workflow_call:\nconcurrency:\n  group: g\n  cancel-in-progress: true\n"
    )
    record(_main_on(root) == 1, "one offender in the tree exits 1")

failed = [n for ok, n in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    for n in failed:
        print(f"  FAILED: {n}")
    sys.exit(1)
