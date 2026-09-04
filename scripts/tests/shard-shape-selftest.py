#!/usr/bin/env python3
"""Selftest for the shard-shape audit (backend#3157, .github#412).

WHY IT EXISTS. `scripts/shard-shape-check.py` asserts that the required
`selftests` context really enforces the sharded mutation tier. It shipped with
no fixture suite and no mutation runner, and the cost showed up immediately:
four negative cases in two review rounds, each a token the guard read where it
does not mean what it says, and Bugbot found one of them on the run right after
a reviewer named two others. @saadqbal's diagnosis is the design of this file --
*"the axis worth pinning isn't 'these two conditions' but 'does each token the
guard asserts actually DO the thing it's asserted for'"*.

Every sibling audit in this repo (`lint-targets-run-in-ci`, `bugbot-gate`,
`closing-ref-gate`, `conflict-gate`, `reason-citations`, `reusable-no-cancel`,
`standards-sync`) ships a selftest and a mutations file. This one now does too.

HERMETIC. Each case builds a workflow document by hand, writes it to a temp
file, points the real module's `WORKFLOW` at it, and calls the real `main()`.
No network, no `gh`. `mutation_targets()` shells out to `make`, so it is
stubbed per case -- that seam is exercised explicitly rather than being the
part nobody tests.

INPUTS ARE WRITTEN DOWN INDEPENDENTLY OF THE MATCHER (CLAUDE.md rule 9's
corollary). The shell fragments, the `success` literal and the runs-anyway
conditions are spelled out as literals below rather than imported from the
module -- iterating `RUNS_ANYWAY` to check `RUNS_ANYWAY` is self-consistent and
therefore blind. The one thing derived is the FIXTURE SHAPE: the passing case
is built from the same fragments the real `selftests.yml` uses, so a case that
stops resembling the real workflow is a case that stopped testing it.

BOTH DIRECTIONS FOR EVERY TOKEN. A guard that refuses too much is not safer --
`exit "$rc"` enforces harder than `[ "$rc" -eq 0 ] || exit 1` and was being
refused, which tells the maintainer who wrote the better version that the check
is broken. That is how a guard gets deleted rather than fixed, so every rule
here has an accept case beside its refuse case.
"""
import contextlib
import importlib.util
import io
import pathlib
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "shard-shape-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("shard_shape_check", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the fragments, literal ────────────────────────────────────────────────────

#: The fan-in body as `selftests.yml` really writes it: accumulate, then refuse.
FANIN_ACCUMULATE = """set -euo pipefail
rc=0
for pair in "mutation-shard:${SHARDS_RESULT}"; do
  res="${pair#*:}"
  if [ "$res" != "success" ]; then
    echo "::error::a shard did not pass" >&2
    rc=1
  fi
done
[ "$rc" -eq 0 ] || exit 1
echo "mutation tier: every shard succeeded."
"""

#: The idiomatic alternative, which enforces at least as hard.
FANIN_PROPAGATE = FANIN_ACCUMULATE.replace(
    '[ "$rc" -eq 0 ] || exit 1', 'exit "$rc"')

#: Teeth removed, the exit surviving only as prose.
FANIN_COMMENTED = FANIN_ACCUMULATE.replace(
    '[ "$rc" -eq 0 ] || exit 1', '# previously: exit 1')

#: Logs the verdict, exits non-zero for something else entirely.
FANIN_UNRELATED = """set -euo pipefail
echo "shards=${SHARDS_RESULT}"
[ -f Makefile ] || exit 1
"""

#: Reads the verdict and cannot fail on it at all.
FANIN_LOG_ONLY = """set -euo pipefail
echo "shards=${SHARDS_RESULT}"
"""

SHARD_RUNS_MAKE = """set -euo pipefail
case "$TARGET" in
  mutation-*) ;;
  *) echo "::error::not a mutation-* target" >&2; exit 1 ;;
esac
make "$TARGET"
"""

