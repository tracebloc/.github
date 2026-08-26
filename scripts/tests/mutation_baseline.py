#!/usr/bin/env python3
"""The baseline every mutation harness in this directory measures against
(backend#2441).

WHY THIS FILE EXISTS. All seven `*-mutations.py` runners work the same way: read
the tracked file, write a mutation over it, run the suite, restore it in a
`finally`. That `finally` covers a crash and an exception. It does not cover
SIGKILL, a runner timeout, or a second harness racing the first in the same
worktree -- and when one of those lands, the mutated text stays on disk. The
NEXT run then reads that text as its `pristine` baseline, measures every
mutation against a premise nobody typed, and writes the corruption back at the
end.

The tell is that such a run reports `0 uncaught`, which is byte-identical to
real coverage. A harness whose entire job is proving guards catch bugs, silently
proving nothing -- fail-open in the one direction that matters. It has already
destroyed a tracked file here once (`scripts/pipefail-early-close.awk`, the
symptom that produced the ticket).

WHAT THIS DOES. `guard()` refuses to start unless every file the harness is
about to mutate is identical to its committed content at HEAD. It does NOT
restore: a modified file may be somebody's work in progress, and quietly
overwriting that is a worse bug than the one being fixed. The operator is told
which file it is and what to do about it.

WHY DETECT RATHER THAN RELOCATE. The other shape -- copy the tree to a temp dir
and never touch the tracked file -- is cleaner in principle and is what
`release-train`'s runner does. It does not transplant here: two of the seven
targets are whole-tree gates that call `git ls-files` in the directory they are
pointed at, so a plain copy is not merely different, it REFUSES
(`pipefail-early-close: 'git ls-files' failed ... refusing to report clean`,
measured on a copy of this tree). Making that work needs a real clone or
worktree per run, which is a different and much larger change. Detecting a
corrupted baseline closes the fail-open direction now, and does not stand in the
way of relocating later.

CANNOT-TELL IS A REFUSAL, never a pass -- this repo's own rule 3. A missing
file, an unreadable one, a `git` that will not run, a repository with no HEAD, a
path git does not track, and any git exit status that is neither "same" nor
"differs" all refuse. Zero evidence of a difference is not evidence of sameness.

ONLY THE WRITING PATH IS GUARDED. `--dry` resolves anchors and writes nothing,
so it has no restore to lose and cannot propagate a corruption. It is also what
`make check` runs on every push, where refusing on any uncommitted edit to a
guarded script would block the pre-push tier for the one person most likely to
be editing it. A corrupted file still cannot hide from `--dry`: its anchors go
stale, and a stale anchor is a red run.
"""
import subprocess
import sys
from pathlib import Path


class DirtyBaseline(Exception):
    """The tree cannot be SHOWN to match HEAD, so no mutation may be measured."""


def _git(root, args):
    """Run git in `root` and return (rc, stdout).

    A git that cannot be run at all raises rather than returning a status: the
    caller must not be able to mistake "could not ask" for "asked, and it is
    clean".
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True,
        )
    except OSError as exc:
        raise DirtyBaseline(
            "`git %s` could not be run (%s), so this run cannot tell whether the "
            "files it is about to mutate still match HEAD. Refusing: a baseline "
            "that was never checked is not a clean one."
            % (" ".join(args), exc)
        )
    return proc.returncode, proc.stdout


def assert_pristine(root, paths):
    """Raise DirtyBaseline unless every path in `paths` matches HEAD.

    `paths` are the files the caller is about to overwrite. Every failure mode
    -- including every way of not being able to tell -- raises.
    """
    root = Path(root)
    rc, _ = _git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
    if rc != 0:
        raise DirtyBaseline(
            "`git rev-parse HEAD` failed in %s, so there is no committed content "
            "to compare the mutation targets against. Refusing to measure "
            "mutations against a baseline nothing vouches for." % root
        )

    for target in paths:
        path = Path(target)
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            raise DirtyBaseline(
                "%s is outside %s, so it cannot be compared with that "
                "repository's HEAD. Refusing." % (path, root)
            )

        try:
            path.read_bytes()
        except OSError as exc:
            raise DirtyBaseline(
                "%s could not be read (%s). A mutation target that cannot be read "
                "cannot be shown to be pristine, and cannot be restored after the "
                "run either. Refusing." % (rel, exc)
            )

        rc, _ = _git(root, ["ls-files", "--error-unmatch", "--", rel])
        if rc != 0:
            raise DirtyBaseline(
                "%s is not tracked by git, so there is nothing to compare it "
                "against and nothing to restore it from. Refusing." % rel
            )

        rc, _ = _git(root, ["diff", "--quiet", "HEAD", "--", rel])
        if rc == 0:
            continue
        if rc == 1:
            raise DirtyBaseline(
                "%s differs from its committed content at HEAD.\n"
                "  This harness measures every mutation against the TRACKED file, "
                "so a modified one silently becomes the baseline -- and the run "
                "then reports `0 uncaught` about a premise nobody typed. That is "
                "how a killed or racing run corrupts the next one (backend#2441).\n"
                "  Restore it with `git checkout -- %s`, or commit it if the "
                "change is deliberate, then re-run.\n"
                "  NOT restored automatically, on purpose: those bytes might be "
                "your work in progress." % (rel, rel)
            )
        raise DirtyBaseline(
            "`git diff --quiet HEAD -- %s` exited %d, which is neither \"matches\" "
            "(0) nor \"differs\" (1). The comparison could not be made, so this run "
            "cannot tell whether its baseline is pristine. Refusing." % (rel, rc)
        )


def guard(root, paths):
    """0 if every path matches HEAD; otherwise print the refusal and return 2.

    The two-line call site every harness uses. Returning a status rather than
    exiting keeps the decision to stop with the harness's own `main`.
    """
    try:
        assert_pristine(root, paths)
    except DirtyBaseline as exc:
        sys.stderr.write("::error::%s\n" % exc)
        return 2
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see below
        # An unexpected exception is the ultimate cannot-tell: the comparison did
        # not finish, so nothing vouches for the baseline. Letting it escape would
        # also be a refusal, but a noisier and less obvious one -- and the caller
        # is a harness whose next statement overwrites a tracked file. The type is
        # named so this stays debuggable rather than swallowed.
        sys.stderr.write(
            "::error::the baseline check itself failed with %s (%s), so this run "
            "cannot tell whether its mutation targets still match HEAD. Refusing.\n"
            % (type(exc).__name__, exc)
        )
        return 2
    return 0
