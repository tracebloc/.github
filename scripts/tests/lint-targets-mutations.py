#!/usr/bin/env python3
"""Mutation runner for lint-targets-selftest.py (.github#388).

Breaks scripts/lint-targets-run-in-ci.py one behaviour at a time and asserts the
fixture suite REDDENS. Every anchor is asserted applied -- an inert mutation and
real coverage are indistinguishable in a log (CLAUDE.md rule 5).

WHY THE FIXTURE SUITE AND NOT THE LIVE TREE. Two of these mutations went GREEN
against this repo while being real holes: every audit here is covered one way or
another, so blanket-passing the direct-run check changes nothing visible. The
fixtures contain an actual orphan, which is what makes the hole observable.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
GUARD = ROOT / "scripts" / "lint-targets-run-in-ci.py"
SUITE = HERE / "lint-targets-selftest.py"

sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import mutation_baseline  # noqa: E402

MUTATIONS = [
    (
        "blanket-pass the direct-run escape hatch",
        "        if any(e in run_text for e in run_evidence(t, text)):",
        "        if True:",
    ),
    (
        "re-open the interpreter leak that made v1 vacuous",
        '    GENERIC = {"python", "python3", "bash", "sh", "env", "make", "pipx", "uv"}',
        "    GENERIC = set()",
    ),
    (
        "trust the interpreter as evidence even when a script is named",
        "    ev = list(scripts) if scripts else list(tools)",
        "    ev = list(scripts) + list(tools)",
    ),
    (
        "stop expanding $(VAR) prerequisite lists",
        "        for p in expand_vars(text, deps.get(t, [])):",
        "        for p in deps.get(t, []):",
    ),
    (
        "count a commented `make` line as a real invocation",
        "                    if stripped.startswith(\"#\"):",
        "                    if False:",
    ),
    (
        "let a missing `lint` target pass",
        '    if "lint" not in deps:',
        "    if False:",
    ),
    (
        "let zero lint prerequisites pass as clean",
        "    if not lint_prereqs:",
        "    if False:",
    ),
    (
        "report clean when no workflow run: content could be read",
        "    if not run_text.strip():",
        "    if False:",
    ),
    (
        "report clean when the Makefile is absent",
        "    if not mk.is_file():",
        "    if False:",
    ),
    (
        "report clean when .github/workflows is absent",
        "    if not wf.is_dir():",
        "    if False:",
    ),
]


def suite_passes() -> bool:
    r = subprocess.run([sys.executable, str(SUITE)], capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    dry = "--dry" in sys.argv
    original = GUARD.read_text(encoding="utf-8")

    missing = [lbl for lbl, old, _ in MUTATIONS if original.count(old) != 1]
    if missing:
        print("::error::these mutation anchors no longer match exactly once:")
        for lbl in missing:
            print(f"  - {lbl}")
        print("  An anchor that does not apply is an INERT mutation: it proves nothing")
        print("  while looking identical to coverage in the log.")
        return 1
    print(f"all {len(MUTATIONS)} anchors match exactly once")

    if dry:
        for lbl, _, _ in MUTATIONS:
            print(f"  would mutate: {lbl}")
        return 0

    rc = mutation_baseline.guard(ROOT, [GUARD])
    if rc:
        return rc

    if not suite_passes():
        print("::error::the fixture suite is RED before any mutation -- fix that first")
        return 1
    print("baseline: fixture suite green\n")

    survivors = []
    try:
        for lbl, old, new in MUTATIONS:
            mutated = original.replace(old, new, 1)
            GUARD.write_text(mutated, encoding="utf-8")
            reddened = not suite_passes()
            print(f"  {'RED  (good)' if reddened else 'GREEN (VACUOUS!)'}  {lbl}")
            if not reddened:
                survivors.append(lbl)
    finally:
        GUARD.write_text(original, encoding="utf-8")

    if survivors:
        print("\n::error::these mutations did NOT redden the suite:")
        for lbl in survivors:
            print(f"  - {lbl}")
        return 1
    print(f"\nall {len(MUTATIONS)} mutations reddened the suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
