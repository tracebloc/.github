#!/usr/bin/env python3
"""Is the mutation tier actually ENFORCED by the required `selftests` context?

tracebloc/backend#3157. `make mutations` used to run every runner in one job, and
`selftests-cover` proved CI executed the tier by grepping the workflow for
`run: make mutations`. Sharding replaced that single invocation with a matrix, so the
grep had to be replaced too -- and the first replacement checked only TWO of the three
properties that make a sharded tier enforced (Bugbot, .github#412):

    1. the shard list is DERIVED, not restated          -> `print-mutation-targets`
    2. something READS the shards' aggregate result     -> `needs.<shard>.result`
    3. the job that reads it actually RUNS when a shard FAILS

(3) is the one that was missing, and it is the one that matters most. A job whose
`needs` failed is SKIPPED, and GitHub reports a skipped required check as SUCCESS --
this repo's own promote.yml lesson (backend#1424). So without `if: always()` (or
`!cancelled()`) on that job, a red shard produces a GREEN `selftests` and the whole
tier is decorative. Two greps could not see it: `always()` anywhere in the file would
satisfy a substring check while sitting on the wrong job entirely.

Hence a parse rather than a grep. Everything here is derived from the workflow and
from `make print-mutation-targets`; nothing restates the target list or the job names.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

# Lives in scripts/, not scripts/tests/: this is an AUDIT of the workflow, like
# lint-targets-run-in-ci.py, not a selftest of a script. selftests-cover enforces that
# distinction by filename -- anything under scripts/tests/ must match
# `*-selftest.{py,sh}` or `*-mutations.py` so its wildcards can see it.
ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "selftests.yml"
# The context branch protection requires. Named here because branch protection names
# it too -- this is the one string that genuinely lives outside the repo.
REQUIRED_CONTEXT = "selftests"
# `always()` keeps the job running through a failure; `!cancelled()` does the same for
# a failure while still yielding to a real cancellation. Either satisfies (3).
# EXACT conditions, not substrings. `always() && needs.<shard>.result == 'success'`
# CONTAINS `always()` and still evaluates FALSE when a shard fails -- so the job is
# skipped and the skipped required check reports SUCCESS (Bugbot, .github#412). A
# substring test cannot tell a runs-anyway guard from a runs-anyway guard that has
# been ANDed with the very condition it exists to survive.
#
# So the whole expression must BE a runs-anyway form. Anything else is refused rather
# than interpreted: statically evaluating arbitrary GitHub expressions is not something
# this script can do honestly, and "cannot tell" must be a finding, not a pass. A
# compound condition that is genuinely safe can be added here deliberately, which is
# the point -- it becomes a reviewed decision instead of an accident.
RUNS_ANYWAY = frozenset({"always()", "!cancelled()"})

#: A step reds its job from a shell only by exiting non-zero, and there are two
#: shapes of that. `exit 0` is not one of them.
#:
#: THE PROPAGATED FORM WAS BEING REFUSED (@saadqbal, #412), which is the
#: expensive direction of wrong: `exit "$rc"` after accumulating is idiomatic
#: and enforces harder than `[ "$rc" -eq 0 ] || exit 1`, and refusing it tells
#: the maintainer who wrote the better version that the guard is broken. A
#: guard that punishes an improvement gets deleted, not fixed.
EXIT_NONZERO = re.compile(
    r"""(?mx) \bexit \s+ (?:
          [1-9][0-9]*            # a non-zero literal
        | "?\$\{?[A-Za-z_]\w*\}?"?  # or a propagated status: exit "$rc"
    )"""
)

#: …and the exit has to be ABOUT the shards' verdict. A bare echo plus an
#: unrelated `[ -f Makefile ] || exit 1` satisfied a presence-only test, so a
#: certifying step must also carry the comparison that makes the decision:
#: either the `success` literal it is compared against, or a `[ ... ]` test on
#: the variable holding a result. Shape, not proof of causation -- what it buys
#: is that both halves of the decision have to be in the step, rather than one
#: half and an exit that happens to be there.
DECIDES_ON_RESULT = re.compile(
    r"""(?mx) (?:
          ["']?success["']?          # compared against the success literal
        | \[\s+"?\$\{?\w*(?:RESULT|result|rc)\w*\}?"?  # or tested as a variable
    )"""
)


#: `make` invoked ON the matrix value, as a COMMAND. Anchored at the start of a
#: line (after optional `VAR=value` prefixes) so `echo "make $TARGET"` and a
#: commented-out invocation cannot satisfy it -- co-occurrence of the two tokens
#: anywhere in the block is not execution (Bugbot, #412).
MAKE_INVOCATION = re.compile(
    r"""(?mx) ^\s* (?:\w+=\S*\s+)*      # optional env prefixes
        make \s+ ["']? (?:
              \$\{?TARGET\}?            # make "$TARGET"
            | \$\{\{\s*matrix\.        # make ${{ matrix.target }}
        )"""
)


def shell_code_only(run: str) -> str:
    """`run` with whole-line shell comments blanked, line numbering intact.

    The same rule the member-enumeration check applies to the workflow, one
    layer down inside a step body: a token in a comment is prose, and counting
    it as executable is how `# previously: exit 1` certified a step whose teeth
    were gone (@saadqbal, #412).

    Whole-line only, deliberately. A trailing comment on an executable line
    (`make "$TARGET"  # the tier`) leaves the line executable, and stripping
    from the first `#` would also eat a `#` inside a quoted string.
    """
    return "\n".join(
        "" if line.lstrip().startswith("#") else line
        for line in run.splitlines()
    )


def normalise_if(cond: str) -> str:
    """Strip `${{ }}` and whitespace so the comparison is about the EXPRESSION."""
    c = cond.strip()
    if c.startswith("${{") and c.endswith("}}"):
        c = c[3:-2]
    return " ".join(c.split())


def mutation_targets() -> list[str]:
    """The tier, asked of make. Never restated -- see the module docstring."""
    p = subprocess.run(
        ["make", "--no-print-directory", "print-mutation-targets"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if p.returncode != 0:
        die(f"`make print-mutation-targets` failed ({p.returncode}): {p.stderr.strip()}")
    targets = [t for t in p.stdout.split() if t]
    if not targets:
        # Zero targets must never read as "the tier is fine": it is the same
        # nothing-to-check-means-everything-passed hole this file exists to close.
        die("derived NO mutation targets; refusing to certify a tier that runs nothing")
    return targets


def die(msg: str) -> None:
    print(f"shard-shape: {msg}", file=sys.stderr)
    raise SystemExit(1)


def needs_of(j: dict) -> list[str]:
    """`needs` is a string OR a list in GitHub's schema, and both appear in the wild.

    Normalised once because reading it raw is a real bug, not a style point: my first
    version did `list(j["needs"])` on the string form, which splits it into CHARACTERS
    and made a correctly-derived matrix report as undeclared -- a guard failing closed
    for the wrong reason is still a guard nobody can act on.
    """
    n = j.get("needs") or []
    return [n] if isinstance(n, str) else list(n)


def jobs(doc: dict) -> dict:
    js = doc.get("jobs")
    if not isinstance(js, dict) or not js:
        die("the workflow declares no jobs")
    return js


def required_job(js: dict) -> tuple[str, dict]:
    """The job whose check-run name IS the required context.

    GitHub names a check run after the job's `name:`, falling back to its id -- so
    that mapping, not the id alone, is what branch protection sees.
    """
    for jid, j in js.items():
        if (j.get("name") or jid) == REQUIRED_CONTEXT:
            return jid, j
    die(
        f"no job produces the required context {REQUIRED_CONTEXT!r}; branch protection "
        "would wait forever for a context nothing emits"
    )
    raise AssertionError  # unreachable, keeps type checkers quiet


def runs_make_mutations(j: dict) -> bool:
    for step in j.get("steps") or []:
        if re.search(r"(?m)^\s*make\s+mutations(\s|$)", str(step.get("run") or "")):
            return True
    return False


def main() -> int:
    if not WORKFLOW.is_file():
        die(f"{WORKFLOW} is unreadable -- refusing to report that CI enforces the tier")
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    js = jobs(doc)
    rid, rjob = required_job(js)
    targets = mutation_targets()
    # AND `main` REFUSES AN EMPTY TIER ITSELF, rather than trusting that its
    # producer did. `mutation_targets` already dies on zero, so this is
    # defence in depth -- and the selftest found it worth having: with the
    # producer stubbed, `main` printed "0 runner(s), none enumerated" and
    # returned 0. "Nothing to check" reading as "everything passed" is the one
    # shape this whole file exists to refuse, so it is refused at both the
    # place that derives the list and the place that reports on it.
    if not targets:
        die("derived NO mutation targets; refusing to certify a tier that runs nothing")

    # SHAPE A: the serial target, still perfectly valid.
    if runs_make_mutations(rjob):
        print(f"shard-shape: {REQUIRED_CONTEXT} runs `make mutations` directly (serial tier).")
        return 0

    # SHAPE B: sharded. All THREE properties, or it is not enforced.
    shard_ids = [n for n in needs_of(rjob) if isinstance(js.get(n), dict)
                 and (js[n].get("strategy") or {}).get("matrix")]
    if not shard_ids:
        die(
            f"{REQUIRED_CONTEXT} neither runs `make mutations` nor needs a matrix job. "
            "The tier is not enforced by the required context in either supported shape."
        )

    # The step that READS the aggregate must also be able to FAIL on it. Presence of
    # the token is not enforcement: a step that echoes the result and exits 0 -- a
    # log-only fan-in -- mentions `needs.<shard>.result` and enforces nothing
    # (Bugbot, .github#412).
    read: list[str] = []
    enforcing: list[str] = []
    for step in rjob.get("steps") or []:
        text = yaml.dump(step)
        hits = [s for s in shard_ids if f"needs.{s}.result" in text]
        if not hits:
            continue
        read.extend(hits)
        run = shell_code_only(str(step.get("run") or ""))
        # A non-zero exit is the only way a step reds its job from a shell. Also
        # refuse continue-on-error, which turns any failure back into a pass.
        #
        # THREE THINGS THIS GOT WRONG, all found on #412 and all the same shape:
        # the token was being read where it does not mean what it says.
        #
        #  1. COMMENTS. The scan read the raw `run` text, so a step whose teeth
        #     were gone but which still carried `# previously: exit 1` in a
        #     shell comment certified. That is the same comment-versus-executable
        #     distinction the member-enumeration check already makes one layer
        #     up, so `shell_code_only` above is that rule applied here too.
        #  2. A PROPAGATED STATUS IS ALSO A FAILURE, and refusing it was the
        #     costlier direction. `exit "$rc"` -- accumulate then propagate -- is
        #     the idiomatic form and enforces HARDER than `[ "$rc" -eq 0 ] ||
        #     exit 1`, and a literal-only regex told the maintainer who wrote it
        #     that the check was broken. That is how a guard gets deleted rather
        #     than fixed. `exit 0` still does not count.
        #  3. THE EXIT MUST BE ABOUT THE RESULT. A bare echo plus an unrelated
        #     `[ -f Makefile ] || exit 1` in the same step satisfied a
        #     presence-only test. So the step must ALSO compare the shards'
        #     verdict to something -- the `success` literal, or a test on the
        #     variable carrying it. This is a shape check and not a proof of
        #     causation; what it buys is that a step certifying the tier has to
        #     contain both halves of the decision rather than one of them and an
        #     unrelated exit.
        #  4. THE STEP'S OWN `if`. The job-level condition is checked below,
        #     and a step-level one skips just as hard (Bugbot, #412). A step
        #     conditioned on the shards having SUCCEEDED is skipped exactly
        #     when a shard fails -- the only reader of the verdict does not
        #     run, later steps do, and the job reports success. That is the
        #     same skip-reports-success path `RUNS_ANYWAY` exists to close,
        #     one level down, so it takes the same allowlist: no `if` at all,
        #     or one that is exactly a runs-anyway form.
        step_cond = normalise_if(str(step.get("if") or ""))
        runs_anyway = step_cond == "" or step_cond in RUNS_ANYWAY
        fails = EXIT_NONZERO.search(run) is not None
        decides = DECIDES_ON_RESULT.search(run) is not None
        if fails and decides and runs_anyway and not step.get("continue-on-error"):
            enforcing.extend(hits)
    if not read:
        die(
            f"{REQUIRED_CONTEXT} needs {shard_ids} but no step reads "
            "`needs.<job>.result`. A shard whose verdict nothing reads is a runner "
            "that does not execute."
        )
    if not enforcing:
        die(
            f"{REQUIRED_CONTEXT} reads {sorted(set(read))} but no such step can FAIL "
            "on it. A certifying step needs THREE things and is missing one: an "
            "`exit` with a non-zero literal or a propagated status (`exit \"$rc\"` "
            "counts, `exit 0` does not), in EXECUTABLE shell rather than a comment; "
            "a comparison of the shards' verdict against `success` or a test on the "
            "variable holding it; and no `continue-on-error`. A fan-in that only "
            "logs the verdict, or that exits non-zero for an unrelated reason, "
            "enforces nothing and the required context goes green over a red "
            "shard. A step-level `if` outside "
            f"{sorted(RUNS_ANYWAY)} disqualifies it for the same reason the "
            "job-level one does: it skips precisely when a shard fails."
        )

    cond = normalise_if(str(rjob.get("if") or ""))
    if cond not in RUNS_ANYWAY:
        die(
            f"{REQUIRED_CONTEXT} has `if: {cond or '<none>'}`, which is not exactly one "
            f"of {sorted(RUNS_ANYWAY)}. A job whose `needs` FAILED is SKIPPED and "
            "GitHub reports a skipped required check as SUCCESS (backend#1424). Note a "
            "condition that merely CONTAINS always() is not enough: "
            "`always() && needs.<shard>.result == 'success'` is false exactly when a "
            "shard fails, so it skips precisely in the case it was meant to survive. "
            "Refusing rather than interpreting the expression."
        )

    derived = any(
        "print-mutation-targets" in str(step.get("run") or "")
        for jid in shard_ids
        for dep in ([jid] + needs_of(js[jid]))
        if isinstance(js.get(dep), dict)
        for step in (js[dep].get("steps") or [])
    )
    if not derived:
        die(
            "the shard matrix is not derived from `make print-mutation-targets`. A "
            "hand-written matrix drifts the moment MUTATION_TARGETS gains a runner, and "
            "the dropped runner still has a green required context beside it (.github#300)."
        )

    # …and no member may be named literally in EXECUTABLE yaml. Comments are stripped:
    # this workflow's own header explains .github#300 by naming a runner, and counting
    # commented prose as an invocation is the backend#2884 shape.
    executable = "\n".join(
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    named = sorted(t for t in targets if t in executable)
    if named:
        die(
            f"the workflow names individual runner(s) {named} in executable YAML. The "
            "sharded tier must derive its matrix, never enumerate members."
        )

    # AND THE SHARD MUST ACTUALLY RUN THE TARGET (Bugbot, #412). Everything
    # above establishes that the tier is WIRED: a derived matrix, a fan-in that
    # reads the shards' result and can fail on it, an `if` that survives a
    # failed dependency. None of it looks inside the shard for work. A matrix
    # of twelve legs whose steps only `echo` satisfies every check so far, and
    # the required `selftests` context would certify the tier as enforced.
    #
    # That is this repo's own defect class -- a mechanism that appears to
    # verify something and is connected to nothing -- in the guard written to
    # find it. The check is deliberately shallow: SOME step in the shard job
    # must invoke `make` on the matrix value. It does not try to prove the
    # invocation is correct, only that one exists; the `mutation-*` case guard
    # in that step is what constrains WHICH target, and `make` itself is what
    # fails when the target does not exist.
    invoking = []
    for jid in shard_ids:
        job = js.get(jid) or {}
        if "strategy" not in job:
            continue          # not a sharded leg; the matrix check above owns it
        for step in (job.get("steps") or []):
            # CODE ONLY, AND `make` MUST BE THE COMMAND (Bugbot, #412). The
            # first version of this check accepted any co-occurrence of `make`
            # and the matrix value in the RAW text -- so a whole-line comment
            # naming the target, or `echo "would run make $TARGET"`, certified
            # a leg that executes nothing.
            #
            # Written one function below the fan-in scan that already blanks
            # comments for exactly this reason, which is the whole lesson: the
            # rule was known here and applied to the neighbouring check and not
            # to this one. `MAKE_INVOCATION` anchors on the start of a line
            # (after optional `VAR=value` prefixes), so `echo`, `#` and any
            # other leading command no longer satisfy it.
            if MAKE_INVOCATION.search(shell_code_only(str(step.get("run") or ""))):
                invoking.append(jid)
                break
    sharded = [j for j in shard_ids
               if "strategy" in (js.get(j) or {})]
    missing = sorted(set(sharded) - set(invoking))
    if missing:
        die(
            f"shard job(s) {missing} run no `make` on their matrix value, so the "
            "tier is wired to legs that do no work. Every check above would "
            "still pass -- a derived matrix of steps that only echo reports the "
            "same green as one that runs the mutations, which is the "
            "appears-to-verify-nothing shape this script exists to refuse."
        )
    if not sharded:
        die(
            "no shard job carries a `strategy:` matrix, so the invocation check "
            "above compared an empty set against an empty set. A tier with no "
            "sharded leg is not the shape this script is written for -- say so "
            "rather than passing."
        )

    print(
        f"shard-shape: {REQUIRED_CONTEXT} enforces the sharded tier -- needs {shard_ids}; "
        f"a step reads {sorted(set(read))} and can fail on it; `if` is exactly "
        f"`{cond}`; matrix derived from make; {len(targets)} runner(s), none enumerated; "
        f"shard leg(s) {sorted(set(invoking))} invoke make on the matrix value."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
