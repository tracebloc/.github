#!/usr/bin/env python3
"""The bug-label promotion is read OUT of `bug-to-ready.yml` and exercised.

WHY THIS EXISTS (backend#2348)

The org rule "bugs get `work-type:bug` and go straight into `Ready`" had a 100%
miss rate because nothing implemented it. `bug-to-ready.yml` implements it, and
the failure mode of a workflow like that is not "it does not work" -- it is that
it works in the one case someone tried and quietly does the WRONG thing in the
others. The wrong thing here is destructive: dragging a card that has reached
`In progress`, `On dev` or `Prod` backwards into `Ready`.

So the cases that matter most in this file are the ones where it must do
NOTHING.

WHY IT IS EXTRACTED RATHER THAN COPIED

A copy of the decision here would let the workflow drift while this file stays
green -- the defect class backend#1729 catalogued, and the one .github#114/#115
hit twice in a day by re-implementing the rule inside the check. Both regions
under test are pulled out of the YAML by their `# selftest:` markers and run
verbatim by bash. If someone renames or reshapes them, this test stops finding
them and fails loudly rather than testing a stale duplicate.

WHAT IS EXTRACTED

  # selftest:candidate-*  Is this event a bug-labelled ISSUE at all? Driven
                          through its env inputs -- event name, the
                          is-a-pull-request flag, the raw labels JSON.
  # selftest:gate-*       Given the card's current column, is moving it to
                          `Ready` forward? Driven through a synthetic board via
                          a stubbed `col_index`, which is the same seam the
                          workflow's real one presents.

THE BOARD IS NOT RESTATED HERE. `kanban-deploy-state-selftest.py` already holds
the live board's option order, and a second hand-written copy of it in the same
directory is the drift shape this repo keeps finding. It is parsed out of that
file with `ast`, so a rename there fails this loudly instead of leaving two
lists to disagree.

Exit 0 when every case behaves as specified.
"""
from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(HERE, os.pardir, os.pardir, ".github", "workflows")
WF = os.path.join(WORKFLOWS, "bug-to-ready.yml")
SIBLING = os.path.join(HERE, "kanban-deploy-state-selftest.py")

RESULTS: "list[tuple[bool, str, str]]" = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        {detail}")


def dedent(block: str) -> str:
    indent = min((len(ln) - len(ln.lstrip()) for ln in block.splitlines()
                  if ln.strip()), default=0)
    return "\n".join(ln[indent:] if ln[:indent].isspace() else ln
                     for ln in block.splitlines())


def region(marker: str) -> str:
    """The workflow's own shell for `# selftest:<marker>-start|end`, verbatim."""
    doc = yaml.safe_load(open(WF))
    bodies = [s["run"] for j in doc["jobs"].values()
              for s in j.get("steps", []) if "run" in s]
    pattern = (rf"^[ \t]*# selftest:{marker}-start\b.*?"
               rf"^[ \t]*# selftest:{marker}-end\b[^\n]*$")
    for body in bodies:
        m = re.search(pattern, body, re.S | re.M)
        if m:
            return dedent(m.group(0))
    sys.exit(f"could not find the # selftest:{marker}-* region in "
             f"{os.path.basename(WF)} — was it renamed or its markers dropped? "
             "This test refuses to fall back to a copy of the logic.")


def board_from_sibling() -> "list[str]":
    """The live board's option ORDER, parsed out of the file that already holds it.

    Derived rather than restated (backend#1729 rule 1). A second literal list here
    would agree with itself forever while the board and its sibling moved on.
    """
    tree = ast.parse(open(SIBLING).read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "BOARD" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                return value
    sys.exit(f"no module-level `BOARD = [...]` literal in {SIBLING}. It moved or "
             "was renamed; this test will not fall back to its own copy of the "
             "board order.")


BOARD = board_from_sibling()
for anchor in ("Backlog", "Ready"):
    if anchor not in BOARD:
        sys.exit(f"the parsed board has no {anchor!r} column, so nothing below "
                 "would be testing the rule this workflow implements.")


def sh(script: str, env: "dict[str, str] | None" = None) -> "tuple[int, str]":
    full = dict(os.environ)
    full.update(env or {})
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env=full)
    return out.returncode, (out.stdout + out.stderr).strip()


# ===========================================================================
# 1. THE CANDIDATE GATE — which events are even eligible.
# ===========================================================================
CANDIDATE = region("candidate")


