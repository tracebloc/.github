#!/usr/bin/env python3
"""Regression: the standards-sync mutation harness must be actor-independent.

The `mutation-standards-sync` shard went red i.f.f. the CI actor happened to BE
SYNC_REVIEWER (saqlainsyed007). The "reviewer reverts to GITHUB_ACTOR" mutation
swaps the sync's reviewer for os.environ["GITHUB_ACTOR"]; its catching assertion
is `SYNC_REVIEWER in reviewer_edit`, so when the triggering actor already equalled
SYNC_REVIEWER the substring still held, the mutation went UNCAUGHT, and the shard
reddened -- for that one person's .github PRs only, on files their PR never
touched (tracebloc/backend#3422).

`standards-sync-mutations.selftest_env()` pins GITHUB_ACTOR to a sentinel that
cannot be a login, making the verdict actor-independent. This runs the WHOLE
harness with GITHUB_ACTOR set to the exact login the catching assertion keys on
-- the precise poison condition of the bug -- and asserts it still reports
0 uncaught. It reddens the instant that pin is removed, which is the automated
form of the acceptance criterion on .github#440: previously the only evidence the
pin worked lived in a PR description, verified by hand.

The poison actor is read from the guard's own SYNC_REVIEWER rather than hardcoded,
so if that login ever changes the poison tracks it -- a literal would silently
stop colliding and the test would pass vacuously.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HARNESS = HERE / "standards-sync-mutations.py"
GUARD = ROOT / "scripts" / "standards-sync.py"
GUARD_REL = "scripts/standards-sync.py"

# The harness prints exactly this line once it has run every mutation. Its
# presence is what separates "the harness reached a verdict" from "it never got
# that far" -- the distinction this test hangs the environment-vs-condition split
# on. Groups: total, stale, malformed, uncaught.
VERDICT_RE = re.compile(
    r"(\d+) mutation\(s\): (\d+) stale, (\d+) malformed, (\d+) uncaught"
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _in_ci() -> bool:
    """True when running under CI (fresh, must-be-clean checkout)."""
    return bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


def _guard_is_dirty() -> bool:
    """True iff standards-sync.py has uncommitted changes vs HEAD.

    This is the exact state the harness's baseline guard refuses to run against,
    and the exact state a developer editing that file is in. We detect it up front
    so a local `make check` mid-edit SKIPs instead of misreporting the refusal as a
    missing pin. Only a tracked-file diff (git exit 1) counts as this benign case;
    anything else (untracked, no git, no HEAD) is left for the harness's own guard
    to surface as the environment failure it is.
    """
    try:
        rc = subprocess.run(
            ["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--", GUARD_REL],
        ).returncode
    except OSError:
        return False
    return rc == 1


def main() -> int:
    sync = _load("standards_sync", GUARD)
    poison = sync.SYNC_REVIEWER
    if not poison:
        sys.stderr.write("FAIL  standards-sync.SYNC_REVIEWER is empty; cannot poison.\n")
        return 1

    if _guard_is_dirty():
        # A dirty guard file cannot be mutation-tested (the harness refuses it). For a
        # developer editing standards-sync.py that is benign, so a LOCAL run skips.
        # In CI the checkout is fresh and must be clean: a dirty guard there means an
        # interrupted or broken run left it mutated, and skipping would pass `make
        # check` green having verified nothing -- the silent no-verify the pin must
        # never have. So CI fails loud instead of skipping.
        msg = (
            "%s has uncommitted changes, so the mutation harness cannot run against it"
            % GUARD_REL
        )
        if _in_ci():
            sys.stderr.write(
                "ERROR  %s. In CI the checkout must be clean for the pin to be "
                "verified -- restore the file. Nothing was asserted.\n" % msg
            )
            return 1
        print(
            "SKIP  %s (by design). Commit or stash it to exercise this regression "
            "locally; nothing asserted this run." % msg
        )
        return 0

    # Run the real harness exactly as CI's mutation shard does, but with the
    # environment a run triggered by SYNC_REVIEWER would carry. The pin must make
    # every mutation caught regardless.
    env = dict(os.environ, GITHUB_ACTOR=poison)
    proc = subprocess.run(
        [sys.executable, str(HARNESS)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    verdict = VERDICT_RE.search(proc.stdout)
    if verdict is None:
        # No verdict line => the harness never reached the mutation loop: a
        # baseline-guard refusal on an otherwise-clean tree, a crash, a failed
        # restore. That is an ENVIRONMENT failure and says nothing about the pin;
        # reporting it as "pin missing" would misdiagnose it (the exact conflation
        # the learned rule warns against).
        sys.stderr.write(
            "ERROR  the standards-sync mutation harness produced no verdict line, so it "
            "did not run to completion (exit %d).\n  This is an environment failure, not "
            "a GITHUB_ACTOR-pin regression -- diagnose the harness itself.\n"
            % proc.returncode
        )
        sys.stderr.write("---- harness stdout ----\n%s\n---- harness stderr ----\n%s\n"
                         % (proc.stdout, proc.stderr))
        return 1

    stale = int(verdict.group(2))
    malformed = int(verdict.group(3))
    uncaught = int(verdict.group(4))

    if uncaught == 0 and stale == 0 and malformed == 0 and proc.returncode == 0:
        print(
            "PASS  harness reports 0 uncaught with GITHUB_ACTOR=%s; the pin makes "
            "the verdict actor-independent" % poison
        )
        return 0

    if uncaught > 0:
        # The one outcome that IS the condition under test: a mutation the pin should
        # have kept catchable slipped through, so the pin is missing or defeated.
        sys.stderr.write(
            "FAIL  with GITHUB_ACTOR=%s the harness reported %d uncaught mutation(s).\n"
            "  The GITHUB_ACTOR pin in standards-sync-mutations.selftest_env() is missing "
            "or defeated, so a real login in the\n  environment leaks into the 'reviewer "
            "reverts to GITHUB_ACTOR' mutation and it goes uncaught (backend#3422).\n"
            % (poison, uncaught)
        )
        sys.stderr.write("---- harness stdout ----\n%s\n" % proc.stdout)
        return 1

    # 0 uncaught but stale/malformed rows (or a non-zero exit for any other reason):
    # anchors that no longer match, a mutation that would not compile, a restore that
    # failed. These are harness-integrity / environment failures, NOT a pin regression
    # -- blaming the pin for them is the conflation the learned rule warns against.
    sys.stderr.write(
        "ERROR  the harness did not run cleanly with GITHUB_ACTOR=%s: %d stale, %d "
        "malformed, exit %d, but 0 uncaught.\n  This is a harness/environment failure, "
        "not a GITHUB_ACTOR-pin regression -- fix the harness anchors or mutations.\n"
        % (poison, stale, malformed, proc.returncode)
    )
    sys.stderr.write("---- harness stdout ----\n%s\n" % proc.stdout)
    return 1


if __name__ == "__main__":
    sys.exit(main())