SHARD_ECHOES = """set -euo pipefail
echo "would run $TARGET"
"""


#: A shard leg that NAMES the invocation without performing it.
SHARD_ECHOES_MAKE = """set -euo pipefail
# make "$TARGET"   <- what this used to do
echo "would run: make $TARGET"
"""


# ── DERIVED FROM THE PROPERTY, NOT TRANSCRIBED FROM A REVIEW ────────────────
#
# `SHARD_TRAILING_COMMENT` and `SHARD_PROSE` used to be the exact strings
# @saadqbal drove, copied in. He named the cost on round four: "the guard fix
# generalises; the fixtures pin the instances. That asymmetry is where both of
# today's findings come from."
#
# The property is: **a token this guard asserts must be read where it
# EXECUTES.** There are only so many ways a token can appear without executing,
# and they are the same three wherever the token is — so they are generated
# from one function and applied to every token, rather than written out per
# instance. A fourth spelling added here is tested against every token at once.
NON_EXECUTING = {
    "whole-line comment": lambda t: f'# {t}\necho nothing\n',
    "trailing comment": lambda t: f'echo nothing   # {t}\n',
    "prose in an echo": lambda t: f'echo "we would {t} eventually"\n',
}


def not_executing(token, how):
    """A `run` body that CONTAINS `token` and does not execute it."""
    return "set -euo pipefail\n" + NON_EXECUTING[how](token)


#: The token each side is asserted on, so the cases below name a property
#: rather than a string somebody typed.
MAKE_TOKEN = 'make "$TARGET"'
EXIT_TOKEN = 'exit 1'
#: The third token, added when @saadqbal reproduced Bugbot's finding that the
#: derivation check was the last `run` scan still reading raw text. It is a
#: TOKEN here rather than a fixture precisely so it inherits every spelling in
#: `NON_EXECUTING` -- which is the generalisation the previous round bought.
DERIVE_TOKEN = 'make --no-print-directory print-mutation-targets'
#: …AND THE SAME TOKEN AS IT REALLY APPEARS (Bugbot, #412, high). The bare
#: token above is not the shape that ships: the real line is a PROCESS
#: SUBSTITUTION, and `DERIVES_MATRIX` must treat `(` as command position for it
#: to match at all. A commented-out copy therefore carries its own `(` -- so
#: the bare spelling was refused while the real one, commented out, certified.
#: Every non-executing case runs against BOTH spellings now: a fixture that
#: strips the token down to its name cannot see a hole that only the real
#: punctuation opens.
DERIVE_REAL = ('mapfile -t targets < '
               '<(make --no-print-directory print-mutation-targets)')


def workflow(fanin=FANIN_ACCUMULATE, shard_run=SHARD_RUNS_MAKE,
             cond="always()", continue_on_error=False, derived=True,
             matrix=True, step_if=None, shard_continue=False,
             make_step_if=None, derive_run=None):
    """A `selftests.yml`-shaped document, one knob per rule under test."""
    matrix_job = {
        "runs-on": "ubuntu-latest",
        "steps": [{
            "name": "shards",
            "id": "plan",
            # `derive_run` overrides both branches, so a body that CONTAINS
            # the token without executing it can be tested. `derived` alone
            # could only ever say present-or-absent.
            "run": (derive_run if derive_run is not None else
                    ("make --no-print-directory print-mutation-targets"
                     if derived else "echo '[\"mutation-a\"]'")),
        }],
        "outputs": {"shards": "${{ steps.plan.outputs.shards }}"},
    }
    shard_job = {
        "runs-on": "ubuntu-latest",
        # THE EDGE MATTERS: the derived-matrix check walks the shard job's own
        # `needs` to find the step that reads `print-mutation-targets`, so a
        # fixture without this edge fails the derivation check before reaching
        # whatever it was written to test. Getting this wrong cost four
        # false failures on the first run of this file.
        "needs": ["mutation-matrix"],
        "env": {"TARGET": "${{ matrix.target }}"},
        "steps": [dict({"name": "make ${{ matrix.target }}", "run": shard_run},
                       **({"if": make_step_if} if make_step_if else {}))],
    }
    if shard_continue:
        shard_job["continue-on-error"] = True
    if matrix:
        shard_job["strategy"] = {
            "fail-fast": False,
            "matrix": {
                "target": "${{ fromJson(needs.mutation-matrix.outputs.shards) }}"},
        }
    step = {
        "name": "mutation tier (fan-in)",
        "env": {"SHARDS_RESULT": "${{ needs.mutation-shard.result }}"},
        "run": fanin,
    }
    if continue_on_error:
        step["continue-on-error"] = True
    if step_if is not None:
        step["if"] = step_if
    return {
        "name": "Selftests",
        "on": {"pull_request": None},
        "jobs": {
            "mutation-matrix": matrix_job,
            "mutation-shard": shard_job,
            "selftests": {
                "runs-on": "ubuntu-latest",
                "needs": ["mutation-matrix", "mutation-shard"],
                "if": cond,
                "steps": [step],
            },
        },
    }


