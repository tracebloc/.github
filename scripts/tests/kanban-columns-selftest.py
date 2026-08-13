#!/usr/bin/env python3
"""Selftest for kanban-columns-check.py — no network, no board.

A checker that cannot fail is worse than no checker: its green reads as
conformance. These drive the pure logic with a stubbed board so the failure
paths are exercised rather than assumed.
"""
from __future__ import annotations

import importlib.util
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
    """Drive main()'s comparison with both reads stubbed."""
    kcc.written_names = lambda: written
    kcc.board_options = lambda: options
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

print(f"\npass={passed} fail={failed}")
sys.exit(1 if failed else 0)
