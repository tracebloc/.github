#!/usr/bin/env python3
"""Selftest for the FR gate's base-branch decision and its trigger (backend#2840).

WHAT WENT WRONG (backend#2840, measured on .github#388)

`gate` is a REQUIRED status check produced by `fr-gate-caller.yml`. The caller
carried `branches: [staging, main]`, and `branches:` filters on the PR's base AT
EVENT TIME. A PR retargeted OUT of that set — `main -> develop` — fires `edited`
with base=develop, the filter rejects it, and the gate never re-runs. The last
run stays FAILURE, welded to the head sha, and nothing can clear it: `synchronize`
needs a push and a retarget has none. The required check blocks the PR forever.

The fix drops the filter and lets the JOB decide from the base it can already see:
`fr-gate.yml` maps a non-promotion base to an empty `required`, and every gating
step is `if: steps.target.outputs.required != ''`, so on develop the job reports
`gate` SUCCESS and supersedes the stale FAILURE.

WHAT THIS ASSERTS, AND WHY IT IS EXTRACTED RATHER THAN COPIED (CLAUDE.md rule 9)

Two independent claims, both of which must hold or the class reopens:

  1. THE MAPPING. The `case "$BASE"` block is pulled verbatim out of fr-gate.yml
     and executed over a base domain written down HERE (not derived from the block
     it checks — rule 6/9). staging/main/master must yield a non-empty `required`
     (the gate proceeds and can BLOCK — never weaken that), and develop / any other
     base must yield empty (the gate skips and reports green — the retarget clears).
     A copy of the mapping here would drift from the workflow while staying green,
     which is the very defect class this file guards.

  2. THE TRIGGER. The caller must re-run on a retarget out of staging/main, so its
     `pull_request` trigger must carry NO `branches:` filter (else base=develop is
     rejected) and must keep `edited` (the only event a base change fires —
     .github#237, backend#1945).

  3. THE DISARM. The step whose error says "Manual promotion PRs are retired" —
     the message the issue calls actively misleading on a retargeted PR — must be
     guarded by `required != ''`, so it cannot fire (and cannot exit 1) on a
     develop base. That guard is the mechanism by which the job goes green.

Exit 0 when every case behaves as specified.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(HERE, os.pardir, os.pardir, ".github", "workflows")
REUSABLE = os.path.join(WORKFLOWS, "fr-gate.yml")
CALLER = os.path.join(WORKFLOWS, "fr-gate-caller.yml")

FAILURES: "list[str]" = []
COUNT = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if not condition:
        FAILURES.append(label + ((" -- " + detail) if detail else ""))


def dedent(block: str) -> str:
    """Strip the workflow's indentation so the block runs standalone."""
    indent = min((len(ln) - len(ln.lstrip()) for ln in block.splitlines()
                  if ln.strip()), default=0)
    return "\n".join(ln[indent:] if ln[:indent].isspace() else ln
                     for ln in block.splitlines())


def runs(path: str) -> "list[str]":
    doc = yaml.safe_load(open(path))
    return [s["run"] for j in doc["jobs"].values()
            for s in j.get("steps", []) if "run" in s]


def steps(path: str) -> "list[dict]":
    doc = yaml.safe_load(open(path))
    return [s for j in doc["jobs"].values() for s in j.get("steps", [])]


def on_block(path: str) -> dict:
    """`on:` is YAML's `True` key once safe_load is done with it — handle both."""
    doc = yaml.safe_load(open(path))
    return doc.get("on", doc.get(True))


def extract(path: str, pattern: str, what: str) -> str:
    for body in runs(path):
        m = re.search(pattern, body, re.S | re.M)
        if m:
            return dedent(m.group(0))
    sys.exit(f"could not find {what} in {os.path.basename(path)} — was it renamed "
             "or reshaped? This test refuses to fall back to a copy.")


def sh(script: str) -> "tuple[int, str]":
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return out.returncode, (out.stdout + out.stderr).strip()


# ---------------------------------------------------------------------------
# 1. THE MAPPING — the real `case "$BASE"` block, run over a base domain.
# ---------------------------------------------------------------------------
CASE = extract(REUSABLE, r"^\s*case \"\$BASE\" in.*?^\s*esac\s*$",
               'the `case "$BASE"` mapping')


def required_for(base: str) -> str:
    """Run fr-gate.yml's own case block for BASE=base; echo the `required` value."""
    with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
        out_file = tf.name
    try:
        rc, out = sh(f"""
set -euo pipefail
BASE={base!r}
GITHUB_OUTPUT={out_file!r}
{CASE}
""")
        if rc != 0:
            return f"ERROR({rc}): {out}"
        with open(out_file) as fh:
            body = fh.read()
        m = re.search(r"^required=(.*)$", body, re.M)
        return m.group(1) if m else "<no required= written>"
    finally:
        os.unlink(out_file)


