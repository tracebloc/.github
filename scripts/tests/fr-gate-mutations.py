#!/usr/bin/env python3
"""Mutation harness for the FR gate's base decision and trigger (backend#2840).

`fr-gate-selftest.py` asserts the behaviour; this asserts the SELFTEST. Break the
real workflows, watch the suite redden, restore. A case that stays green under its
own rule being deleted is vacuous.

TWO TARGETS, because "the mapping is right" and "something re-runs the gate on a
retarget" are separate claims in separate files:

  reusable   fr-gate.yml         — the base -> required mapping and the guard that
                                    disarms the misleading step on a non-promotion
                                    base.
  caller     fr-gate-caller.yml  — the trigger. The regression backend#2840 fixes
                                    lives entirely here: a `branches:` filter that
                                    cannot see a retarget out, and the `edited`
                                    event that must survive.

THE MUTATION EDITS THE REAL FILES (CLAUDE.md rule 9): it rewrites the workflow on
disk and re-runs the real suite, which extracts its logic out of that same file.
There is no second copy of the rule in here.

EVERY ANCHOR MUST MATCH EXACTLY ONCE — an anchor matching twice mutates an
arbitrary one, and one matching zero times is stale; both fail the run, so an
inert mutation cannot masquerade as coverage.

  fr-gate-mutations.py          run them all
  fr-gate-mutations.py --dry    resolve anchors only (the fast tier)
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REUSABLE = ROOT / ".github" / "workflows" / "fr-gate.yml"
CALLER = ROOT / ".github" / "workflows" / "fr-gate-caller.yml"
SUITE = ROOT / "scripts" / "tests" / "fr-gate-selftest.py"

TARGETS = {"reusable": REUSABLE, "caller": CALLER}

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402

# (label, old, new) — edits to fr-gate.yml (the mapping + the disarm guard).
REUSABLE_MUTATIONS = [
    # THE RETARGET-OUT HOLE, reintroduced in the mapping. If a non-promotion base
    # maps to a gating value, the gate runs on develop and its stale FAILURE is
    # never superseded by a green run — backend#2840, in the reusable half.
    ("a non-promotion base (develop, feature/*) is gated instead of skipped",
     '            *)           echo "required="',
     '            *)           echo "required=On dev"'),

    # THE OTHER DIRECTION: staging stops gating. This is the "do not weaken the
    # block" guard — a promotion to staging must still be able to fail.
    ("promotion to staging stops gating (required goes empty)",
     '            staging)     echo "required=On dev"',
     '            staging)     echo "required="'),

    # main/master stops gating — the prod hop must still be able to fail.
    ("promotion to main/master stops gating (required goes empty)",
     '            main|master) echo "required=Ready for prod"',
     '            main|master) echo "required="'),

    # THE DISARM REMOVED. Strip `required != ''` off the misleading promotion-shape
    # guard and it fires (exit 1) on a develop retarget — the job can never go
    # green, so the stale FAILURE is never cleared even with the trigger fixed.
    ("the misleading promotion-shape guard loses its `required != ''` gate",
     "      - name: Promotion-shape guard — only the train promotes\n"
     "        if: steps.target.outputs.required != '' && steps.skip.outputs.skip != 'true'",
     "      - name: Promotion-shape guard — only the train promotes\n"
     "        if: steps.skip.outputs.skip != 'true'"),
]

# (label, old, new) — edits to fr-gate-caller.yml (the trigger; the #2840 core).
CALLER_MUTATIONS = [
    # THE EXACT backend#2840 REGRESSION, made in good faith: "run the gate only
    # where it blocks." The base filter cannot see a retarget OUT, so the required
    # check welds a stale FAILURE forever.
    ("the caller reacquires a `branches:` filter it cannot re-run through",
     '  pull_request:\n    types:',
     '  pull_request:\n    branches: [staging, main]\n    types:'),

    # backend#1945 REOPENED: without `edited` even the main<->staging retargets go
    # stale, because `edited` is the only event a base change fires.
    ("the caller drops `edited`, so no base change ever re-runs the gate",
     ', unlabeled, edited]',
     ', unlabeled]'),

    # backend#3228: the same retarget-out / never-arrive weld in `branches:`'s
    # three siblings. Each must be caught by the selftest's per-filter checks, or
    # a required gate can be skipped in a spelling this suite never exercised.
    ("the caller reacquires the weld as `branches-ignore:` instead of `branches:`",
     '  pull_request:\n    types:',
     '  pull_request:\n    branches-ignore: [develop]\n    types:'),
    ("the caller adds a `paths:` filter, so the required gate never arrives",
     '  pull_request:\n    types:',
     "  pull_request:\n    paths: ['**.py']\n    types:"),
    ("the caller adds a `paths-ignore:` filter, so the required gate never arrives",
     '  pull_request:\n    types:',
     "  pull_request:\n    paths-ignore: ['docs/**']\n    types:"),
]

ALL_MUTATIONS = ([("reusable", *m) for m in REUSABLE_MUTATIONS]
                 + [("caller", *m) for m in CALLER_MUTATIONS])


def apply_one(src, old, new):
    n = src.count(old)
    if n != 1:
        raise LookupError("anchor matched %d times, expected exactly 1: %r" % (n, old[:80]))
    out = src.replace(old, new, 1)
    return None if out == src else out


def main():
    dry = "--dry" in sys.argv

    if not dry:
        rc = mutation_baseline.guard(ROOT, list(TARGETS.values()))
        if rc:
            return rc

    pristine_by_target = {
        name: path.read_text(encoding="utf-8") for name, path in TARGETS.items()
    }
    stale, uncaught = [], []

    for target, label, old, new in ALL_MUTATIONS:
        path = TARGETS[target]
        pristine = pristine_by_target[target]
        try:
            mutated = apply_one(pristine, old, new)
        except LookupError as exc:
            stale.append((label, str(exc)))
            continue
        if mutated is None:
            stale.append((label, "NO-OP: the mutation changed nothing"))
            continue
        if dry:
            print("  anchor ok  [%s] %s" % (target, label))
            continue
        path.write_text(mutated, encoding="utf-8")
        try:
            run = subprocess.run(
                [sys.executable, "-B", str(SUITE)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
        finally:
            path.write_text(pristine, encoding="utf-8")
        caught = [
            line.strip()[6:].strip()
            for line in run.stdout.splitlines()
            if line.strip().startswith("FAIL:")
        ]
        # A crash counts as caught ONLY if the suite actually ran and reported; a
        # bare traceback means the mutation broke the harness, not that a case
        # detected it.
        reported = "fr-gate-selftest:" in run.stdout
        shown = "[%s] %s" % (target, label)
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (shown, ", ".join(caught)[:120]))
        elif not reported:
            uncaught.append((shown, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % shown)
        else:
            uncaught.append((shown, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % shown)

    for name, path in TARGETS.items():
        if path.read_text(encoding="utf-8") != pristine_by_target[name]:
            sys.stderr.write(
                "::error::%s was left mutated. Restore it from git.\n" % path.name)
            return 2

    print("\n%d mutation(s) across %d file(s): %d stale, %d uncaught"
          % (len(ALL_MUTATIONS), len(TARGETS), len(stale), len(uncaught)))
    for label, why in stale:
        sys.stderr.write("::error::STALE mutation `%s`: %s\n" % (label, why))
    for label, why in uncaught:
        sys.stderr.write(
            "::error::UNCAUGHT `%s`: %s. Add a case that fails under it, or delete "
            "the mutation and say why it is not worth pinning.\n" % (label, why)
        )
    return 1 if (stale or uncaught) else 0


if __name__ == "__main__":
    raise SystemExit(main())
