#!/usr/bin/env python3
"""Cases for the mutation harnesses' baseline guard (backend#2441).

THE CASE THIS FILE EXISTS FOR is `dirty working tree refuses`. Every
`*-mutations.py` here overwrites a tracked file and restores it in a `finally`,
and the ticket is about the runs where the `finally` never happens -- SIGKILL, a
runner timeout, a second harness racing the first. The mutated text is then on
disk, the next run adopts it as its baseline, and reports `0 uncaught` about a
premise nobody typed.

So the assertions come in pairs, deliberately: every refusal is paired with the
neighbouring situation that must NOT refuse. A guard that refuses everything
protects nothing, and the log of a guard that always refuses is
indistinguishable from the log of one that is working.

Real git repositories, built and thrown away per case. No network, no `gh`, and
nothing in this repo's own tree is touched.
"""
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from mutation_baseline import (  # noqa: E402
    DirtyBaseline,
    assert_pristine,
    guard,
)
import mutation_baseline as _mb  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]

P, F = 0, 0

print("mutation-baseline-selftest: the baseline guard for scripts/tests/*-mutations.py")


def ok(m):
    global P
    P += 1
    print("PASS  %s" % m)


def bad(m):
    global F
    F += 1
    print("FAIL  %s" % m)


def refuses(what, root, paths, *needles):
    """assert_pristine must raise, and the message must SAY WHICH and WHY.

    The needles are the point. `assertRaises(Exception)` would pass on any
    refusal at all, so a case could go on green while exercising a different arm
    than its name describes -- rule 10.
    """
    try:
        assert_pristine(root, paths)
    except DirtyBaseline as exc:
        missing = [n for n in needles if n not in str(exc)]
        if missing:
            bad("%s: refused, but the message omits %r -- %r"
                % (what, missing, str(exc)[:160]))
        else:
            ok("%s: refused, naming %s" % (what, " + ".join(repr(n) for n in needles)))
        return
    except Exception as exc:  # noqa: BLE001 -- any other exception is a defect
        bad("%s: raised %s, not DirtyBaseline (%s)" % (what, type(exc).__name__, exc))
        return
    bad("%s: did NOT refuse" % what)


def accepts(what, root, paths):
    try:
        assert_pristine(root, paths)
    except Exception as exc:  # noqa: BLE001
        bad("%s: refused a clean baseline (%s)" % (what, str(exc)[:160]))
        return
    ok("%s: accepted" % what)


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root),
         "-c", "user.email=selftest@example.invalid",
         "-c", "user.name=selftest",
         "-c", "commit.gpgsign=false",
         *args],
        capture_output=True, text=True,
    )