#: The serial shape's token. `main` has TWO supported shapes and the selftest
#: only ever built one of them, which is how the serial arm shipped as a
#: bypass: every case here exercised SHAPE B and returned before SHAPE A.
SERIAL_TOKEN = "make mutations"


def serial_workflow(run_body=None, cond="always()", job_continue=False,
                    step_continue=False, step_if=None):
    """The OTHER supported shape: the required job runs `make mutations` itself.

    No matrix and no shard job, so anything that stops SHAPE A certifying must
    fall through to SHAPE B and be refused there -- which is the property the
    cases below assert, and the reason a refusal here is never a false green.
    """
    step = {"name": "mutations (serial)",
            "run": run_body if run_body is not None
            else "set -euo pipefail\nmake mutations\n"}
    if step_continue:
        step["continue-on-error"] = True
    if step_if is not None:
        step["if"] = step_if
    job = {"runs-on": "ubuntu-latest", "if": cond, "steps": [step]}
    if job_continue:
        job["continue-on-error"] = True
    return {"name": "Selftests", "on": {"pull_request": None},
            "jobs": {"selftests": job}}


def run(doc, targets=("mutation-alpha", "mutation-beta")):
    """`(rc, output)` from the real `main()` over `doc`."""
    import yaml
    mod = _load()
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "selftests.yml"
        path.write_text(yaml.dump(doc), encoding="utf-8")
        mod.WORKFLOW = path
        mod.mutation_targets = lambda: list(targets)
        buf = io.StringIO()
        rc = 0
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                rc = mod.main()
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, buf.getvalue()


# ── the cases ─────────────────────────────────────────────────────────────────

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("the real shape certifies")
def _():
    rc, out = run(workflow())
    assert rc == 0, out
    assert "enforces the sharded tier" in out, out


@case("a propagated exit certifies too -- refusing it punished an improvement")
def _():
    rc, out = run(workflow(fanin=FANIN_PROPAGATE))
    assert rc == 0, (
        "`exit \"$rc\"` is the idiomatic accumulate-then-propagate form and "
        "enforces at least as hard as the literal; refusing it tells the "
        "maintainer who wrote it that the guard is broken.\n" + out)


@case("an exit surviving only in a comment is refused")
def _():
    rc, out = run(workflow(fanin=FANIN_COMMENTED))
    assert rc == 1, ("a shell comment is prose; counting it as executable "
                     "certifies a step whose teeth are gone\n" + out)
    assert "can FAIL on it" in out, out


@case("an unrelated exit beside a bare echo is refused")
def _():
    rc, out = run(workflow(fanin=FANIN_UNRELATED))
    assert rc == 1, ("`[ -f Makefile ] || exit 1` exits non-zero for a reason "
                     "that has nothing to do with the shards' verdict\n" + out)
    assert "can FAIL on it" in out, out


