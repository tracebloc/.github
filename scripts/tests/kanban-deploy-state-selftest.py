#!/usr/bin/env python3
"""The deploy-state classification is read OUT of the workflows and exercised.

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

A copy of the logic here would let the workflows drift while this file stays
green, which is the same defect class the classification itself had. So every
piece under test is pulled out of the YAML and run verbatim. If someone renames
or reshapes it, this test stops finding it and fails loudly rather than testing a
stale duplicate.

WHAT IS EXTRACTED, AND WHY IT IS THREE THINGS

Each workflow contributes three regions, because they share only two of them:

  col_index()              DIFFERS -- only the inputs differ (the router reads
                           `$PROJ`, reconcile a `$STATUS_ORDER` passed between
                           steps), so each is run with its own preamble.
  # selftest:classify-*    IDENTICAL, asserted byte-for-byte. The verdict must
                           not depend on which workflow is asking.
  # selftest:policy-*      DIFFERS BY DESIGN, and each file's own copy is run
                           (Bugbot, .github#252). The two lean OPPOSITE ways on
                           `unknown`: the router refuses to write Done, while
                           reconcile refuses to assert it. Substituting one for
                           the other would leave the real decision untested.

Exit 0 when every case behaves as specified.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(HERE, os.pardir, os.pardir, ".github", "workflows")

# BOTH COPIES, and the second one is why this list exists.
#
# The classification lives in two files because the two workflows genuinely
# cannot share code: kanban-closure-router.yml is a REUSABLE workflow that runs
# in the CALLER's checkout, so a script in this repo is not on disk for it, while
# kanban-reconcile.yml runs here and works from step outputs rather than `$PROJ`.
# Two copies is forced by the architecture -- so the guarantee has to be that
# they AGREE, which is what this file asserts.
ROUTER = os.path.join(WORKFLOWS, "kanban-closure-router.yml")
RECONCILE = os.path.join(WORKFLOWS, "kanban-reconcile.yml")

RESULTS: "list[tuple[bool, str, str]]" = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        {detail}")


def dedent(block: str) -> str:
    """Strip the workflow's indentation so the block parses standalone."""
    indent = min((len(ln) - len(ln.lstrip()) for ln in block.splitlines()
                  if ln.strip()), default=0)
    return "\n".join(ln[indent:] if ln[:indent].isspace() else ln
                     for ln in block.splitlines())


def runs(WF) -> "list[str]":
    doc = yaml.safe_load(open(WF))
    return [s["run"] for j in doc["jobs"].values()
            for s in j.get("steps", []) if "run" in s]


def extract(WF, pattern: str, what: str) -> str:
    for body in runs(WF):
        m = re.search(pattern, body, re.S | re.M)
        if m:
            return dedent(m.group(0))
    sys.exit(f"could not find {what} in {os.path.basename(WF)} — was it renamed "
             "or its markers dropped? This test refuses to fall back to a copy.")


def col_index(WF) -> str:
    return extract(WF, r"^\s*col_index\(\) \{.*?^\s*\}\s*$", "col_index()")


def region(WF, marker: str) -> str:
    return extract(WF, rf"^[ \t]*# selftest:{marker}-start\b.*?^[ \t]*# selftest:"
                       rf"{marker}-end\b[^\n]*$", f"the # selftest:{marker}-* region")


def proj(names) -> str:
    return json.dumps({"data": {"organization": {"projectV2": {"fields": {
        "nodes": [{"name": "Status",
                   "options": [{"name": n} for n in names]}]}}}}})


# Each copy takes its board differently. The PREAMBLE differs; the verdict must
# not.
COPIES = {
    "router": (ROUTER, lambda names: "PROJ=%s" % shlex.quote(proj(names))),
    "reconcile": (RECONCILE, lambda names: "STATUS_ORDER=$(printf %%s %s)"
                                           % shlex.quote("\t".join(names))),
}

# The live board's order, as the API returns it.
BOARD = ["Backlog", "North Stars", "Ready", "In progress", "Code review",
         "On dev", "Staging (agent review)", "FR on staging", "Ready for prod",
         "Prod", "Done", "Cancelled"]


def sh(script: str) -> "tuple[int, str]":
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return out.returncode, (out.stdout + out.stderr).strip()


# ---------------------------------------------------------------------------
# 0. The shared half really is shared. Byte-for-byte, or the "they agree"
#    guarantee below is only as good as the cases this file happens to try.
# ---------------------------------------------------------------------------
CLASSIFY = {c: region(WF, "classify") for c, (WF, _) in COPIES.items()}
record(CLASSIFY["router"] == CLASSIFY["reconcile"],
       "both workflows carry a byte-identical classify_column()",
       "the decision is shared; only col_index's inputs may differ")


def classify_one(copy: str, current: str, names=None) -> str:
    """Run ONE workflow's own classify_column, with its own index primitive."""
    WF, preamble = COPIES[copy]
    rc, out = sh(f"""
set -euo pipefail
{preamble(BOARD if names is None else names)}
{col_index(WF)}
{CLASSIFY[copy]}
classify_column {shlex.quote(current)}
""")
    return out if rc == 0 else f"ERROR({rc}): {out}"


