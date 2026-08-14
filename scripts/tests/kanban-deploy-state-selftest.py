#!/usr/bin/env python3
"""The deploy-state classification is read OUT of the workflow and exercised.

WHY THIS EXISTS (backend#1846)

`kanban-closure-router.yml` must never overwrite a deploy state with `Done`
(RFC-BACKEND-1405 D8). It decided that from a hand-maintained list of six column
names, duplicated in `kanban-reconcile.yml` -- and the list rotted: it carried
the pre-rename "Staging (human review)" while the board's column is
"Staging (agent review)", so a card hand-closed there lost its deploy state and
`kanban-archive.yml` hid it (.github#237).

The classification now comes from the board's own option ORDER: a deploy state is
any column at or after `On dev` and at or before `Prod`. That is what makes an
inserted column -- which is how "Staging (agent review)" arrived -- correct with
no edit.

WHY IT IS EXTRACTED RATHER THAN COPIED

A copy of the logic here would let the workflow drift while this file stays
green, which is the same defect class the classification itself had. So the
`col_index` function is pulled from the YAML and sourced. If someone renames or
reshapes it, this test stops finding it and fails loudly rather than testing a
stale duplicate.

Exit 0 when every case behaves as specified.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
WF = os.path.join(HERE, os.pardir, os.pardir,
                  ".github", "workflows", "kanban-closure-router.yml")

RESULTS: "list[tuple[bool, str, str]]" = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        {detail}")


def extract_col_index() -> str:
    """Pull `col_index()` out of the workflow's run: block, verbatim."""
    doc = yaml.safe_load(open(WF))
    runs = [s["run"] for j in doc["jobs"].values()
            for s in j.get("steps", []) if "run" in s]
    for body in runs:
        m = re.search(r"^\s*col_index\(\) \{.*?^\s*\}\s*$", body,
                      re.S | re.M)
        if m:
            # Strip the workflow's indentation so it parses standalone.
            block = m.group(0)
            indent = len(block) - len(block.lstrip())
            return "\n".join(ln[indent:] if ln[:indent].isspace() else ln
                             for ln in block.splitlines())
    sys.exit("could not find col_index() in the workflow — did it get renamed? "
             "This test refuses to fall back to a copy.")


COL_INDEX = extract_col_index()

# The live board's order, as the API returns it.
BOARD = ["Backlog", "North Stars", "Ready", "In progress", "Code review",
         "On dev", "Staging (agent review)", "FR on staging", "Ready for prod",
         "Prod", "Done", "Cancelled"]


def proj(names) -> str:
    return json.dumps({"data": {"organization": {"projectV2": {"fields": {
        "nodes": [{"name": "Status",
                   "options": [{"name": n} for n in names]}]}}}}})


def classify(current: str, names=None) -> str:
    """Run the EXTRACTED function plus the guard's own comparison."""
    names = BOARD if names is None else names
    script = f"""
set -euo pipefail
PROJ='{proj(names)}'
{COL_INDEX}
cur_i=$(col_index "{current}")
dev_i=$(col_index "On dev")
prod_i=$(col_index "Prod")
if [ "$dev_i" -lt 0 ] || [ "$prod_i" -lt 0 ]; then echo ANCHORS_MISSING; exit 0; fi
if [ "$cur_i" -lt 0 ]; then echo UNKNOWN; exit 0; fi
if [ "$cur_i" -ge "$dev_i" ] && [ "$cur_i" -le "$prod_i" ]; then
  echo DEPLOY_STATE
else
  echo FREE
fi
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    if out.returncode != 0:
        return f"ERROR: {out.stderr.strip()}"
    return out.stdout.strip()


# 1. Every deploy state, including the one the old list missed.
for col in ("On dev", "Staging (agent review)", "FR on staging",
            "Ready for prod", "Prod"):
    record(classify(col) == "DEPLOY_STATE",
           f"{col!r} is a deploy state", f"-> {classify(col)}")

# 2. Everything before On dev is free to be overwritten by Done.
for col in ("Backlog", "Ready", "In progress", "Code review"):
    record(classify(col) == "FREE",
           f"{col!r} is not a deploy state", f"-> {classify(col)}")

# 3. TERMINAL columns sit AFTER Prod in the order, so the `<= Prod` bound is
#    what stops them being treated as deploy states. Without it a card already
#    in Done would refuse to be set to Done -- harmless, but it would also make
#    Cancelled unreachable.
for col in ("Done", "Cancelled"):
    record(classify(col) == "FREE",
           f"{col!r} is terminal, not a deploy state", f"-> {classify(col)}")

# 4. THE CASE THE OLD LIST GOT WRONG. A column inserted between the anchors is
#    classified correctly with no edit here -- which is exactly how
#    "Staging (agent review)" arrived and why the list rotted.
inserted = BOARD[:7] + ["Staging (robot review)"] + BOARD[7:]
record(classify("Staging (robot review)", inserted) == "DEPLOY_STATE",
       "a NEWLY inserted column between the anchors is a deploy state",
       "no edit to the workflow required — this is the rot the list had")

# 5. UNKNOWN MUST NOT FALL OPEN.
record(classify("Some Column Nobody Declared") == "UNKNOWN",
       "a column the board does not report is UNKNOWN, not free",
       "the old `case` fell through and let Done erase the deploy state")

record(classify("") == "UNKNOWN",
       "an unreadable current column is UNKNOWN, not free", "empty CURRENT_COL")

# 6. A board missing an anchor cannot be classified at all.
record(classify("On dev", [n for n in BOARD if n != "Prod"]) == "ANCHORS_MISSING",
       "a board with no 'Prod' column refuses rather than guessing",
       "the guard exits 1 on this, which is the fail-closed half")

failed = [r for r in RESULTS if not r[0]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