def candidate(event: str, is_pr: str, labels_json: str,
              bug_label: str = "work-type:bug") -> "tuple[int, str]":
    rc, out = sh("set -euo pipefail\n" + CANDIDATE + '\necho "VERDICT=$VERDICT"',
                 {"EVENT_NAME": event, "IS_PULL_REQUEST": is_pr,
                  "LABELS_JSON": labels_json, "BUG_LABEL": bug_label})
    verdict = ""
    for line in out.splitlines():
        if line.startswith("VERDICT="):
            verdict = line[len("VERDICT="):]
    return rc, (verdict if rc == 0 else f"rc={rc}|{out}")


def labels(*names: str) -> str:
    import json
    return json.dumps([{"name": n} for n in names])


# 1a. The case the ticket is about: a bug-labelled issue is a candidate.
rc, v = candidate("issues", "false", labels("work-type:bug"))
record(rc == 0 and v == "yes", "a bug-labelled issue is a candidate", f"-> {v}")

rc, v = candidate("issues", "false",
                  labels("priority", "work-type:bug", "from:customer"))
record(rc == 0 and v == "yes",
       "the label is found among several", f"-> {v}")

# 1b. NOT A CANDIDATE, and each of these is a way the workflow could have done
#     damage or noise instead.
for why, args in {
    "an unlabelled issue": ("issues", "false", labels()),
    "an issue labelled something else": ("issues", "false",
                                         labels("work-type:feature")),
}.items():
    rc, v = candidate(*args)
    record(rc == 0 and v == "no:label-absent", f"{why} is not a candidate",
           f"-> {v}")

# 1c. NEAR MISSES. The match must be the WHOLE label name. A substring or
#     case-insensitive match would promote cards nobody labelled as a defect,
#     and `work-type:bug` is a prefix of nothing today — which is exactly why
#     the inputs are written down here independently of the matcher rather than
#     generated from it (backend#1729 rule 9's corollary).
for near in ("work-type:bugfix", "Work-Type:Bug", "WORK-TYPE:BUG", "bug",
             "type:bug", " work-type:bug", "work-type:bug "):
    rc, v = candidate("issues", "false", labels(near))
    record(rc == 0 and v == "no:label-absent",
           f"{near!r} is not {'work-type:bug'!r}", f"-> {v}")

# 1d. PULL REQUESTS, NEVER. `issues:` events do not fire for PRs, so the caller
#     already excludes them — but a reusable can be called from any trigger, and
#     `issue_comment` fires for both. Per the board's model a PR lives in the
#     flow columns, so promoting one to `Ready` would be a demotion of real work.
rc, v = candidate("issues", "true", labels("work-type:bug"))
record(rc == 0 and v == "no:pull-request",
       "a PR carrying the bug label is refused, label or not", f"-> {v}")

rc, v = candidate("issue_comment", "false", labels("work-type:bug"))
record(rc == 0 and v == "no:not-an-issues-event",
       "a non-`issues` event is refused before anything else", f"-> {v}")

# 1e. CANNOT TELL IS A FINDING (backend#1729 rule 3). Each of these used to be
#     the easy fail-open: unparseable labels reading as "not a bug", and the
#     card staying in Backlog behind a green check — indistinguishable from the
#     bug this workflow removes.
for why, payload in {
    "labels that are not JSON": "not json at all",
    "an empty labels payload": "",
    "labels that are JSON but not a list": '{"name": "work-type:bug"}',
}.items():
    rc, out = candidate("issues", "false", payload)
    record(rc == 1 and "::error::" in out,
           f"{why} fails the run rather than reading as 'no'",
           out.splitlines()[0] if out else "<no output>")

rc, out = candidate("issues", "false", labels("work-type:bug"), bug_label="")
record(rc == 1 and "::error::" in out,
       "an empty `bug-label` input fails rather than matching everything",
       out.splitlines()[0] if out else "<no output>")

# ===========================================================================
# 2. THE MONOTONICITY GATE — the half that must refuse.
# ===========================================================================
GATE = region("gate")

# The marker is only reached when the gate falls through to the write, so its
# presence IS the promote verdict; `hold` exits 0 before it and the two refusals
# exit 1. Nothing here re-implements the decision.
WOULD_WRITE = "WOULD-WRITE"


def gate(current: str, board: "list[str]" = None) -> "tuple[int, str]":
    names = BOARD if board is None else board
    script = f"""
set -euo pipefail
ISSUE_NUMBER=1
FROM_STATUS="Backlog"
TO_STATUS="Ready"
BOARD_NAMES={shlex.quote(chr(10).join(names))}
# The same contract the workflow's own col_index presents: index on the board,
# or -1. Stubbed so the board can be reshaped; the DECISION below is the
# workflow's own text.
col_index() {{
  _i=0
  while IFS= read -r _n; do
    if [ "$_n" = "$1" ]; then echo "$_i"; return; fi
    _i=$((_i + 1))
  done <<BOARD_EOF
$BOARD_NAMES
BOARD_EOF
  echo -1
}}
CURRENT_COL={shlex.quote(current)}
{GATE}
echo "{WOULD_WRITE}"
"""
    return sh(script)