@case("a log-only fan-in is refused")
def _():
    rc, out = run(workflow(fanin=FANIN_LOG_ONLY))
    assert rc == 1, out
    assert "can FAIL on it" in out, out


@case("continue-on-error on the certifying step is refused")
def _():
    rc, out = run(workflow(continue_on_error=True))
    assert rc == 1, ("continue-on-error turns the failure back into a pass, so "
                     "the exit is decorative\n" + out)


@case("a certifying step skipped by its OWN `if` is refused")
def _():
    rc, out = run(workflow(
        step_if="${{ needs.mutation-shard.result == 'success' }}"))
    assert rc == 1, (
        "a step conditioned on the shards having SUCCEEDED is skipped exactly "
        "when one fails -- the only reader of the verdict does not run, later "
        "steps do, and the job reports success. The job-level check cannot see "
        "this because it looks at the JOB's `if`.\n" + out)
    assert "can FAIL on it" in out, out


@case("a runs-anyway step `if` is accepted")
def _():
    """The other direction: a step may carry a condition, just not that one."""
    rc, out = run(workflow(step_if="always()"))
    assert rc == 0, ("a step-level `always()` does not skip on a failed shard "
                     "and must not disqualify the step\n" + out)


@case("a shard leg that only NAMES the make invocation is refused")
def _():
    rc, out = run(workflow(shard_run=SHARD_ECHOES_MAKE))
    assert rc == 1, (
        "`# make \"$TARGET\"` in a comment and `echo \"would run: make "
        "$TARGET\"` both contain the tokens; neither executes anything. "
        "Co-occurrence is not invocation.\n" + out)
    assert "run no `make`" in out, out


@case("no non-executing spelling of `make` certifies a shard leg")
def _():
    """Every way a token can appear without executing, against the make token.

    Generated rather than transcribed, so adding a fourth spelling to
    `NON_EXECUTING` tests it here AND against the exit token below, with no
    second edit. That is the asymmetry @saadqbal named: a fixture that pins
    the instance leaves the next spelling to be found in review.
    """
    for how in NON_EXECUTING:
        rc, out = run(workflow(shard_run=not_executing(MAKE_TOKEN, how)))
        assert rc == 1, (f"a shard leg with `{MAKE_TOKEN}` as a {how} "
                         f"certifies; co-occurrence is not invocation\n" + out)
        assert "run no `make`" in out, (how, out)


@case("no non-executing spelling of `exit` certifies the fan-in")
def _():
    """The same three spellings, against the token on the other side.

    The trailing-comment case here is the one that shipped: `shell_code_only`
    blanks WHOLE-LINE comments (correctly), and `EXIT_NONZERO` was unanchored,
    so `echo "tier checked"   # exit 1` satisfied it while nothing exited.
    """
    for how in NON_EXECUTING:
        body = ("set -euo pipefail\nrc=0\n"
                'if [ "${SHARDS_RESULT}" != "success" ]; then rc=1; fi\n'
                + NON_EXECUTING[how](EXIT_TOKEN))
        rc, out = run(workflow(fanin=body))
        assert rc == 1, (f"a fan-in whose only `{EXIT_TOKEN}` is a {how} "
                         f"certifies; nothing exits\n" + out)
        assert "can FAIL on it" in out, (how, out)


@case("no non-executing spelling of the derivation certifies the matrix")
def _():
    """The third token, and the one that was still read as raw text.

    @saadqbal reproduced Bugbot's finding: comment the `mapfile` out, source
    the shard list from a file naming no member, and the guard printed
    "matrix derived from make" and returned 0. The matrix was then hand-written
    -- exactly what this rule exists to refuse -- while the rule certified it.

    It is generated over `NON_EXECUTING` rather than pinned to the mapfile
    spelling @saadqbal happened to drive, for the reason that block states: a
    fixture pins the instance, and the next spelling waits to be found in
    review.
    """
    for token in (DERIVE_TOKEN, DERIVE_REAL):
        for how in NON_EXECUTING:
            rc, out = run(workflow(derive_run=not_executing(token, how)))
            assert rc == 1, (
                f"a matrix job with `{token}` as a {how} certifies as "
                "derived; the shards are hand-written and drift the moment "
                "MUTATION_TARGETS gains a runner\n" + out)
            assert "not derived from" in out, (token, how, out)