# Expectations written down HERE, independently of the block under test. A
# non-empty value means the gate PROCEEDS (and can block); an empty value means it
# SKIPS and reports green. Both promotion targets and the retarget-out case are
# named explicitly.
GATING = {
    "staging": "On dev",
    "main": "Ready for prod",
    "master": "Ready for prod",
}
NON_GATING = ["develop", "feature/x-y", "hotfix-backmerge/z", "release/1.2", ""]

for base, want in GATING.items():
    got = required_for(base)
    check(f"base={base!r} gates with required={want!r}", got == want,
          f"got {got!r} — a promotion base must stay gated, never weakened")

for base in NON_GATING:
    got = required_for(base)
    check(f"base={base!r} is NOT gated (required empty -> gate reports green)",
          got == "",
          f"got {got!r} — a non-promotion base must skip so a retargeted PR clears")

# THE LOAD-BEARING ROW of backend#2840: a PR retargeted main -> develop. If
# `develop` mapped to anything non-empty the gate would run on it and the stale
# FAILURE would never be superseded by a green run.
check("the retarget-out base 'develop' yields an empty required",
      required_for("develop") == "",
      "this is the exact case .github#388 dead-locked on")


# ---------------------------------------------------------------------------
# 2. THE TRIGGER — the caller must re-run on a retarget out of staging/main.
# ---------------------------------------------------------------------------
CALLER_ON = on_block(CALLER)
check("caller has a pull_request trigger", "pull_request" in CALLER_ON,
      f"on: keys = {sorted(map(str, CALLER_ON))}")
PR = CALLER_ON.get("pull_request") or {}

# THE FIX. A `branches:` filter rejects base=develop, so the gate never re-runs on
# the retarget out — the entire backend#2840 mechanism. It must be absent.
check("caller carries NO `branches:` filter (else a retarget-out is never seen)",
      "branches" not in PR,
      f"branches = {PR.get('branches')!r} — this is exactly backend#2840")

# THE SAME WELD IN ITS OTHER SPELLINGS (Bugbot, backend#3228). `branches:` is not
# the only filter that skips a required check: `branches-ignore: [develop]` rejects
# the retarget-out identically, and `paths:` / `paths-ignore:` make the required
# `gate` context never ARRIVE on a PR that touches the wrong files — a permanent
# pending that blocks the merge exactly as the stale FAILURE did, with nothing to
# clear it. The mutation harness only reintroduced `branches:`, so these three
# spellings would have welded the gate while this suite stayed green (rule 6:
# derive the input domain and test all of it). None may appear on the trigger.
for _skip_filter in ("branches-ignore", "paths", "paths-ignore"):
    check(f"caller carries NO `{_skip_filter}:` filter (skips the required gate too)",
          _skip_filter not in PR,
          f"{_skip_filter} = {PR.get(_skip_filter)!r} — welds the required gate like backend#2840")

# THE #1945 HALF. `edited` is the only event a base change fires; without it even
# the main<->staging retargets go stale.
check("caller keeps `edited` in its trigger types",
      "edited" in (PR.get("types") or []),
      f"types = {PR.get('types')!r} — dropping it reopens backend#1945")


# ---------------------------------------------------------------------------
# 3. THE DISARM — the misleading step cannot fire on a non-promotion base.
# ---------------------------------------------------------------------------
# The issue calls the "Manual promotion PRs are retired" error actively misleading
# on a retargeted PR. Its step must be guarded by `required != ''`, so on a develop
# base it is skipped (never exit 1) and the job can report green.
promo_steps = [s for s in steps(REUSABLE)
               if "Manual promotion PRs are retired" in s.get("run", "")]
check("the promotion-shape guard step was located", len(promo_steps) == 1,
      f"found {len(promo_steps)} step(s) carrying the retired-promotion error")
if len(promo_steps) == 1:
    guard = promo_steps[0].get("if", "")
    check("the misleading promotion-shape guard is gated by `required != ''`",
          "steps.target.outputs.required != ''" in guard,
          f"if = {guard!r} — without this it exits 1 on a develop retarget")


if FAILURES:
    print("fr-gate-selftest: %d/%d FAILED" % (len(FAILURES), COUNT))
    for f in FAILURES:
        print("  FAIL: " + f)
    sys.exit(1)
print("fr-gate-selftest: %d assertions, all passed" % COUNT)
