#!/usr/bin/env python3
"""Mutation harness for the merge-conflict gate (tracebloc/backend#2637).

`conflict-gate-selftest.py` asserts the gate's behaviour; this asserts the
SELFTEST. Break a rule in `scripts/conflict-gate.py`, watch the suite redden,
restore. A case that stays green under its own rule being deleted is vacuous,
and a green log cannot tell you which of its 91 assertions are load-bearing.

THE MUTATION CALLS THE CODE UNDER TEST (CLAUDE.md rule 9). It edits
`scripts/conflict-gate.py` on disk and re-runs the real suite, which imports that
same file by path. There is no second copy of the rule in here -- the alternative
shape, re-implementing the check inline and mutating the copy, is
indistinguishable from real coverage in a log and has bitten this org twice.

EVERY ANCHOR MUST MATCH EXACTLY ONCE. An anchor matching twice mutates an
arbitrary one, so the run reports "uncaught" for the wrong reason; an anchor
matching zero times is stale and fails the run exactly like an uncaught
mutation. That is the assertion that the anchor ACTUALLY APPLIED -- an inert
mutation and good coverage look identical in a log otherwise. `--dry` resolves
every anchor without running the suite, which is what belongs in the fast tier.

  conflict-gate-mutations.py          run them all
  conflict-gate-mutations.py --dry    resolve anchors only

THE BYTECODE CACHE IS DISARMED for the reason bugbot-gate-mutations.py documents
at length: a pyc is revalidated on (mtime-to-the-second, byte size), several
mutations below change the file by the same number of bytes, and back-to-back
runs inside one second would otherwise execute the PREVIOUS mutation's bytecode
-- reporting a caught mutation as uncaught with nothing in the log to say so.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "conflict-gate.py"
SUITE = ROOT / "scripts" / "tests" / "conflict-gate-selftest.py"

# See scripts/tests/mutation_baseline.py: the `finally` below restores the file on
# a crash but cannot after SIGKILL, a runner timeout, or a second harness racing
# this one -- and a mutation left on disk becomes the NEXT run's `pristine`.
#
# dont_write_bytecode BEFORE the import: `selftests-cover` rejects anything under
# scripts/tests/ that is not a suite or a runner, and a `__pycache__/` is exactly
# that.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402


# (label, old, new)
MUTATIONS = [
    # --- (A) the load-bearing claim: affirmative conflict evidence wins -----
    #
    # The cheapest way to get this file wrong. A conflicted PR presents
    # `mergeable=CONFLICTING` with `mergeStateStatus=UNKNOWN` for the first
    # moments after its base moves, so an "any UNKNOWN wins" rule drops exactly
    # the finding the gate exists to raise -- and passes every other case.
    ("an UNKNOWN in either field outranks affirmative conflict evidence",
     '    for field, value in CONFLICT_EVIDENCE:',
     '    if "UNKNOWN" in (mergeable, state):\n'
     '        return UNDETERMINED, "unknown wins"\n'
     '    for field, value in CONFLICT_EVIDENCE:'),

    # Only ONE of the two fields consulted. `mergeStateStatus=DIRTY` with
    # `mergeable` not yet computed is a real payload, and this drops it.
    ("only `mergeable` is consulted, so a DIRTY-only payload reads clean",
     'CONFLICT_EVIDENCE = (("mergeable", "CONFLICTING"), ("mergeStateStatus", "DIRTY"))',
     'CONFLICT_EVIDENCE = (("mergeable", "CONFLICTING"),)'),

    # --- (B) fail-closed: "cannot tell" must not become "fine" --------------
    ("a value GitHub never declared is treated as CLEAR",
     '        if not isinstance(value, str) or value not in domain:\n'
     '            return UNDETERMINED, (',
     '        if not isinstance(value, str) or value not in domain:\n'
     '            return CLEAR, ('),

    ("a field absent from the payload is treated as CLEAR",
     '        if value is None:\n'
     '            return UNDETERMINED, f"{field} absent from the PR payload"',
     '        if value is None:\n'
     '            return CLEAR, f"{field} absent from the PR payload"'),

    ("an undetermined mergeability writes a SUCCESS status",
     '    UNDETERMINED: "pending",',
     '    UNDETERMINED: "success",'),

    ("an undetermined PR stops being a run-level error",
     '        if st["verdict"] == UNDETERMINED:\n            errors.append(',
     '        if False and st["verdict"] == UNDETERMINED:\n            errors.append('),

    # --- (C) the status states themselves ----------------------------------
    ("a conflicted PR is marked SUCCESS",
     '    CONFLICTED: "failure",',
     '    CONFLICTED: "success",'),

    # The half that makes the context requireable. If a healthy PR stops getting
    # `success`, requiring this context bricks every clean PR at
    # "Expected -- waiting" -- the exact failure bricked-prs.py hunts.
    ("a healthy PR stops getting a clearing SUCCESS status",
     '    CLEAR: "success",',
     '    CLEAR: "pending",'),

    # A renamed context does not stop being required, it stops being REPORTED.
    ("the status context is renamed, which would brick every PR requiring it",
     'CONTEXT = "conflict-gate / mergeable"',
     'CONTEXT = "conflict-gate / conflicts"'),

    # --- (D) coverage of the PR set ----------------------------------------
    ("drafts are skipped, so a draft has no status when it turns ready",
     '    out = []\n    for pr in prs:\n        verdict, why = classify(pr)',
     '    out = []\n    for pr in prs:\n        if pr.get("isDraft"):\n'
     '            continue\n        verdict, why = classify(pr)'),

    ("a PR list at the truncation cap is swept partially and called clean",
     '    if len(prs) >= PR_LIST_LIMIT:',
     '    if False and len(prs) >= PR_LIST_LIMIT:'),

    # --- (E) unreadable is not clean ---------------------------------------
    ("an unreadable PR list reports the repo as having no PRs",
     '        return [], [f"{name}: PR list unreadable ({exc.detail})"]',
     '        return [], []'),

    ("a status that failed to write is swallowed, so the PR stays empty-green",
     "            errors.append(\n"
     "                f\"{name}#{st['number']}: could not write the {st['state']} status \"",
     "            _swallowed = (\n"
     "                f\"{name}#{st['number']}: could not write the {st['state']} status \""),

    ("a PR with no head sha is skipped silently",
     '            errors.append(f"{name}#{st[\'number\']}: no head sha, so no status could be written")',
     '            pass'),

    # --- (F) the exit-code ranking -----------------------------------------
    #
    # If an error stops outranking a clean sweep, a partially-read fleet reports
    # "nothing conflicted" and exits 0 -- the fail-open, in the gate.
    ("an un-evaluated PR no longer outranks a clean sweep",
     '    if errors:\n        return 2\n'
     '    return 1 if any(s["verdict"] == CONFLICTED for s in statuses) else 0',
     '    return 1 if any(s["verdict"] == CONFLICTED for s in statuses) else 0'),

    # --- (F2) the 1000-statuses-per-sha-and-context cap ---------------------
    #
    # Without the dedup a PR open three weeks exhausts the cap at 48 writes a day
    # and every later write 422s -- the gate going silent on the stalest PRs.
    ("the unchanged-status skip is removed, so every sweep burns a write",
     '        if st["existing"] == st["state"]:',
     '        if False and st["existing"] == st["state"]:'),

    # The inverse is worse: it writes only when nothing would change, so a
    # resolved conflict never gets cleared and a new one is never reported.
    ("the skip is inverted, so the status is only ever written when identical",
     '        if st["existing"] == st["state"]:',
     '        if st["existing"] != st["state"]:'),

    # GraphQL says SUCCESS, the Statuses API takes success. Unfolded, every status
    # looks changed and the dedup silently does nothing.
    ("the case fold goes, so every status looks changed and the cap returns",
     '            return state.lower() if isinstance(state, str) else None',
     '            return state if isinstance(state, str) else None'),

    ("existing_state matches ANY context, so another check's state is read as ours",
     '        if entry.get("context") == CONTEXT:',
     '        if entry.get("context") is not None:'),

    # --- (G) the retry loop -------------------------------------------------
    ("every PR is re-read, not only the ones GitHub would not answer",
     '        pending = [i for i, pr in enumerate(resolved)\n'
     '                   if classify(pr)[0] == UNDETERMINED]',
     '        pending = [i for i, pr in enumerate(resolved)]'),

    ("the retry sleeps for real instead of through the injected sleeper",
     '        sleeper(sleep_for)',
     '        time.sleep(sleep_for)'),
]


def _drop_bytecode_cache():
    """Remove any cached bytecode for the gate. See the header: a stale pyc makes
    a caught mutation report as uncaught."""
    try:
        cached = importlib.util.cache_from_source(str(GATE))
    except (ValueError, NotImplementedError):
        return
    try:
        os.unlink(cached)
    except OSError:
        pass


def apply_one(src, old, new):
    n = src.count(old)
    if n != 1:
        raise LookupError("anchor matched %d times, expected exactly 1: %r" % (n, old[:80]))
    out = src.replace(old, new, 1)
    return None if out == src else out


def main():
    dry = "--dry" in sys.argv

    # Refuse rather than measure against a baseline nothing vouches for. Only the
    # writing path: `--dry` writes nothing, so it has no restore to lose -- and it
    # is what `make check` runs on every push, where refusing on an uncommitted
    # edit would block the pre-push tier for whoever is editing the target.
    if not dry:
        rc = mutation_baseline.guard(ROOT, [GATE])
        if rc:
            return rc

    pristine = GATE.read_text(encoding="utf-8")
    stale, uncaught = [], []

    for label, old, new in MUTATIONS:
        try:
            mutated = apply_one(pristine, old, new)
        except LookupError as exc:
            stale.append((label, str(exc)))
            continue
        if mutated is None:
            stale.append((label, "NO-OP: the mutation changed nothing"))
            continue
        if dry:
            print("  anchor ok  %s" % label)
            continue
        GATE.write_text(mutated, encoding="utf-8")
        _drop_bytecode_cache()
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            run = subprocess.run(
                [sys.executable, "-B", str(SUITE)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
        finally:
            # ALWAYS restore, including on a crash. A mutation left on disk makes
            # every later run measure the wrong script, and the tell is a suite
            # that reddens for reasons nobody typed.
            GATE.write_text(pristine, encoding="utf-8")
            _drop_bytecode_cache()
        caught = [
            line.strip()[6:].strip()
            for line in run.stdout.splitlines()
            if line.strip().startswith("FAIL:")
        ]
        # A crash counts as caught ONLY if the suite actually ran and reported; a
        # bare traceback with no assertion output means the mutation broke the
        # harness rather than being detected by a case, which is not coverage.
        reported = "conflict-gate-selftest:" in run.stdout
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (label, ", ".join(caught)[:120]))
        elif not reported:
            uncaught.append((label, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % label)
        else:
            uncaught.append((label, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % label)

    if GATE.read_text(encoding="utf-8") != pristine:
        sys.stderr.write("::error::%s was left mutated. Restore it from git.\n" % GATE.name)
        return 2

    print("\n%d mutation(s): %d stale, %d uncaught" % (len(MUTATIONS), len(stale), len(uncaught)))
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