def verdict_of(rc: int, out: str) -> str:
    if rc != 0:
        if "does not sit after" in out:
            return "noboard"
        if "does not list as a Status option" in out:
            return "unknown"
        return f"error({rc}):{out[:120]}"
    return "promote" if WOULD_WRITE in out else "hold"


# 2a. THE ONE COLUMN THAT PROMOTES, plus the two no-Status spellings. A card
#     that reached the board but carries no Status is not "past Ready" — leaving
#     it unplaced would keep it invisible, which is the whole complaint.
for col in ("Backlog", "", "No status"):
    rc, out = gate(col)
    record(verdict_of(rc, out) == "promote",
           f"a card at {col or '<no Status>'!r} is promoted",
           f"-> {verdict_of(rc, out)}")

# 2b. THE WHOLE VOCABULARY, DERIVED FROM THE BOARD (backend#1729 rule 6).
#     Mutation coverage cannot see a vocabulary gap, so every column the board
#     declares is driven through the gate and exactly ONE of them may promote.
#     `Prod`, `Done` and `Cancelled` are the expensive ones: a card that shipped
#     or was cancelled must never be dragged back into the pull queue by someone
#     labelling it after the fact.
promoted = [c for c in BOARD if verdict_of(*gate(c)) == "promote"]
record(promoted == ["Backlog"],
       "of all %d board columns, exactly 'Backlog' promotes" % len(BOARD),
       f"promoting: {promoted or 'none'}; every other column holds")

for col in BOARD:
    if col == "Backlog":
        continue
    rc, out = gate(col)
    v = verdict_of(rc, out)
    record(v == "hold" and rc == 0,
           f"a card already at {col!r} is left alone",
           f"-> {v}; automation must never move a card backward")

# 2c. A COLUMN THE BOARD DOES NOT REPORT. Unreachable from the workflow's single
#     atomic read, and kept so the `case` has no fall-through. Driven here
#     because an arm nothing exercises is an arm nobody knows the sign of.
rc, out = gate("Some Column Nobody Declared")
record(verdict_of(rc, out) == "unknown" and rc == 1,
       "a column the board does not list refuses LOUDLY",
       "the monotonicity proof could not be made, so 'cannot tell' is a finding")

# 2d. A BOARD MISSING AN ANCHOR cannot place anything.
for gone in ("Backlog", "Ready"):
    trimmed = [c for c in BOARD if c != gone]
    assert gone not in trimmed and len(trimmed) == len(BOARD) - 1, trimmed
    rc, out = gate("Backlog" if gone == "Ready" else "Ready", trimmed)
    record(verdict_of(rc, out) == "noboard" and rc == 1,
           f"a board with no {gone!r} column refuses rather than guessing",
           f"-> {verdict_of(rc, out)}")

# 2e. BOTH ANCHORS PRESENT, ORDER INVERTED. This is the monotonicity assertion
#     itself, and it is the hole backend#1994 found in the sibling classifier:
#     existence was checked and ORDER was not. Drag the Status options so
#     `Ready` sorts before `Backlog` and this workflow's "promotion" becomes a
#     demotion — it must refuse to make it.
b_i, r_i = BOARD.index("Backlog"), BOARD.index("Ready")
inverted = list(BOARD)
inverted[b_i], inverted[r_i] = inverted[r_i], inverted[b_i]
# An inert input and a working guard produce the same green line, so assert the
# board really is inverted and otherwise UNCHANGED.
assert inverted.index("Ready") < inverted.index("Backlog"), inverted
assert sorted(inverted) == sorted(BOARD), inverted
rc, out = gate("Ready", inverted)
record(verdict_of(rc, out) == "noboard" and rc == 1,
       "a board whose Backlog/Ready order is INVERTED refuses",
       "moving 'forward' on that board is a demotion — existence is not enough")
# And the card sitting in Backlog on that same inverted board must ALSO refuse:
# the anchor check has to precede the current-column check, or the one case that
# would actually perform the demotion sails past it.
rc, out = gate("Backlog", inverted)
record(verdict_of(rc, out) == "noboard" and rc == 1,
       "the inverted board refuses even for a card at 'Backlog'",
       "the anchor check runs BEFORE the current-column check, or the single "
       "demoting case is the one that escapes")

failed = [r for r in RESULTS if not r[0]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
