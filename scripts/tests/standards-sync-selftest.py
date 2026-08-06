#!/usr/bin/env python3
"""Offline self-test for scripts/standards-sync.py (tracebloc/backend#1602).

Same contract as caller-drift-selftest.py: no network, no token. The sync's
whole job is splicing a managed block into files other people own, so the
splice logic and its fail-closed paths are asserted here rather than trusted.

Exit 0 when every path behaves the way it is supposed to.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, os.pardir, "standards-sync.py")

_spec = importlib.util.spec_from_file_location("standards_sync", GUARD)
if _spec is None or _spec.loader is None:
    sys.exit(f"cannot import {GUARD}")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

RESULTS: "list[tuple[bool, str, str]]" = []
CANON = "# tracebloc engineering standards\n\n- rule one\n- rule two\n"


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        {detail}")


def expect_exit_2(name: str, fn) -> None:
    try:
        fn()
    except SystemExit as exc:
        record(exc.code == 2, name, f"exited {exc.code} (want 2)")
        return
    record(False, name, "returned instead of failing closed")


# ---------------------------------------------------------------- classify()
record(sync.classify(None, CANON) == sync.NO_FILE,
       "classify: absent file", "None -> NO_FILE")
record(sync.classify("# repo notes\n", CANON) == sync.MISSING_BLOCK,
       "classify: no markers", "plain file -> MISSING_BLOCK")

stamped = f"# repo notes\n\n{sync.render_block(CANON)}\n"
record(sync.classify(stamped, CANON) == sync.IN_SYNC,
       "classify: freshly stamped file is IN_SYNC", "render_block round-trips")
record(sync.classify(stamped, CANON + "- rule three\n") == sync.DRIFTED,
       "classify: canon moved on", "old block vs new canon -> DRIFTED")

for label, text in [
    ("begin without end", f"x\n{sync.BEGIN}\ny\n"),
    ("end without begin", f"x\n{sync.END}\ny\n"),
    ("end before begin", f"{sync.END}\nmiddle\n{sync.BEGIN}\n"),
    ("two begins", f"{sync.BEGIN}\n{sync.BEGIN}\na\n{sync.END}\n"),
]:
    record(sync.classify(text, CANON) == sync.MALFORMED,
           f"classify: {label}", "unpaired/duplicated markers -> MALFORMED")

# ------------------------------------------------------------ build_desired()
created = sync.build_desired(None, CANON, sync.NO_FILE)
record(sync.classify(created, CANON) == sync.IN_SYNC and created.startswith("# CLAUDE.md"),
       "build: NO_FILE creates stub + block", "stub header present, result IN_SYNC")

repo_owned = "# repo notes\n\nlocal gotcha: keep this line.\n"
appended = sync.build_desired(repo_owned, CANON, sync.MISSING_BLOCK)
record(sync.classify(appended, CANON) == sync.IN_SYNC
       and "local gotcha: keep this line." in appended
       and appended.index("local gotcha") < appended.index(sync.BEGIN),
       "build: MISSING_BLOCK appends below repo content",
       "repo-owned prose preserved, block appended, result IN_SYNC")

old = f"pre kept\n{sync.render_block('old canon body')}\npost kept\n"
replaced = sync.build_desired(old, CANON, sync.DRIFTED)
record(sync.classify(replaced, CANON) == sync.IN_SYNC
       and replaced.startswith("pre kept\n") and replaced.endswith("post kept\n")
       and "old canon body" not in replaced,
       "build: DRIFTED replaces only the block",
       "prefix/suffix byte-identical, old inner gone, result IN_SYNC")

resynced = sync.build_desired(replaced, CANON, sync.classify(replaced, CANON)) \
    if sync.classify(replaced, CANON) != sync.IN_SYNC else replaced
record(resynced == replaced, "build: idempotent",
       "re-running the sync on an IN_SYNC file changes nothing")

try:
    sync.build_desired(f"{sync.BEGIN}\n{sync.BEGIN}\n{sync.END}\n", CANON, sync.MALFORMED)
    record(False, "build: MALFORMED is never spliced", "splice happened")
except AssertionError:
    record(True, "build: MALFORMED is never spliced", "AssertionError as designed")

# --------------------------------------------------------------- load_canon()
with tempfile.TemporaryDirectory() as tmp:
    empty = os.path.join(tmp, "empty.md")
    open(empty, "w", encoding="utf-8").close()
    expect_exit_2("canon: empty file fails closed", lambda: sync.load_canon(empty))

    nested = os.path.join(tmp, "nested.md")
    with open(nested, "w", encoding="utf-8") as handle:
        handle.write(f"rules\n{sync.BEGIN}\n")
    expect_exit_2("canon: marker inside canon fails closed", lambda: sync.load_canon(nested))

    expect_exit_2("canon: unreadable path fails closed",
                  lambda: sync.load_canon(os.path.join(tmp, "missing.md")))

# ------------------------------------------------------------- load_targets()
with tempfile.TemporaryDirectory() as tmp:
    no_repos = os.path.join(tmp, "inv.yml")
    with open(no_repos, "w", encoding="utf-8") as handle:
        handle.write("org: tracebloc\n")
    expect_exit_2("inventory: missing repos key fails closed",
                  lambda: sync.load_targets(no_repos))

    stale_exempt = os.path.join(tmp, "inv2.yml")
    with open(stale_exempt, "w", encoding="utf-8") as handle:
        handle.write("org: tracebloc\nrepos:\n  backend: {}\n")
    # EXEMPT names devex-bootstrap; an inventory without it must fail, not skip.
    expect_exit_2("inventory: exemption naming an unknown repo fails closed",
                  lambda: sync.load_targets(stale_exempt))

    good = os.path.join(tmp, "inv3.yml")
    with open(good, "w", encoding="utf-8") as handle:
        handle.write("org: tracebloc\nrepos:\n  backend: {}\n  devex-bootstrap: {}\n")
    org, targets = sync.load_targets(good)
    record(org == "tracebloc" and targets == ["backend"],
           "inventory: exempt repo excluded from targets",
           f"targets={targets} (devex-bootstrap exempt with written reason)")

# ----------------------------------------------------------- crash semantics
# An operational crash must exit 2 ("could not evaluate"), never 1 — the
# workflow reads 1 as confirmed drift and would report a crash as a drift
# finding. Run the guard as a subprocess with `gh` stripped from PATH: the
# first repo read raises FileNotFoundError, and the entry point must map it.
with tempfile.TemporaryDirectory() as tmp:
    canon_path = os.path.join(tmp, "canon.md")
    with open(canon_path, "w", encoding="utf-8") as handle:
        handle.write(CANON)
    inv_path = os.path.join(tmp, "inv.yml")
    with open(inv_path, "w", encoding="utf-8") as handle:
        handle.write("org: tracebloc\nrepos:\n  backend: {}\n  devex-bootstrap: {}\n")
    empty_bin = os.path.join(tmp, "bin")
    os.makedirs(empty_bin)
    env = dict(os.environ, PATH=empty_bin)  # no gh anywhere on PATH
    env.pop("GITHUB_STEP_SUMMARY", None)
    proc = subprocess.run(
        [sys.executable, GUARD, "--canonical", canon_path,
         "--inventory", inv_path, "--repo", "backend"],
        capture_output=True, text=True, env=env, check=False,
    )
    record(proc.returncode == 2, "crash: gh unavailable exits 2, not 1",
           f"exited {proc.returncode} (want 2 — a crash must never read as drift)")

# ---------------------------------------------------------------------- tally
failed = [name for ok, name, _ in RESULTS if not ok]
print(f"\n{len(RESULTS)} checks, {len(failed)} failed.")
if failed:
    for name in failed:
        print(f"  FAIL: {name}")
    sys.exit(1)
