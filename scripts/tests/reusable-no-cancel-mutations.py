#!/usr/bin/env python3
"""Mutation runner for reusable-no-cancel-selftest.py (tracebloc/backend#2756).

Breaks scripts/reusable-no-cancel.py one behaviour at a time and asserts the
suite REDDENS. A case that survives its own mutation is vacuous and worse than
absent, because it makes the tier look staffed.

EVERY MUTATION ASSERTS ITS ANCHOR APPLIED (CLAUDE.md rule 5). An anchor that no
longer matches produces an inert mutation, and an inert mutation is
indistinguishable from good coverage in a log -- so a missing anchor is a hard
failure here, never a skip.

--dry lists the mutations and checks every anchor still matches, without running
the suite. That is the cheap CI shape used by the other runners in this tier.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
GUARD = ROOT / "scripts" / "reusable-no-cancel.py"
SUITE = HERE / "reusable-no-cancel-selftest.py"

# dont_write_bytecode BEFORE the import, deliberately: `selftests-cover` rejects
# anything under scripts/tests/ that is not a suite or a runner, and a
# `__pycache__/` left by this import is exactly that.
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import mutation_baseline  # noqa: E402

MUTATIONS = [
    (
        "stop flagging cancel-in-progress: true",
        "    if value is True:",
        "    if False:",
    ),
    (
        "treat a ${{ }} expression as safe instead of unreadable",
        '    if isinstance(value, str) and "${{" in value:',
        "    if False:",
    ),
    (
        "read the string key 'on' instead of the YAML boolean True",
        "    if True in doc:\n        return doc[True]",
        "    if False:\n        return doc[True]",
    ),
    (
        "let an unparseable workflow pass as clean",
        '        return [f"{name}: does not parse, so its concurrency cannot be read ({exc.__class__.__name__})"]',
        "        return []",
    ),
    (
        "let a non-mapping document pass as clean",
        '        return [f"{name}: is not a YAML mapping, so its concurrency cannot be read"]',
        "        return []",
    ),
    (
        "report clean on an empty workflow scan",
        '        print(f"::error::no workflow files under {d} — refusing to report clean on an empty scan")\n        return 1',
        "        return 0",
    ),
    (
        "report clean when the workflow dir is missing",
        '        print(f"::error::{d} is not a directory — refusing to report clean from a read I could not make")\n        return 1',
        "        return 0",
    ),
    (
        "flag NON-reusable workflows too (over-broad rule)",
        '    if not _declares(on, "workflow_call"):\n        return []',
        "    if False:\n        return []",
    ),
    (
        "stop recognising the bare-string `on: workflow_call` form",
        '    if isinstance(on, str):\n        return on == event',
        "    if False:\n        return on == event",
    ),
    (
        "stop recognising the list `on: [workflow_call]` form",
        "    if isinstance(on, list):\n        return event in on",
        "    if False:\n        return event in on",
    ),
]


def run_suite() -> bool:
    """True when the suite passes."""
    r = subprocess.run([sys.executable, str(SUITE)], capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    dry = "--dry" in sys.argv
    original = GUARD.read_text(encoding="utf-8")

    missing = [label for label, old, _ in MUTATIONS if original.count(old) != 1]
    if missing:
        print("::error::these mutation anchors no longer match the guard exactly once:")
        for label in missing:
            print(f"  - {label}")
        print("  An anchor that does not apply is an INERT mutation: it proves nothing")
        print("  while looking identical to coverage. Re-point it at the current code.")
        return 1
    print(f"all {len(MUTATIONS)} anchors match exactly once")

    if dry:
        for label, _, _ in MUTATIONS:
            print(f"  would mutate: {label}")
        return 0

    # Refuse rather than measure against a baseline nothing vouches for
    # (backend#2441). A previous run killed mid-mutation leaves mutated text on
    # disk, and the next run would read THAT as pristine, measure every mutation
    # against a premise nobody typed, and report `0 uncaught` -- byte-identical
    # to real coverage. Only the writing path is guarded: `--dry` writes nothing
    # and is what `make check` runs on every push.
    rc = mutation_baseline.guard(ROOT, [GUARD])
    if rc:
        return rc

    if not run_suite():
        print("::error::the suite is RED before any mutation — fix that first")
        return 1
    print("baseline: suite green\n")

    survivors = []
    try:
        for label, old, new in MUTATIONS:
            mutated = original.replace(old, new, 1)
            GUARD.write_text(mutated, encoding="utf-8")
            reddened = not run_suite()
            print(f"  {'RED  (good)' if reddened else 'GREEN (VACUOUS!)'}  {label}")
            if not reddened:
                survivors.append(label)
    finally:
        GUARD.write_text(original, encoding="utf-8")

    if survivors:
        print("\n::error::these mutations did NOT redden the suite:")
        for label in survivors:
            print(f"  - {label}")
        return 1
    print(f"\nall {len(MUTATIONS)} mutations reddened the suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
