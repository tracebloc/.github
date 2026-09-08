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
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HARNESS = HERE / "standards-sync-mutations.py"
GUARD = ROOT / "scripts" / "standards-sync.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    sync = _load("standards_sync", GUARD)
    poison = sync.SYNC_REVIEWER
    if not poison:
        sys.stderr.write("FAIL  standards-sync.SYNC_REVIEWER is empty; cannot poison.\n")
        return 1

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

    if proc.returncode == 0 and "0 uncaught" in proc.stdout:
        print(
            "PASS  harness reports 0 uncaught with GITHUB_ACTOR=%s; the pin makes "
            "the verdict actor-independent" % poison
        )
        return 0

    sys.stderr.write(
        "FAIL  harness was NOT actor-independent: with GITHUB_ACTOR=%s it exited %d.\n"
        "  The GITHUB_ACTOR pin in standards-sync-mutations.selftest_env() is missing\n"
        "  or defeated, so a real login in the environment leaks into the 'reviewer\n"
        "  reverts to GITHUB_ACTOR' mutation and it goes uncaught (backend#3422).\n"
        % (poison, proc.returncode)
    )
    sys.stderr.write("---- harness stdout ----\n%s\n---- harness stderr ----\n%s\n"
                     % (proc.stdout, proc.stderr))
    return 1


if __name__ == "__main__":
    sys.exit(main())