def make_repo(work, name="repo", body="original\n"):
    """A repo with one committed file, `guard.txt`, holding `body`."""
    root = pathlib.Path(work) / name
    (root / "scripts").mkdir(parents=True)
    target = root / "scripts" / "guard.txt"
    target.write_text(body, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root, target


with tempfile.TemporaryDirectory() as work:
    # --- the pair the ticket is about -------------------------------------
    root, target = make_repo(work, "clean")
    accepts("a tracked file identical to HEAD", root, [target])

    root, target = make_repo(work, "dirty")
    target.write_text("MUTATED left behind by a killed run\n", encoding="utf-8")
    refuses("a working-tree file that differs from HEAD", root, [target],
            "scripts/guard.txt", "git checkout -- scripts/guard.txt",
            "backend#2441")

    # ... and the same corruption STAGED. `git diff` with no `HEAD` compares
    # against the index, which a staged mutation matches -- so the comparison
    # has to be against HEAD or this arm reads as clean.
    root, target = make_repo(work, "staged")
    target.write_text("MUTATED and staged\n", encoding="utf-8")
    git(root, "add", "-A")
    refuses("a staged modification", root, [target], "differs from its committed")

    # ... while a COMMITTED change is the sanctioned way to work on a guarded
    # file and must not refuse. Without this the guard would make editing any
    # mutation target impossible, which is how a guard gets commented out.
    root, target = make_repo(work, "committed")
    target.write_text("a deliberate, committed edit\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "deliberate")
    accepts("a committed change to the target", root, [target])

    # --- cannot tell is a finding, not a pass ------------------------------
    root, target = make_repo(work, "untracked")
    stray = root / "scripts" / "stray.txt"
    stray.write_text("never added\n", encoding="utf-8")
    refuses("an untracked target", root, [stray], "not tracked by git", "stray.txt")

    root, target = make_repo(work, "missing")
    target.unlink()
    refuses("a target that does not exist", root, [target], "could not be read")

    if os.name == "posix" and os.geteuid() != 0:
        root, target = make_repo(work, "unreadable")
        target.chmod(0o000)
        refuses("a target that cannot be read", root, [target], "could not be read")
        target.chmod(0o644)
    else:
        print("SKIP  unreadable-target case (running as root, or not POSIX)")

    # A repository git cannot open. Deliberately a CORRUPT `.git` rather than a
    # plain non-repo directory: git walks UP from the path it is given, so a
    # bare directory would resolve to whatever repository happens to contain
    # the temp dir, and the case would assert a different arm on someone
    # else's machine.
    nogit = pathlib.Path(work) / "notarepo"
    (nogit / "scripts").mkdir(parents=True)
    (nogit / ".git").write_text("not a gitfile\n", encoding="utf-8")
    (nogit / "scripts" / "guard.txt").write_text("x\n", encoding="utf-8")
    refuses("a repository git cannot open", nogit,
            [nogit / "scripts" / "guard.txt"], "no committed content")

    # A repo with a HEAD that resolves to nothing: init, no commit.
    bare = pathlib.Path(work) / "nocommits"
    (bare / "scripts").mkdir(parents=True)
    (bare / "scripts" / "guard.txt").write_text("x\n", encoding="utf-8")
    git(bare, "init", "-q")
    refuses("a repository with no commit yet", bare,
            [bare / "scripts" / "guard.txt"], "no committed content")

    root, target = make_repo(work, "outside")
    other = pathlib.Path(work) / "elsewhere.txt"
    other.write_text("x\n", encoding="utf-8")
    refuses("a target outside the repository", root, [other], "is outside")

    # A `git` that will not run AT ALL. Absence from a comparison that was never
    # made proves nothing, so this must refuse rather than fall through to the
    # clean arm -- the same repo whose PATH is emptied is clean above.
    root, target = make_repo(work, "nogitbinary")
    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = str(pathlib.Path(work) / "no-such-bin")
    try:
        refuses("git that cannot be executed", root, [target], "could not be run")
    finally:
        os.environ["PATH"] = saved
    accepts("the same repo once git is back on PATH", root, [target])

    # A git that exits NEITHER 0 ("matches") NOR 1 ("differs"). No real
    # repository produces one on demand, so this one seam is stubbed -- without
    # the case, an arm that reads any unexpected status as a match would sit
    # here untested, and a git failing halfway would report a clean baseline.
    root, target = make_repo(work, "weirdrc")

    class _Proc:
        def __init__(self, rc):
            self.returncode, self.stdout = rc, ""

    real_run = _mb.subprocess.run

    def fake_run(args, **kw):
        return _Proc(129) if "diff" in args else real_run(args, **kw)

    _mb.subprocess.run = fake_run
    try:
        refuses("a git comparison that exits neither 0 nor 1", root, [target],
                'neither "matches"', "129")
    finally:
        _mb.subprocess.run = real_run

    # --- several targets: one dirty is enough ------------------------------
    root, target = make_repo(work, "twofiles")
    second = root / "scripts" / "other.txt"
    second.write_text("second\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "second")
    accepts("two clean targets", root, [target, second])
    second.write_text("MUTATED\n", encoding="utf-8")
    refuses("two targets, the SECOND of them dirty", root, [target, second],
            "scripts/other.txt")

    # --- guard(): the call site the harnesses actually use -----------------
    root, target = make_repo(work, "guardapi")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = guard(root, [target])
    if rc == 0 and err.getvalue() == "":
        ok("guard() returns 0 and says nothing on a clean baseline")
    else:
        bad("guard() returned %r / wrote %r on a clean baseline" % (rc, err.getvalue()[:120]))

    target.write_text("MUTATED\n", encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = guard(root, [target])
    # `::error::` is what makes the refusal visible in an Actions log rather than
    # scrolling past as ordinary output, so it is asserted, not assumed.
    if rc == 2 and err.getvalue().startswith("::error::") and "guard.txt" in err.getvalue():
        ok("guard() returns 2 and annotates the refusal on a dirty baseline")
    else:
        bad("guard() returned %r / wrote %r on a dirty baseline, want 2 and an "
            "::error:: naming the file" % (rc, err.getvalue()[:120]))

    # guard()'s caller overwrites a tracked file on the next statement, so an
    # UNEXPECTED exception out of the check must land as a refusal too -- not as
    # a traceback the harness never sees a return value from. The collaborator is
    # stubbed because there is no honest input that produces an arbitrary bug.
    real_assert = _mb.assert_pristine
    _mb.assert_pristine = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            rc = guard(root, [target])
    except Exception as exc:  # noqa: BLE001
        rc, err = None, io.StringIO()
        bad("guard() let %s escape instead of refusing (%s)" % (type(exc).__name__, exc))
    finally:
        _mb.assert_pristine = real_assert
    if rc == 2 and err.getvalue().startswith("::error::") and "RuntimeError" in err.getvalue():
        ok("guard() refuses, naming the type, when the check itself fails")
    elif rc is not None:
        bad("guard() returned %r / wrote %r when the check itself failed"
            % (rc, err.getvalue()[:120]))

# --- EVERY runner IS GUARDED, derived from the directory --------------------
#
# Not a hand-written list of seven: a new `*-mutations.py` that overwrites a
# tracked file without calling the guard is the whole defect coming back, and a
# restated list would not see it. Fails closed if the glob finds nothing.
runners = sorted((ROOT / "scripts" / "tests").glob("*-mutations.py"))
if not runners:
    bad("found no *-mutations.py at all -- refusing to report that they are guarded")
for runner in runners:
    try:
        src = runner.read_text(encoding="utf-8")
    except OSError as exc:
        bad("%s could not be read (%s)" % (runner.name, exc))
        continue
    call = src.find("mutation_baseline.guard(")
    write = src.find(".write_text(mutated")
    if call < 0:
        bad("%s never calls mutation_baseline.guard()" % runner.name)
    elif write < 0:
        bad("%s has no `.write_text(mutated` -- this check cannot locate its "
            "first write, so it cannot say the guard runs first" % runner.name)
    elif call > write:
        bad("%s calls the guard AFTER it starts writing" % runner.name)
    else:
        ok("%s calls mutation_baseline.guard() before its first write" % runner.name)

print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
