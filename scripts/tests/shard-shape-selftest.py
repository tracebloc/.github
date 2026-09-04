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


def workflow(fanin=FANIN_ACCUMULATE, shard_run=SHARD_RUNS_MAKE,
             cond="always()", continue_on_error=False, derived=True,
             matrix=True, step_if=None, shard_continue=False,
             make_step_if=None):
    """A `selftests.yml`-shaped document, one knob per rule under test."""
    matrix_job = {
        "runs-on": "ubuntu-latest",
        "steps": [{
            "name": "shards",
            "id": "plan",
            "run": ("make --no-print-directory print-mutation-targets"
                    if derived else "echo '[\"mutation-a\"]'"),
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