@case("a fan-in that only MENTIONS success is refused")
def _():
    """The literal must sit in a comparison, not merely in the step.

    Bugbot raised this as `succeeded` satisfying the unbounded literal. That
    example is wrong -- `succeeded` does not contain `success` -- but three
    spellings it did not name do, and the third is the one a word boundary
    cannot reach:

      * `successful` / `successfully`  -- a boundary stops these
      * the BARE WORD in prose         -- a boundary does NOT

    and this repo's own fan-in carries the bare word one line above its real
    decision. Each fixture exits non-zero for an UNRELATED reason, so
    `EXIT_NONZERO` is satisfied and only the decision half is missing: that
    isolates `DECIDES_ON_RESULT` instead of passing for its neighbour's reason.
    """
    for label, line in (
            ("bare word in prose", 'echo "a shard did not report success"'),
            ("successful",         'echo "the tier was successful"'),
            ("successfully",       'echo "every shard ran successfully"')):
        body = ("set -euo pipefail\n" + line + "\n"
                "if [ ! -f report.json ]; then exit 1; fi\n")
        rc, out = run(workflow(fanin=body))
        assert rc == 1, (
            f"a fan-in whose only `success` is {label} decides nothing about "
            "the shards, so the required context can go green over a red "
            "shard\n" + out)


@case("a fan-in whose only `success` comparison is not executed is refused")
def _():
    """The decision half, through the same three spellings as everything else.

    `DECIDES_ON_RESULT` was read off `shell_code_only` text, which keeps
    trailing comments AND quoted strings, and the pattern had no structural
    anchor -- so `exit 1  # [ "$X" != "success" ]` and an echo mentioning the
    comparison both certified (Bugbot, #412, high).

    Two mechanisms are needed here and neither alone suffices, which is the
    point worth pinning: the comparison must be inside a TEST (`[ … ]`), which
    is what rejects the prose spelling, AND comments must be cut, which is what
    rejects the commented-out spelling -- brackets and all. Quotes are
    deliberately NOT blanked, because the legitimate form quotes its literal.
    """
    for label, line in (
            ("a trailing comment", 'exit 1   # [ "$SHARDS_RESULT" != "success" ]'),
            ("a whole-line comment", '# [ "$SHARDS_RESULT" != "success" ]\nexit 1'),
            ("prose in an echo",
             'echo "we compare != '+chr(39)+'success'+chr(39)+' further down"'),
            ("prose with no quotes at all",
             'echo "the shard result != success is what we check"')):
        body = ("set -euo pipefail\n" + line + "\n"
                "if [ ! -f report.json ]; then exit 1; fi\n")
        rc, out = run(workflow(fanin=body))
        assert rc == 1, (
            f"a fan-in whose only `success` comparison is {label} decides "
            "nothing; the required context can go green over a red shard\n"
            + out)


@case("a fan-in that enumerates FAILURE values is refused")
def _():
    """backend#1424, one level down (Bugbot, #412, high).

    A fan-in comparing against a failure value passes everything that is not
    that value -- and a SKIPPED shard reports `skipped`, not `failure`. GitHub
    reports a skipped required job as SUCCESS, which is the precise hole this
    required context exists to close, so a fan-in that closes it only against
    `failure` leaves it open against the case that actually happens.

    `success` is the only value meaning the tier passed, so it is the only one
    worth comparing against.
    """
    for form in ('[ "$SHARDS_RESULT" = "failure" ]',
                 '[ "$SHARDS_RESULT" = "cancelled" ]',
                 '[ "$SHARDS_RESULT" != "success_maybe" ]'):
        body = ("set -euo pipefail\n"
                f"if {form}; then exit 1; fi\n")
        rc, out = run(workflow(fanin=body))
        assert rc == 1, (
            f"`{form}` treats every other result -- `skipped` and `cancelled` "
            "included -- as a pass, so a shard that never ran certifies the "
            "tier\n" + out)


