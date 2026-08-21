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
    # `written_names` takes `options` now: it also collects CASE-ARM names from
    # WRITERS files, which is what put rank()'s twelve under the check.
    kcc.written_names = lambda options: written
    kcc.board_options = lambda: options
    kcc.cross_check = lambda found, options: []
    # STUBBED FOR THE SAME REASON AS cross_check, and worth saying so: it also reads
    # the real workflow files, and against a synthetic board it fires on every one of
    # them and short-circuits main() before the assertions here can run. It has its
    # own cases at the bottom of this file.
    kcc.unlisted_namers = lambda options, where=None: []
    # Same reason as the two above: reads real files, would short-circuit main().
    kcc.stale_exemptions = lambda options: []
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
# COMMENT LINES ARE PART OF THE BLOCK. The first version matched only CONSECUTIVE
# list items, so a comment between two entries truncated the block and the entries
# after it read as uncovered -- a false "uncovered=[...]" that took two rounds to
# recognise as a parser artefact rather than a missing path. A comment inside a YAML
# list is legitimate, so the pattern admits them.
_paths_block = re.search(
    r"\n\s*paths:\s*\n((?:\s*(?:-\s*'[^']*'|-\s*\"[^\"]*\"|#[^\n]*)\s*\n)+)",
    _wf_text)
check(_paths_block is not None, "the workflow still has a paths: block")
_listed = set(re.findall(r"-\s*[\"']([^\"']+)[\"']", _paths_block.group(1) if _paths_block else ""))
_uncovered = [w for w in kcc_fresh.WRITERS if ".github/workflows/" + w not in _listed]
check(_uncovered == [],
      "every WRITERS entry is in the workflow's paths: filter",
      "uncovered=" + repr(_uncovered))


# --- unlisted_namers: a curated WRITERS tuple, checked rather than trusted ----
# Two hand-removals from WRITERS on .github#295 were both wrong -- the router (six
# literals) and advance-deploy-env (thirteen, in `rank()`). This guard is why the
# tuple is derived against now, so it gets its own cases against a CONTROLLED
# workflows dir rather than the real one.
import tempfile as _tf  # noqa: E402

_BOARD = {"On dev", "Prod", "FR on staging"}
with _tf.TemporaryDirectory() as _d:
    _p = pathlib.Path(_d)
    (_p / "listed.yml").write_text('        STATUS="On dev"\n')
    (_p / "namer.yml").write_text('            "Prod")  echo 10 ;;\n')
    (_p / "only-a-comment.yml").write_text('        # a card at "Prod" is shipped\n')
    (_p / "innocent.yml").write_text('        run: echo hello\n')

    # WRITERS on the FRESH module too -- `run()` replaced the function on `kcc`
    # permanently, which is exactly how these cases first got the no-op stub and
    # reported a clean guard. Same reason the cross_check cases use kcc_fresh.
    _real_writers = kcc_fresh.WRITERS
    try:
        kcc_fresh.WRITERS = ("listed.yml",)
        _found = " ".join(kcc_fresh.unlisted_namers(_BOARD, where=_p))
        check("namer.yml" in _found,
              "a workflow naming a column while absent from WRITERS is a finding",
              _found)
        check("listed.yml" not in _found,
              "a file already in WRITERS is not double-reported", _found)
        # CODE LINES ONLY -- comments discuss column names constantly, including the
        # two comments explaining the removals this guard exists because of.
        check("only-a-comment.yml" not in _found,
              "a column named only in a COMMENT is not a finding", _found)
        check("innocent.yml" not in _found,
              "a workflow naming no column is not a finding", _found)
        # ... and it must be able to come back CLEAN, or it is a permanent red.
        kcc_fresh.WRITERS = ("listed.yml", "namer.yml")
        check(kcc_fresh.unlisted_namers(_BOARD, where=_p) == [],
              "listing the namer clears the finding", "still reported")
    finally:
        kcc_fresh.WRITERS = _real_writers


# --- case-arm names, and the stale-exemption check the docstring promised ----
# Restoring a file to WRITERS did NOT put its `case` arm names under the board check:
# `LITERAL` needs an `=` and a case arm has none, and WRITERS membership makes
# `unlisted_namers` skip the file. All twelve of advance-deploy-env's rank() names
# were invisible, three of them collected from nowhere at all (Bugbot, .github#295).
with _tf.TemporaryDirectory() as _d2:
    _p2 = pathlib.Path(_d2)
    # BOTH shapes, because a real WRITERS file has both -- and the staleness guard
    # inside written_names is keyed on the ASSIGNMENT pass, so a fixture with only
    # case arms would (correctly) trip it.
    (_p2 / "arms.yml").write_text(
        '        STATUS="Prod"\n'
        '            "Prod")  echo 10 ;;\n'
        '            "Backlog") echo 1 ;;\n'
        '        # "Cancelled" is only named in this comment\n')
    _rw, _rwf = kcc_fresh.WRITERS, kcc_fresh.WORKFLOWS
    try:
        kcc_fresh.WRITERS = ("arms.yml",)
        kcc_fresh.WORKFLOWS = _p2
        _got = kcc_fresh.written_names({"Prod", "Backlog", "Cancelled"})
        check("arms.yml" in _got.get("Prod", set()),
              "a case-arm name IS attributed to its file now", repr(_got))
        check("arms.yml" in _got.get("Backlog", set()),
              "a second case-arm name is collected too", repr(_got))
        check("Cancelled" not in _got,
              "a name only in a COMMENT is still not collected", repr(_got))

        # STALE EXEMPTIONS, which the docstring claimed the caller reported and
        # nothing did. Three ways an exemption expires; each asserted separately
        # because they are different facts.
        kcc_fresh.EXEMPT_NAMERS = {"gone.yml": "reason"}
        check(any("no longer exists" in r
                  for r in kcc_fresh.stale_exemptions({"Prod"})),
              "an exemption for a deleted file is stale",
              repr(kcc_fresh.stale_exemptions({"Prod"})))
        kcc_fresh.EXEMPT_NAMERS = {"arms.yml": "reason"}
        kcc_fresh.WRITERS = ("arms.yml",)
        check(any("now in WRITERS" in r
                  for r in kcc_fresh.stale_exemptions({"Prod"})),
              "an exemption for a file that joined WRITERS is stale",
              repr(kcc_fresh.stale_exemptions({"Prod"})))
        (_p2 / "quiet.yml").write_text("        run: echo hi\n")
        kcc_fresh.EXEMPT_NAMERS = {"quiet.yml": "reason"}
        kcc_fresh.WRITERS = ()
        check(any("names no board column" in r
                  for r in kcc_fresh.stale_exemptions({"Prod"})),
              "an exemption for a file naming no column is stale",
              repr(kcc_fresh.stale_exemptions({"Prod"})))
        # ... and a LIVE exemption is not reported, or the check is a permanent red.
        kcc_fresh.EXEMPT_NAMERS = {"arms.yml": "reason"}
        check(kcc_fresh.stale_exemptions({"Prod"}) == [],
              "a live exemption is left alone",
              repr(kcc_fresh.stale_exemptions({"Prod"})))
    finally:
        kcc_fresh.WRITERS, kcc_fresh.WORKFLOWS = _rw, _rwf

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
