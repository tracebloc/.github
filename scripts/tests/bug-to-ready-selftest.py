#!/usr/bin/env python3
"""The bug-label promotion is read OUT of the workflow and exercised.

WHY THIS EXISTS (tracebloc/backend#2348)

The org rule -- "bugs get `work-type:bug` and go straight into `Ready` (defects
skip refinement)" -- was documented in `org-standards.md` and implemented
nowhere. `customer-priority-bump.yml`'s `bug-to-ready` job implements it, and
this asserts the two things that job can get catastrophically wrong:

  * it moves a card that should not move (un-shipping work on the board), or
  * it reports success without having moved anything (the defect it fixes).

WHY IT IS EXTRACTED RATHER THAN COPIED

`customer-priority-bump.yml` is a REUSABLE workflow: it runs in the CALLER's
checkout, so no script in this repo is on disk for it and the decision has to be
inline shell. A copy of that shell in here would let the workflow drift while
this file stayed green -- the same defect class the rule itself had. So every
piece under test is pulled out of the YAML by its `# selftest:` markers and run
verbatim (CLAUDE.md rule 9). If someone renames or reshapes a region, this test
stops finding it and fails loudly rather than testing a stale duplicate.

WHERE THE VOCABULARY COMES FROM (CLAUDE.md rules 1 and 6)

A monotonicity check that tries two columns is vacuous, and a hand-written list
of the other ten agrees with itself. So the board's Status vocabulary is DERIVED
from `advance-deploy-env.yml`'s `rank()` -- the org's declared pipeline order,
and the same file this job's monotonicity was modelled on -- and cross-checked
against `kanban-deploy-state-selftest.py`'s independently written BOARD. Two
derivations that disagree is a finding, not a tie to break here.

The LABEL and the two COLUMN NAMES come from `org-standards.md`, which is the
canon the rule is written in. Rename the label there and this reddens, which is
the point: the workflow's default and the written rule cannot drift apart.

Exit 0 when every case behaves as specified.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir, os.pardir)
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")

BUG_WF = os.path.join(WORKFLOWS, "customer-priority-bump.yml")
ROUTER = os.path.join(WORKFLOWS, "kanban-closure-router.yml")
ADVANCE = os.path.join(WORKFLOWS, "advance-deploy-env.yml")
SIBLING_SUITE = os.path.join(HERE, "kanban-deploy-state-selftest.py")
CANON = os.path.join(ROOT, "org-standards.md")

JOB = "bug-to-ready"

RESULTS: "list[tuple[bool, str, str]]" = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}\n        {detail}")


def dedent(block: str) -> str:
    """Strip the workflow's indentation so the block parses standalone."""
    indent = min((len(ln) - len(ln.lstrip()) for ln in block.splitlines()
                  if ln.strip()), default=0)
    return "\n".join(ln[indent:] if ln[:indent].isspace() else ln
                     for ln in block.splitlines())