@case("a real comparison against the literal still certifies")
def _():
    """NON-VACUITY for the rule above: the legitimate shape must survive.

    Without this, tightening `DECIDES_ON_RESULT` to something nothing can
    satisfy would look identical to tightening it correctly.
    """
    for form in ('[ "$SHARDS_RESULT" != "success" ]',
                 '[ "$SHARDS_RESULT" = success ]'):
        body = ("set -euo pipefail\n"
                f"if {form}; then exit 1; fi\n")
        rc, out = run(workflow(fanin=body))
        assert rc == 0, (f"`{form}` IS a decision on the shards' result and "
                         "must certify\n" + out)


# ── SHAPE A: the serial tier, which had no cases at all until Bugbot ────────
#
# `main` supports two shapes and returns 0 from the first one that matches.
# Every case above builds the SHARDED shape, so the serial arm was never once
# driven -- and it returned success on the raw text of any step naming the
# token, skipping all four teeth the sharded shape is held to. An unexercised
# arm of a guard is not a lesser risk than an unexercised guard; it is the
# same risk behind a green suite.


@case("a clean serial tier still certifies")
def _():
    """NON-VACUITY. Without this the five refusals below could all pass by
    refusing everything, which is a guard nobody can satisfy."""
    rc, out = run(serial_workflow())
    assert rc == 0, "the serial shape is supported and must certify\n" + out
    assert "serial tier" in out, out


@case("`make mutations-dry` does not certify the serial tier")
def _():
    """A DRY RUN IS NOT THE TIER (Bugbot, #412, high -- and a regression I made).

    CLAUDE.md is explicit: `make mutations-dry` "only proves the markers still
    resolve and is NOT evidence that anything still reddens." The ORIGINAL
    raw-text arm ended `mutations(\\s|$)` and rejected it correctly; rewriting
    that as `\\b` while fixing the arm's other holes accepted it, because
    `s`->`-` IS a word boundary. Every other tooth got sharper in that commit
    and this one got blunter, which is why it is pinned here rather than just
    corrected.
    """
    for target in ("make mutations-dry", "make mutations-dry --verbose"):
        rc, out = run(serial_workflow(
            run_body=f"set -euo pipefail\n{target}\n"))
        assert rc == 1, (
            f"`{target}` resolves markers and reddens nothing, so certifying "
            "on it is a green required context over a tier that was never "
            "actually run\n" + out)


@case("a serial tier whose failure is swallowed is refused")
def _():
    for tail in ("|| true", "|| :", "|| echo 'ignored'"):
        rc, out = run(serial_workflow(
            run_body=f"set -euo pipefail\nmake mutations {tail}\n"))
        assert rc == 1, (f"`make mutations {tail}` cannot red the job, so the "
                         "required context is green over a failed tier\n" + out)


@case("a continue-on-error serial job is refused")
def _():
    rc, out = run(serial_workflow(job_continue=True))
    assert rc == 1, "a continue-on-error job reports success regardless\n" + out
    assert "continue-on-error" in out, out


@case("a continue-on-error serial step is refused")
def _():
    rc, out = run(serial_workflow(step_continue=True))
    assert rc == 1, "the step's failure would not red the job\n" + out


@case("a skippable serial step is refused")
def _():
    rc, out = run(serial_workflow(step_if="${{ github.event_name == 'push' }}"))
    assert rc == 1, ("a skipped step does not red its job, so every PR would "
                     "carry a green required context over a tier that never "
                     "ran\n" + out)


@case("a serial job that can be skipped wholesale is refused")
def _():
    rc, out = run(serial_workflow(cond="${{ github.event_name == 'push' }}"))
    assert rc == 1, ("GitHub reports a skipped required job as SUCCESS "
                     "(backend#1424)\n" + out)


