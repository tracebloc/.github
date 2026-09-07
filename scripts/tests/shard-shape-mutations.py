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
    #
    # RETARGETED ONTO THE `decides` HALF, and the reason is worth recording.
    # This mutation used to be about the EXIT surviving in a comment -- and
    # once `EXIT_NONZERO` was anchored in command position (@saadqbal, #412),
    # a commented-out `exit 1` stopped matching whether or not comments were
    # blanked, so the mutation went UNCAUGHT for a good reason: the anchoring
    # subsumed it.
    #
    # `shell_code_only` still does real work on the OTHER half. A fan-in whose
    # only `!= "success"` comparison sits in a comment satisfies
    # `DECIDES_ON_RESULT` without it, so a step that exits non-zero for an
    # unrelated reason certifies again. Deleting the mutation would have lost
    # that; leaving it pointed at the exit would have left an entry proving
    # nothing.
    # STILL DISTINCT from the trailing-comment mutation below: this one
    # removes comment handling ENTIRELY (whole-line comments come back),
    # that one keeps the whole-line pass and restores only trailing ones.
    ("the verdict comparison is counted from a comment",
     '        run = executable_text(shell_code_only(str(step.get("run") or "")),\n                              blank_quotes=False)',
     '        run = str(step.get("run") or "")'),

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
     "        runs_anyway = step_runs_anyway(step)",
     "        runs_anyway = True"),

    # --- …and the invocation check must read CODE, not co-occurrence -------
    #
    # ALSO BUGBOT'S, and the one worth being embarrassed about: this check was
    # written one function below the fan-in scan that already blanks comments,
    # and read raw text anyway. A commented-out `make "$TARGET"` and an
    # `echo "would run: make $TARGET"` both certified.
    ("the invocation check goes back to reading raw text",
     'MAKE_INVOCATION.search(\n'
     '                        shell_code_only(str(step.get("run") or "")))):',
     '"make" in str(step.get("run") or "")):'),

    # THE SAME SHAPE, ON THE THIRD SCAN. This one shipped: the derivation
    # check was the last `run` scan reading raw text, so commenting the
    # `mapfile` out and sourcing the shard list from a file naming no member
    # left the guard printing "matrix derived from make" over a hand-written
    # matrix -- the exact drift the rule exists to refuse. Two mutations, not
    # one, because the reviewer's proposed fix (blanking alone) closes the
    # whole-line spelling and leaves the trailing-comment one open.
    ("the derivation check goes back to reading raw text",
     '        DERIVES_MATRIX.search(\n            executable_text(shell_code_only(str(step.get("run") or ""))))',
     '        "print-mutation-targets" in str(step.get("run") or "")'),

    ("the derivation token stops needing to be in command position",
     '        DERIVES_MATRIX.search(\n            executable_text(shell_code_only(str(step.get("run") or ""))))',
     '        "print-mutation-targets" in shell_code_only(str(step.get("run") or ""))'),

    # --- SHAPE A: the serial arm, which had no cases and no mutations ------
    # It returned success on the RAW TEXT of any step naming `make mutations`,
    # and `main` returned 0 on the spot -- skipping every tooth the sharded
    # shape is held to. Three mutations, one per tooth, because the arm can
    # lose them independently.
    #
    # --- …and the decision literal, plus the WEAKER FIX that looks like one --
    # Bugbot reported `succeeded` satisfying the unbounded literal. That
    # example is wrong -- `succeeded` contains no `success` -- but `successful`
    # and the BARE WORD in prose do satisfy it, and this repo's own fan-in has
    # the bare word one line above its decision. The last mutation is the
    # word-boundary-only version: it closes `successful` and leaves the bare
    # word, so it reads as a fix and must still redden.
    ('the serial arm stops refusing a swallowed failure',
     '        (?! [^\\n]* \\|\\| )                 # and its failure not swallowed',
     ''),

    ('the serial arm stops refusing a skippable step',
     'if not step_runs_anyway(step):',
     'if False:'),

    ('the serial arm stops refusing a continue-on-error job',
     '    if j.get("continue-on-error"):',
     '    if False:'),

    ('the success literal stops needing a comparison',
     '        \\[ \\[?                                 # inside a TEST…\n        [^\\]\\n]*\n        (?: == | != | -eq | -ne | (?<=\\s)= )    # …a COMPARISON…\n        \\s* ["\']? \\b success \\b ["\']?          # …against `success` ITSELF\n        [^\\]\\n]* \\]',
     '          ["\']?success["\']?'),

    ('the success literal is word-bounded but not compared',
     '        \\[ \\[?                                 # inside a TEST…\n        [^\\]\\n]*\n        (?: == | != | -eq | -ne | (?<=\\s)= )    # …a COMPARISON…\n        \\s* ["\']? \\b success \\b ["\']?          # …against `success` ITSELF\n        [^\\]\\n]* \\]',
     '          ["\']? \\b success \\b ["\']?'),

    # A DRY RUN IS NOT THE TIER. `mutations-dry` resolves markers and
    # reddens nothing; `\b` accepts it because `s`->`-` is a boundary,
    # which is how a fix for the arm's other holes blunted this one.
    ('the serial arm accepts `make mutations-dry`',
     '        make \\s+ mutations (?= \\s | $ )   # …as the command, and NOT',
     '        make \\s+ mutations \\b             # …as the command'),

    # --- the two holes the PROCESS SUBSTITUTION concession opened ----------
    # `DERIVES_MATRIX` must treat `(` as command position, because the real
    # invocation is `<(make … print-mutation-targets)`. That one concession is
    # what a commented-out copy AND an echoed copy of the same line each walk
    # through, carrying their own `(`. Anchoring cannot tell them apart; only
    # knowing the text is a comment or a string can.
    #
    # And the decision: a `[` test on any *RESULT*/*rc* variable never required
    # the literal, so `[ "$SHARDS_RESULT" = "failure" ]` certified while a
    # shard reporting `skipped` counted as a pass -- backend#1424 reopened by
    # the very context that closes it.
    ('the derivation scan reads comments and strings again',
     '            executable_text(shell_code_only(str(step.get("run") or ""))))',
     '            shell_code_only(str(step.get("run") or "")))'),

    ('the decision accepts a bare result-variable test again',
     '        \\[ \\[?                                 # inside a TEST…\n        [^\\]\\n]*\n        (?: == | != | -eq | -ne | (?<=\\s)= )    # …a COMPARISON…\n        \\s* ["\']? \\b success \\b ["\']?          # …against `success` ITSELF\n        [^\\]\\n]* \\]',
     '        (?: (?: == | != | -eq | -ne | (?<=\\s)= )\n            \\s* ["\']? \\b success \\b ["\']?\n          | \\[\\s+"?\\$\\{?\\w*(?:RESULT|result|rc)\\w*\\}?"? )'),

    # --- the DECISION half, through the same three spellings -------------
    # TWO mechanisms, and neither alone is enough -- which is why both are
    # mutated separately. Cutting comments is what rejects
    # `exit 1  # [ "$X" != "success" ]`, brackets and all; requiring the
    # comparison to sit inside a TEST is what rejects an echo that merely
    # mentions it. Quotes are deliberately NOT blanked here, because the
    # legitimate form quotes its own literal -- the opposite of what the
    # derivation scan needs from the same helper.
    ('the fan-in scan reads commented-out text again',
     '        run = executable_text(shell_code_only(str(step.get("run") or "")),\n                              blank_quotes=False)',
     '        run = shell_code_only(str(step.get("run") or ""))'),

    ('the success comparison stops needing to be inside a test',
     '        \\[ \\[?                                 # inside a TEST…\n        [^\\]\\n]*\n        (?: == | != | -eq | -ne | (?<=\\s)= )    # …a COMPARISON…\n        \\s* ["\']? \\b success \\b ["\']?          # …against `success` ITSELF\n        [^\\]\\n]* \\]',
     '        (?: == | != | -eq | -ne | (?<=\\s)= )    # a COMPARISON…\n        \\s* ["\']? \\b success \\b ["\']?          # …against `success` ITSELF'),

    # --- …and a leg that cannot report failure satisfies it by construction
    ("a continue-on-error shard job stops being refused",
     "        if job.get(\"continue-on-error\"):",
     "        if False:"),

    # --- the exit must be in COMMAND POSITION, not merely present ----------
    #
    # `shell_code_only` blanks WHOLE-LINE comments, which is right and is what
    # its docstring says. `EXIT_NONZERO` was unanchored, so a TRAILING comment
    # satisfied it: `echo "tier checked"   # exit 1` certified while nothing
    # exited (@saadqbal, #412). The same trailing-comment case closed on the
    # `make` side one function above, with the `#` moved to the end of a line.
    ("the exit is counted wherever it appears, not where it runs",
     "    (?: ^ | [;&|] | \\bthen\\b | \\belse\\b | \\bdo\\b | \\{ )  # command position",
     "    (?: )  # command position"),

    # --- …and the make step must not be skippable --------------------------
    #
    # The step-level `if` rule was added to the fan-in and not to the shard
    # scan four lines below, while `continue-on-error` was read in both. A
    # skipped step does not red its job, so a leg gated on
    # `github.event_name == 'push'` is green on every PR.
    ("the make step's own `if` stops being read",
     "            if (step_runs_anyway(step)",
     "            if (True"),

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
        # PARSE-FIRST (Bugbot, backend#3220). A mutation whose `new` does not even
        # parse tests nothing about the guard's LOGIC -- it just breaks Python. Run
        # unguarded it would reach the SUITE as an import-time SyntaxError, which
        # this harness files as UNCAUGHT ("the suite did not report -- mutation
        # broke the harness") and, on `--dry`, waves through as "anchor ok". Both
        # are wrong: an unparseable mutant is a STALE row (rule 3: cannot-evaluate
        # is a finding, not coverage). Reject it up front, in BOTH modes, so a
        # syntactically invalid mutant can never masquerade as a caught mutation or
        # as a genuine coverage gap.
        try:
            compile(mutated, str(GUARD), "exec")
        except SyntaxError as exc:
            stale.append((label, "MUTANT DOES NOT PARSE (%s) -- fix the mutation" % exc))
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