def doc(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def runs(path, job=None) -> "list[str]":
    d = doc(path)
    jobs = d["jobs"] if job is None else {job: d["jobs"][job]}
    return [s["run"] for j in jobs.values()
            for s in j.get("steps", []) if "run" in s]


def extract(path, pattern: str, what: str, job=None) -> str:
    for body in runs(path, job):
        m = re.search(pattern, body, re.S | re.M)
        if m:
            return dedent(m.group(0))
    # FAIL CLOSED. A missing region is "cannot tell", never "nothing to check":
    # falling back to a copy is how a suite comes to prove a regex nothing uses.
    sys.exit(f"could not find {what} in {os.path.basename(path)} - was it renamed "
             "or its markers dropped? This test refuses to fall back to a copy.")


def func(path, name: str, job=None) -> str:
    return extract(path, rf"^\s*{name}\(\) \{{.*?^\s*\}}\s*$", f"{name}()", job)


def region(path, marker: str, job=None) -> str:
    return extract(path, rf"^[ \t]*# selftest:{marker}-start\b.*?^[ \t]*# selftest:"
                         rf"{marker}-end\b[^\n]*$", f"the # selftest:{marker}-* region",
                   job)


def sh(script: str) -> "tuple[int, str]":
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return out.returncode, (out.stdout + out.stderr).strip()


# ---------------------------------------------------------------------------
# 0. THE VOCABULARY, DERIVED TWICE.
# ---------------------------------------------------------------------------
# `rank()` in advance-deploy-env.yml is the org's declared pipeline order and the
# construct this job's monotonicity was modelled on. Parsing it gives both the
# names and their order; a hand-written list here would be a third copy.
RANK_ARM = re.compile(r'^\s*"([^"]+)"\)\s*echo\s+(\d+)\s*;;', re.M)
_rank_src = func(ADVANCE, "rank")
_arms = [(name, int(n)) for name, n in RANK_ARM.findall(_rank_src)]
if len(_arms) < 5:
    sys.exit(f"parsed only {len(_arms)} rank() arms out of advance-deploy-env.yml - "
             "the pattern is stale, and a vocabulary of 0 would make every case "
             "below pass vacuously")
# Sorted by declared rank, ties (Done/Cancelled share a rank) in file order.
BOARD = [name for name, _ in sorted(_arms, key=lambda kv: (kv[1], _arms.index(kv)))]

# The second, independent declaration: the sibling suite's BOARD literal, written
# by hand from a live read of project #2. Two derivations that disagree means one
# of them is stale, and this file is not the place to pick a winner.
with open(SIBLING_SUITE, encoding="utf-8") as fh:
    _m = re.search(r"^BOARD = (\[[^\]]*\])", fh.read(), re.M | re.S)
if _m is None:
    sys.exit("could not find BOARD in kanban-deploy-state-selftest.py - the "
             "cross-check cannot be made, so this suite refuses to report on a "
             "single unchecked derivation")
SIBLING_BOARD = ast.literal_eval(_m.group(1))
record(sorted(BOARD) == sorted(SIBLING_BOARD),
       "the Status vocabulary derived from rank() agrees with the sibling suite's board",
       f"{len(BOARD)} columns: {', '.join(BOARD)}")
record(BOARD == SIBLING_BOARD,
       "and in the same ORDER, which is what the monotonic gate is derived from",
       f"rank(): {BOARD}\n        sibling: {SIBLING_BOARD}")

# ---------------------------------------------------------------------------
# 1. THE RULE'S OWN WORDS decide the label and the two anchors.
# ---------------------------------------------------------------------------
with open(CANON, encoding="utf-8") as fh:
    CANON_TEXT = fh.read()


def from_canon(pattern: str, what: str) -> str:
    m = re.search(pattern, CANON_TEXT)
    if m is None:
        sys.exit(f"could not read {what} out of org-standards.md (pattern "
                 f"{pattern!r}). The workflow's value cannot be checked against "
                 "the written rule, so this suite fails rather than assuming.")
    return m.group(1)


CANON_LABEL = from_canon(r"label them `([^`]+)`", "the bug label")
CANON_SOURCE = from_canon(r"New tickets start in `([^`]+)`", "the starting column")
CANON_TARGET = from_canon(r"straight into `([^`]+)`", "the target column")

_bug_job = doc(BUG_WF)["jobs"][JOB]
_step = [s for s in _bug_job["steps"] if "run" in s][0]
_env = _step.get("env") or {}
_inputs = doc(BUG_WF)[True]["workflow_call"]["inputs"]  # `on:` parses as True

record(_inputs["bug-label"]["default"] == CANON_LABEL,
       "the bug-label default is the label org-standards.md names",
       f"workflow: {_inputs['bug-label']['default']!r}; canon: {CANON_LABEL!r}")
record(_env.get("SOURCE_COLUMN") == CANON_SOURCE,
       "SOURCE_COLUMN is the column org-standards.md says new tickets start in",
       f"workflow: {_env.get('SOURCE_COLUMN')!r}; canon: {CANON_SOURCE!r}")
record(_env.get("TARGET_COLUMN") == CANON_TARGET,
       "TARGET_COLUMN is the column org-standards.md sends defects to",
       f"workflow: {_env.get('TARGET_COLUMN')!r}; canon: {CANON_TARGET!r}")
record(CANON_SOURCE in BOARD and CANON_TARGET in BOARD
       and BOARD.index(CANON_SOURCE) < BOARD.index(CANON_TARGET),
       "the canon's two columns exist in the declared order, source before target",
       f"{CANON_SOURCE} @{BOARD.index(CANON_SOURCE) if CANON_SOURCE in BOARD else '?'} "
       f"-> {CANON_TARGET} @{BOARD.index(CANON_TARGET) if CANON_TARGET in BOARD else '?'}")

# ---------------------------------------------------------------------------
# 2. THE JOB `if:` IS A COST GATE, AND MUST BE EXACT.
# ---------------------------------------------------------------------------
# The workflow says the `if:` can only ever be STRICTER than `label_gate`. A
# `contains()` there would make it LOOSER -- `work-type:bugfix` would mint a
# token and reach a gate that then refuses it -- and a substring match is the
# classic way this shape goes wrong, so it is asserted rather than trusted.
_if = str(_bug_job.get("if", ""))
record("contains(" not in _if and "inputs.bug-label" in _if
       and "github.event.label.name == inputs.bug-label" in _if,
       "the job `if:` is exact equality against inputs.bug-label",
       f"if: {_if}")
record("github.event_name == 'issues'" in _if,
       "the job `if:` also pins the event to `issues`",
       "a pull_request payload must not reach the promotion at all")

# The mint must be scoped (backend#2157): this job is new, so there is no
# pre-existing full grant to inherit and no reason to take one.
_mint = [s for s in _bug_job["steps"]
         if str(s.get("uses", "")).startswith("actions/create-github-app-token")]
record(len(_mint) == 1 and any(k.startswith("permission-")
                               for k in (_mint[0].get("with") or {})),
       "the token mint names explicit permission-* scopes",
       f"with: {sorted((_mint[0].get('with') or {})) if _mint else '<no mint step>'}")

# ---------------------------------------------------------------------------
# 3. THE INDEX PRIMITIVE IS SHARED WITH THE ROUTER, byte for byte.
# ---------------------------------------------------------------------------
# Both read the same `$PROJ` response shape, so a divergence between them is a
# defect rather than a difference. If one ever legitimately has to differ, this
# assertion is where that gets argued.
COL_INDEX = func(BUG_WF, "col_index", JOB)
record(COL_INDEX == func(ROUTER, "col_index"),
       "col_index() is byte-identical to kanban-closure-router.yml's",
       "same $PROJ shape, same primitive -- one definition, two callers")

MONOTONIC = region(BUG_WF, "monotonic", JOB)
POLICY = region(BUG_WF, "policy", JOB)
LABEL_GATE = region(BUG_WF, "label-gate", JOB)


def proj(names) -> str:
    return json.dumps({"data": {"organization": {"projectV2": {"fields": {
        "nodes": [{"name": "Status",
                   "options": [{"name": n} for n in names]}]}}}}})


def decide(current: str, archived: str = "false", names=None,
           source=None, target=None) -> str:
    """Run the workflow's own promote_decision against a synthetic board."""
    rc, out = sh(f"""
set -euo pipefail
PROJ={shlex.quote(proj(BOARD if names is None else names))}
SOURCE_COLUMN={shlex.quote(source or CANON_SOURCE)}
TARGET_COLUMN={shlex.quote(target or CANON_TARGET)}
{COL_INDEX}
{MONOTONIC}
promote_decision {shlex.quote(current)} {shlex.quote(archived)}
""")
    return out if rc == 0 else f"ERROR({rc}): {out}"


# ---------------------------------------------------------------------------
# 4. EVERY COLUMN IN THE VOCABULARY, not the two that are convenient.
# ---------------------------------------------------------------------------
# Mutation coverage cannot see a vocabulary gap (CLAUDE.md rule 6): a gate that
# promotes from `North Stars` too passes every two-column test ever written.
_promoting = [c for c in BOARD if decide(c) == "promote"]
record(_promoting == [CANON_SOURCE],
       f"of all {len(BOARD)} declared columns, exactly {CANON_SOURCE!r} promotes",
       f"promoting: {_promoting}; every other column must hold")
for col in BOARD:
    want = "promote" if col == CANON_SOURCE else "hold"
    got = decide(col)
    record(got == want, f"a card at {col!r} -> {want}", f"-> {got}")

# A card on the board with no Status is not "past Ready" -- leaving it unplaced
# is what keeps a defect invisible, which is the whole complaint.
for col in ("", "No status"):
    got = decide(col)
    record(got == "promote", f"{col or '<no Status>'!r} is promoted, not left unplaced",
           f"-> {got}")

# ARCHIVED is out of the flow: the archiver only touches terminal columns, and an
# archived item's field write errors anyway.
record(decide(CANON_SOURCE, archived="true") == "hold",
       "an ARCHIVED card holds even when it sits in the source column",
       f"-> {decide(CANON_SOURCE, archived='true')}")

# UNKNOWN MUST NOT FALL OPEN. Nothing can be said about "forward" from a position
# the board does not report.
got = decide("Some Column Nobody Declared")
record(got == "unknown", "a column the board does not report is unknown, not promotable",
       f"-> {got}")

# ---------------------------------------------------------------------------
# 5. THE ANCHORS: missing, and INVERTED.
# ---------------------------------------------------------------------------
for missing in (CANON_SOURCE, CANON_TARGET):
    board = [n for n in BOARD if n != missing]
    got = decide(CANON_SOURCE, names=board)
    record(got == "noboard", f"a board with no {missing!r} column refuses rather than guessing",
           f"-> {got}")

# INVERTED, which is one drag of the Status options away. With `Ready` before
# `Backlog` this job's "promotion" is a demotion, so it must refuse -- and it must
# refuse for the card AT the source column, the one case that would actually
# perform the demotion if the anchor check ran second.
s_i, t_i = BOARD.index(CANON_SOURCE), BOARD.index(CANON_TARGET)
inverted = list(BOARD)
inverted[s_i], inverted[t_i] = inverted[t_i], inverted[s_i]
# An inert input and a working guard produce the same green line, so assert the
# board really is inverted and otherwise UNCHANGED.
assert inverted.index(CANON_TARGET) < inverted.index(CANON_SOURCE), inverted
assert sorted(inverted) == sorted(BOARD), inverted
for col in (CANON_SOURCE, CANON_TARGET, "In progress", ""):
    got = decide(col, names=inverted)
    record(got == "noboard",
           f"{col or '<no Status>'!r} on a board whose anchors are INVERTED refuses",
           f"-> {got}; an unsatisfiable order must fail closed, and that includes "
           "the unplaced card the shortcut would otherwise let through")

# ---------------------------------------------------------------------------
# 6. THE POLICY, run verbatim, driven through every verdict.
# ---------------------------------------------------------------------------
# What the job DOES with a verdict is the other half of the decision, and the
# fail-closed DIRECTION is the part this job chose differently from its siblings:
# `unknown` and `noboard` are RED here, where the router exits 0. A cron gets
# another go next week; a `labeled` event fires once.
#
# `_bogus` is not a verdict any board can produce -- it drives the `*)` arm, which
# exists so the `case` has no fall-through to the write.
POLICY_EXPECTED = {
    "promote": (0, "_write=yes"),
    "hold": (0, "_write=no"),
    "unknown": (1, "::error::"),
    "noboard": (1, "::error::"),
    "_bogus": (1, "::error::"),
}
for verdict, (want_rc, want) in POLICY_EXPECTED.items():
    rc, out = sh(f"""
set -euo pipefail
NUMBER=1; CURRENT_COL='Some Column'; ARCHIVED=false
PROJECT_NUMBER=2; SOURCE_COLUMN={shlex.quote(CANON_SOURCE)}; TARGET_COLUMN={shlex.quote(CANON_TARGET)}
promote_decision() {{ echo {shlex.quote(verdict)}; }}
{POLICY}
echo "_write=${{_write:-unset}}"
""")
    record(rc == want_rc and want in out, f"policy: {verdict} -> {want} (rc {want_rc})",
           f"rc={rc}; {out.splitlines()[0] if out else '<no output>'}")

# ---------------------------------------------------------------------------
# 7. THE LABEL/EVENT GATE, and each refusal named (CLAUDE.md rule 10).
# ---------------------------------------------------------------------------
# A case that accepts any non-zero exit cannot say WHICH refusal it exercised, so
# every row below pins the specific reason string.
def gate(event="issues", label=None, want=None, pr="false", number="7") -> "tuple[int, str]":
    return sh(f"""
set -euo pipefail
EVENT_NAME={shlex.quote(event)}
LABEL_ADDED={shlex.quote(CANON_LABEL if label is None else label)}
BUG_LABEL={shlex.quote(CANON_LABEL if want is None else want)}
HAS_PR_PAYLOAD={shlex.quote(pr)}
NUMBER={shlex.quote(number)}
{LABEL_GATE}
echo "reached-the-board-read"
""")


GATE_CASES = [
    ("the exact label on an issue proceeds", {}, 0, "reached-the-board-read"),
    # GREEN, because it is the common case: this workflow fires on every
    # `labeled` event in 16 repos.
    ("a different label is a quiet no-op, not a failure",
     {"label": "work-type:docs"}, 0, "nothing to do"),
    # The substring trap, in both directions.
    ("'work-type:bugfix' is not 'work-type:bug'",
     {"label": CANON_LABEL + "fix"}, 0, "nothing to do"),
    ("a label the bug label is a prefix OF does not match",
     {"label": CANON_LABEL[:-1]}, 0, "nothing to do"),
    ("a pull_request payload is refused loudly",
     {"pr": "true"}, 1, "refuse:pull-request-payload"),
    ("a non-issues event is refused loudly",
     {"event": "pull_request"}, 1, "refuse:not-an-issues-event"),
    ("an unreadable/absent label in the payload fails rather than reading as 'no'",
     {"label": ""}, 1, "refuse:unreadable-label"),
    ("an empty configured label refuses instead of matching everything",
     {"want": ""}, 1, "refuse:no-configured-label"),
]
for name, kwargs, want_rc, needle in GATE_CASES:
    rc, out = gate(**kwargs)
    record(rc == want_rc and needle in out, f"label gate: {name}",
           f"rc={rc} (want {want_rc}); looked for {needle!r} in: "
           f"{out.splitlines()[-1] if out else '<no output>'}")

failed = [r for r in RESULTS if not r[0]]
print(f"\nbug-to-ready-selftest: {len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