def classify(current: str, names=None) -> str:
    """Both copies, and they must AGREE — that agreement is the guarantee."""
    verdicts = {c: classify_one(c, current, names) for c in COPIES}
    if len(set(verdicts.values())) != 1:
        return "DISAGREE: " + ", ".join(f"{c}={v}" for c, v in verdicts.items())
    return next(iter(verdicts.values()))


# 1. Every deploy state, including the one the old list missed.
for col in ("On dev", "Staging (agent review)", "FR on staging",
            "Ready for prod", "Prod"):
    record(classify(col) == "yes",
           f"{col!r} is a deploy state", f"-> {classify(col)}")

# 2. Everything before On dev is free to be overwritten by Done.
for col in ("Backlog", "Ready", "In progress", "Code review"):
    record(classify(col) == "no",
           f"{col!r} is not a deploy state", f"-> {classify(col)}")

# 3. TERMINAL columns sit AFTER Prod in the order, so the `<= Prod` bound is
#    what stops them being treated as deploy states. Without it a card already
#    in Done would refuse to be set to Done -- harmless, but it would also make
#    Cancelled unreachable.
for col in ("Done", "Cancelled"):
    record(classify(col) == "no",
           f"{col!r} is terminal, not a deploy state", f"-> {classify(col)}")

# 4. THE CASE THE OLD LIST GOT WRONG. A column inserted between the anchors is
#    classified correctly with no edit here -- which is exactly how
#    "Staging (agent review)" arrived and why the list rotted.
inserted = BOARD[:7] + ["Staging (robot review)"] + BOARD[7:]
record(classify("Staging (robot review)", inserted) == "yes",
       "a NEWLY inserted column between the anchors is a deploy state",
       "no edit to the workflow required — this is the rot the list had")

# 5. UNKNOWN MUST NOT FALL OPEN.
record(classify("Some Column Nobody Declared") == "unknown",
       "a column the board does not report is unknown, not free",
       "the old `case` fell through and let Done erase the deploy state")

# 6. NO COLUMN AT ALL is not an unplaceable column -- it is evidence that
#    nothing deployed. Classifying it `unknown` would make reconcile skip every
#    card that never reached the board, leaving closed strays unarchived
#    forever.
for col in ("", "No status"):
    record(classify(col) == "no",
           f"{col or '<empty>'!r} means nothing deployed, not unknown",
           "placeable — reconcile must still be able to terminalise it")

# 7. A board missing an anchor cannot be classified at all.
record(classify("On dev", [n for n in BOARD if n != "Prod"]) == "noboard",
       "a board with no 'Prod' column refuses rather than guessing",
       "each workflow then applies its own fail-closed direction — see below")

# ---------------------------------------------------------------------------
# 8. EACH WORKFLOW'S OWN POLICY, run verbatim (Bugbot, .github#252).
#
#    A shared verdict is only half the decision; what each file DOES with it is
#    the other half, and the two lean opposite ways. Running the router's
#    comparison for both files would leave reconcile's real `if` untested — a
#    regression in it would stay green while this file reported agreement.
#
#    classify_column is stubbed so the policy is driven through all four
#    verdicts, including ones no board can currently produce.
# ---------------------------------------------------------------------------
POLICY = {c: region(WF, "policy") for c, (WF, _) in COPIES.items()}

# verdict -> (expected exit code, expected marker in the output)
EXPECTED = {
    "router": {                         # decides whether to WRITE Done
        "noboard": (1, "::error::"),    #   refuses loudly, fails the run
        "unknown": (0, "::notice::"),   #   declines quietly, no write
        "yes": (0, "_protect=yes"),     #   a deploy state is protected
        "no": (0, "_protect=no"),       #   free to overwrite
    },
    "reconcile": {                      # decides whether to ASSERT Done
        "noboard": (0, "_skip=yes"),    #   cannot place it -> leave it alone
        "unknown": (0, "_skip=yes"),    #   opposite lean to the router's
        "yes": (0, "_skip=yes"),
        "no": (0, "_skip=no"),          #   the ONLY case it may terminalise
    },
}
for copy, table in EXPECTED.items():
    for verdict, (want_rc, want) in table.items():
        rc, out = sh(f"""
set -euo pipefail
NUMBER=1; CURRENT_COL='Some Column'; COL="$CURRENT_COL"
classify_column() {{ echo {verdict}; }}
{POLICY[copy]}
echo "_protect=${{_protect:-unset}} _skip=${{_skip:-unset}}"
""")
        ok = rc == want_rc and want in out
        record(ok, f"{copy} policy: {verdict} -> {want}",
               f"rc={rc} (want {want_rc}); {out.splitlines()[0] if out else '<no output>'}")

failed = [r for r in RESULTS if not r[0]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