@case("no non-executing spelling of the serial invocation certifies")
def _():
    """The third token through `NON_EXECUTING`, for the third time.

    Same generator as the make and exit tokens, so the serial arm inherits
    every spelling the other two are tested against rather than getting its
    own hand-written pair.
    """
    for how in NON_EXECUTING:
        rc, out = run(serial_workflow(
            run_body=not_executing(SERIAL_TOKEN, how)))
        assert rc == 1, (f"`{SERIAL_TOKEN}` as a {how} certifies the serial "
                         "tier; co-occurrence is not invocation\n" + out)


@case("a verdict comparison that lives only in a comment is refused")
def _():
    """The half `shell_code_only` still protects on the fan-in side.

    Once `EXIT_NONZERO` was anchored in command position, a commented-out
    `exit 1` stopped matching whether or not comments were blanked -- the
    anchoring subsumed that case. `DECIDES_ON_RESULT` is where the blanking
    still does work: a step whose only `!= "success"` sits in a comment, with
    a real but UNRELATED exit, would certify without it.
    """
    body = ("set -euo pipefail\n"
            'echo "shards=${SHARDS_RESULT}"\n'
            '# if [ "$res" != "success" ]; then rc=1; fi\n'
            "[ -f Makefile ] || exit 1\n")
    rc, out = run(workflow(fanin=body))
    assert rc == 1, ("the only comparison against `success` is commented out, "
                     "so nothing here decides on the shards' verdict\n" + out)
    assert "can FAIL on it" in out, out


@case("a make step skippable by its own `if` is refused")
def _():
    """The mirror of the fan-in rule, which the shard scan did not have.

    A skipped step does not red its job, so a leg gated on
    `github.event_name == 'push'` is green on every PR, the fan-in reads
    `success`, and the tier runs nothing behind a green required context
    (@saadqbal, #412). Both directions: `always()` on the same step certifies.
    """
    for cond in ("${{ github.event_name == 'push' }}", "${{ false }}"):
        rc, out = run(workflow(make_step_if=cond))
        assert rc == 1, (f"a make step gated on `{cond}` certifies\n" + out)
        assert "run no `make`" in out, (cond, out)
    rc, out = run(workflow(make_step_if="always()"))
    assert rc == 0, ("a runs-anyway step condition must not disqualify the "
                     "leg\n" + out)


@case("a continue-on-error shard job is refused")
def _():
    """The third root: same token, checked in one place and not the other.

    `continue-on-error` disqualified the CERTIFYING step from the start and
    was unchecked on the shard job -- where it makes a failed leg report
    `success` to the fan-in, satisfying the fan-in's assertion by
    construction (@saadqbal, #412).
    """
    rc, out = run(workflow(shard_continue=True))
    assert rc == 1, out
    assert "continue-on-error" in out, out


@case("a shard whose steps only echo is refused")
def _():
    rc, out = run(workflow(shard_run=SHARD_ECHOES))
    assert rc == 1, ("a derived matrix of legs that run no `make` satisfies "
                     "every wiring check and executes nothing\n" + out)
    assert "run no `make`" in out, out


@case("a compound runs-anyway condition is refused, not interpreted")
def _():
    rc, out = run(workflow(
        cond="always() && needs.mutation-shard.result == 'success'"))
    assert rc == 1, ("that condition CONTAINS always() and is false exactly "
                     "when a shard fails, so the job skips in the one case it "
                     "exists to survive\n" + out)
    assert "not exactly one of" in out, out


@case("!cancelled() certifies as well as always()")
def _():
    rc, out = run(workflow(cond="!cancelled()"))
    assert rc == 0, out


@case("a hand-written matrix is refused")
def _():
    rc, out = run(workflow(derived=False))
    assert rc == 1, out
    assert "not derived" in out, out


