#!/usr/bin/env python3
"""Selftest for kanban-columns-check.py — no network, no board.

A checker that cannot fail is worse than no checker: its green reads as
conformance. These drive the pure logic with a stubbed board so the failure
paths are exercised rather than assumed.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import io
import contextlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("kcc", HERE.parent / "kanban-columns-check.py")
kcc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kcc)

passed = failed = 0


def check(ok: bool, name: str, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}\n        {detail}")


def run(written, options):
    """Drive main()'s comparison with both reads stubbed.

    `cross_check` is stubbed out too: it reads the real workflow files, so
    against a synthetic `written` dict it correctly reports staleness and would
    mask the missing-name assertions these cases exist for. It has its own
    tests at the bottom of this file.
    """
    kcc.written_names = lambda: written
    kcc.board_options = lambda: options
    kcc.cross_check = lambda found, options: []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = kcc.main()
    return code, buf.getvalue()


BOARD = {"On dev", "FR on staging", "Prod", "Done", "Cancelled", "Staging (agent review)"}

code, out = run({"FR on staging": {"a.yml"}, "Prod": {"b.yml"}}, BOARD)
check(code == 0, "all written names present -> exit 0", f"code={code}")

code, out = run({"Staging (human review)": {"advance-deploy-env.yml"}}, BOARD)
check(code == 1, "a written name absent from the board -> exit 1", f"code={code}")
check("advance-deploy-env.yml" in out,
      "the failure names the file that writes it", out[:200])
check("Staging (human review)" in out, "the failure names the offending value", out[:200])

# The pre-#245 regression exactly: one good name and one stale one.
code, _ = run({"FR on staging": {"x.yml"}, "Staging (human review)": {"y.yml"}}, BOARD)
check(code == 1, "one stale name among good ones still fails", f"code={code}")

# Case matters: the board lookup is exact, so a near-miss must not pass.
code, _ = run({"fr on staging": {"x.yml"}}, BOARD)
check(code == 1, "case-mismatched name does not silently pass", f"code={code}")

# --- the cross-check: an extractor that under-collects must not report clean ---
# This is the defect the check itself shipped with (.github#243): WRITERS named
# two files and one idiom, three column names were invisible, and it passed.
kcc.cross_check.__doc__  # touch, so a rename of the function fails loudly here

# run() replaced cross_check with a stub; restore the real one for its own tests.
importlib_spec = importlib.util.spec_from_file_location("kcc_fresh", HERE.parent / "kanban-columns-check.py")
kcc_fresh = importlib.util.module_from_spec(importlib_spec)
importlib_spec.loader.exec_module(kcc_fresh)
kcc.cross_check = kcc_fresh.cross_check

real_writers = kcc_fresh.WRITERS
try:
    # A writer whose only Status name is written by an idiom the regex misses.
    import os
    import tempfile
    tmp = tempfile.mkdtemp()
    stale = os.path.join(tmp, "stale-writer.yml")
    with open(stale, "w", encoding="utf-8") as fh:
        fh.write('        run: |\n          UNMATCHED_VAR="Ready for prod"\n')
    kcc_fresh.WORKFLOWS = __import__("pathlib").Path(tmp)
    kcc_fresh.WRITERS = ("stale-writer.yml",)
    stale_rows = kcc_fresh.cross_check({}, BOARD | {"Ready for prod"})
    check(any("Ready for prod" in r for r in stale_rows),
          "an assigned board name no idiom matched is reported as a stale idiom list",
          f"rows={stale_rows}")

    # A mention that is NOT an assignment must not be flagged.
    prose = os.path.join(tmp, "prose-writer.yml")
    with open(prose, "w", encoding="utf-8") as fh:
        fh.write('          # advances the card to Ready for prod eventually\n')
    kcc_fresh.WRITERS = ("prose-writer.yml",)
    check(kcc_fresh.cross_check({}, BOARD | {"Ready for prod"}) == [],
          "a comment mentioning a column is not mistaken for a write")
finally:
    kcc_fresh.WRITERS = real_writers
    kcc_fresh.WORKFLOWS = HERE.parent.parent / ".github" / "workflows"

# --- the PR trigger must cover every writer --------------------------------
# Broadening WRITERS without broadening the workflow's `paths:` filter means a PR
# touching only a new writer never runs this check at all, and a bad column write
# merges until the next cron (Bugbot, #248). That is the same shape as the check
# itself shipping with an incomplete WRITERS list: the guard is present, looks
# green, and is not watching the thing it names.
#
# THE MAPPING FILE IS AN INPUT TOO (Bugbot, .github#295). The check imports
# `branch_status_map.py`, so a PR touching only that file changes what this check
# would say -- and it was not in `paths:`, so the check never ran on it. WRITERS does
# not name it (it is not a workflow), which is exactly why it needed its own
# assertion rather than being covered by the WRITERS-vs-paths one.
wf = (pathlib.Path(__file__).resolve().parent.parent.parent
      / ".github" / "workflows" / "kanban-columns.yml").read_text()
check("the imported mapping file is in the workflow's paths: filter",
      '"scripts/branch_status_map.py"' in wf,
      "a mapping-only PR would skip the board-name check")

# Derived from WRITERS rather than eyeballed, so the two cannot drift apart.
_wf_text = (HERE.parent.parent / ".github" / "workflows" / "kanban-columns.yml").read_text()
_paths_block = re.search(r"\n\s*paths:\s*\n((?:\s*-\s*'[^']*'|\s*-\s*\"[^\"]*\"\s*\n)+)", _wf_text)
check(_paths_block is not None, "the workflow still has a paths: block")
_listed = set(re.findall(r"-\s*[\"']([^\"']+)[\"']", _paths_block.group(1) if _paths_block else ""))
_uncovered = [w for w in kcc_fresh.WRITERS if ".github/workflows/" + w not in _listed]
check(_uncovered == [],
      "every WRITERS entry is in the workflow's paths: filter",
      "uncovered=" + repr(_uncovered))

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
