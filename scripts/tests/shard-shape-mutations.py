#!/usr/bin/env python3
"""Mutation harness for the shard-shape audit (backend#3157, .github#412).

`shard-shape-selftest.py` asserts the audit's behaviour; this asserts the
SELFTEST. Break a rule in `scripts/shard-shape-check.py`, watch the suite
redden, restore. A case that stays green under its own rule being deleted is
vacuous, and a green selftest log cannot tell you which of its assertions are
load-bearing.

WHY THIS FILE IS THE ACTUAL ASK. Four negative cases arrived on #412 in two
review rounds -- an `exit` in a comment, an `exit` unrelated to the verdict, a
shard leg that only echoes, and the idiomatic `exit "$rc"` being refused. Every
one was a token the audit read where it does not mean what it says. @saadqbal's
diagnosis was that fixing the named ones is whack-a-mole: *"the axis worth
pinning isn't 'these two conditions' but 'does each token the guard asserts
actually DO the thing it's asserted for'"*. Each mutation below removes one
such token's teeth; the selftest case that reddens is the proof that token is
doing work.

THE MUTATION CALLS THE CODE UNDER TEST (CLAUDE.md rule 9). It edits
`scripts/shard-shape-check.py` on disk and re-runs the real suite, which loads
that same file by path. No second copy of any rule lives in here.

EVERY ANCHOR MUST MATCH EXACTLY ONCE -- twice mutates an arbitrary one and the
run reports "uncaught" for the wrong reason; zero is stale and fails the run
exactly like an uncaught mutation. That is the assertion that the anchor
ACTUALLY APPLIED, without which an inert mutation and good coverage look
identical in a log.

  shard-shape-mutations.py          run them all
  shard-shape-mutations.py --dry    resolve anchors only
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "shard-shape-check.py"
SUITE = ROOT / "scripts" / "tests" / "shard-shape-selftest.py"

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402


# (label, old, new)
MUTATIONS = [
    # --- the fan-in must be able to FAIL, not merely mention the result -----
    ("a step that only logs the verdict certifies the tier",
     "        fails = EXIT_NONZERO.search(run) is not None",
     "        fails = True"),

    # --- …and the exit must be about the VERDICT, not anything at all -------
    ("an exit unrelated to the shards' result certifies",
     "        decides = DECIDES_ON_RESULT.search(run) is not None",
     "        decides = True"),

    # --- …in EXECUTABLE shell. A comment is prose. -------------------------
    ("an `exit 1` surviving only in a comment certifies",
     "        run = shell_code_only(str(step.get(\"run\") or \"\"))",
     "        run = str(step.get(\"run\") or \"\")"),

    # --- …and continue-on-error turns the failure back into a pass ---------
    ("continue-on-error stops disqualifying the certifying step",
     "        if fails and decides and runs_anyway and not step.get(\"continue-on-error\"):",
     "        if fails and decides and runs_anyway:"),

    # --- the runs-anyway condition, EXACT and not a substring --------------
    ("a compound condition containing always() is accepted",
     "    if cond not in RUNS_ANYWAY:",
     "    if not any(r in cond for r in RUNS_ANYWAY):"),

    # --- the matrix must be derived from make ------------------------------
    ("a hand-written matrix stops being refused",
     "    if not derived:",
     "    if False:"),

    # --- the shard legs must actually run the target -----------------------
    #
    # THE ONE BUGBOT FOUND. Everything above proves the tier is WIRED; this is
    # the only rule that looks inside a leg for work. Without it a derived
    # matrix of steps that only echo certifies, and the required context goes
    # green over fifteen shards that executed nothing.
    ("shard legs that run no `make` certify the tier",
     "    if missing:",
     "    if False:"),

    # --- the step's OWN `if` skips just as hard as the job's ---------------
    #
    # BUGBOT'S, #412. The job-level condition was checked and the step-level
    # one was not, so a step conditioned on the shards having SUCCEEDED
    # certified: it is skipped exactly when a shard fails, later steps run,
    # and the required context reports success.
    ("a step-level `if` that skips on a failed shard stops disqualifying it",
     "        runs_anyway = step_cond == \"\" or step_cond in RUNS_ANYWAY",
     "        runs_anyway = True"),

    # --- …and the invocation check must read CODE, not co-occurrence -------
    #
    # ALSO BUGBOT'S, and the one worth being embarrassed about: this check was
    # written one function below the fan-in scan that already blanks comments,
    # and read raw text anyway. A commented-out `make "$TARGET"` and an
    # `echo "would run: make $TARGET"` both certified.
    ("the invocation check goes back to reading raw text",
     "            if MAKE_INVOCATION.search(shell_code_only(str(step.get(\"run\") or \"\"))):",
     "            if \"make\" in str(step.get(\"run\") or \"\"):"),

    # --- nothing-to-check must never read as everything-passed -------------
    ("an empty tier certifies in main",
     "    if not targets:\n        die(\"derived NO mutation targets; refusing to certify a tier that runs nothing\")\n\n    # SHAPE A",
     "    if False:\n        die(\"derived NO mutation targets; refusing to certify a tier that runs nothing\")\n\n    # SHAPE A"),

    # --- …and at the producer, where the list is derived -------------------
    ("an empty target list from make is accepted",
     "    if not targets:\n        # Zero targets must never read",
     "    if False:\n        # Zero targets must never read"),

    # --- an unreadable workflow is a finding -------------------------------
    ("an unreadable workflow reports the tier as enforced",
     "    if not WORKFLOW.is_file():",
     "    if False:"),

    # --- a failing `make` must not read as an empty tier -------------------
    ("a failing `make print-mutation-targets` is swallowed",
     "    if p.returncode != 0:",
     "    if False:"),
]


def _drop_bytecode_cache():
    """Remove any cached bytecode for the gate. See the header: a stale pyc makes
    a caught mutation report as uncaught."""
    try:
        cached = importlib.util.cache_from_source(str(GUARD))
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
        rc = mutation_baseline.guard(ROOT, [GUARD])
        if rc:
            return rc

    pristine = GUARD.read_text(encoding="utf-8")
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
        GUARD.write_text(mutated, encoding="utf-8")
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
            GUARD.write_text(pristine, encoding="utf-8")
            _drop_bytecode_cache()
        caught = [
            line.strip()[6:].strip()
            for line in run.stdout.splitlines()
            if line.strip().startswith("FAIL:")
        ]
        # A crash counts as caught ONLY if the suite actually ran and reported;
        # a bare traceback with no assertion output means the mutation broke the
        # harness rather than being detected by a case, which is not coverage.
        reported = "shard-shape-selftest:" in run.stdout
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (label, ", ".join(caught)[:120]))
        elif not reported:
            uncaught.append((label, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % label)
        else:
            uncaught.append((label, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % label)

    if GUARD.read_text(encoding="utf-8") != pristine:
        sys.stderr.write("::error::%s was left mutated. Restore it from git.\n" % GUARD.name)
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