@case("a required job with no matrix job at all is refused")
def _():
    doc = workflow(matrix=False)
    rc, out = run(doc)
    assert rc == 1, ("without a `strategy:` matrix this is neither supported "
                     "shape, and must not certify\n" + out)


@case("zero derived targets never reads as a healthy tier (main)")
def _():
    """Found a real hole: `main` trusted its producer to refuse zero.

    `mutation_targets()` dies on an empty list, and this case stubs that
    function -- so the first version of it certified "0 runner(s), none
    enumerated" at rc 0. Testing "zero targets" by stubbing past the function
    that refuses zero targets was testing nothing, and the fix was to refuse
    it in `main` as well. Both places now, which is what defence in depth is
    for when the property is "nothing to check must not read as passed".
    """
    rc, out = run(workflow(), targets=())
    assert rc == 1, ("nothing-to-check must not mean everything-passed\n" + out)


@case("zero derived targets is refused at the producer too")
def _():
    """The other half, with the REAL `mutation_targets` and `make` stubbed.

    Stubs the subprocess rather than the function, so the refusal under test
    is the one that ships.
    """
    mod = _load()
    mod.subprocess.run = lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": "  \n", "stderr": ""})()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            mod.mutation_targets()
            rc = 0
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    assert rc == 1, buf.getvalue()
    assert "runs nothing" in buf.getvalue(), buf.getvalue()


@case("a failing `make` is a finding, not an empty tier")
def _():
    mod = _load()
    mod.subprocess.run = lambda *a, **k: type(
        "P", (), {"returncode": 2, "stdout": "", "stderr": "no such target"})()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            mod.mutation_targets()
            rc = 0
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    assert rc == 1, buf.getvalue()
    assert "failed" in buf.getvalue(), buf.getvalue()


@case("an unreadable workflow is a CLEAN finding, not a traceback")
def _():
    """A refusal, with its own message -- not an exception escaping `main`.

    CATCHES BROADLY ON PURPOSE. Delete the `is_file()` guard and the module
    raises `FileNotFoundError` out of `main` instead of dying with its own
    words. Catching only `SystemExit` let that escape, and the mutation runner
    then reported "harness broke, not detected" -- an uncaught mutation for a
    reason that was really this case being too narrow. A guard that crashes has
    not "reported a finding": the operator sees a traceback rather than a
    sentence naming what could not be read, which is precisely the difference
    between refusing and falling over.
    """
    mod = _load()
    mod.WORKFLOW = pathlib.Path("/nonexistent/selftests.yml")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = mod.main()
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        except BaseException as exc:            # noqa: BLE001 - see docstring
            raise AssertionError(
                f"`main` raised {type(exc).__name__} instead of refusing with a "
                "message. An unreadable workflow must be a stated finding, not "
                "a traceback."
            ) from None
    assert rc == 1, buf.getvalue()
    assert "unreadable" in buf.getvalue(), buf.getvalue()


@case("the fixture really is the shape the repo ships")
def _():
    """Non-vacuity, and the one case that is DERIVED rather than literal.

    Every refusal above is only meaningful if the passing fixture resembles
    `selftests.yml`. If the real workflow's fan-in stops matching the fragment
    this file builds from, these cases go on passing about a shape nobody
    ships -- so compare the two directly.
    """
    import yaml
    real = yaml.safe_load(
        (ROOT / ".github/workflows/selftests.yml").read_text(encoding="utf-8"))
    step = next(
        s for s in real["jobs"]["selftests"]["steps"]
        if "result" in yaml.dump(s))
    run_text = str(step.get("run") or "")
    for fragment in ('!= "success"', "rc=1", "exit"):
        assert fragment in run_text, (
            f"the real fan-in no longer contains {fragment!r}, so the "
            "FANIN_ACCUMULATE fixture above has stopped resembling it and "
            "every case in this file is about a workflow nobody ships")


def main() -> int:
    failed = 0
    for name, fn in CASES:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\nshard-shape-selftest: {len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
