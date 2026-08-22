#!/usr/bin/env python3
"""Mutation harness for the Bugbot review gate (tracebloc/backend#2284).

`bugbot-gate-selftest.py` asserts the gate's behaviour; this asserts the
SELFTEST. Break a rule in `scripts/bugbot-gate.py`, watch the suite redden,
restore. A case that stays green under its own rule being deleted is vacuous,
and a green selftest log cannot tell you which of its 51 assertions are
load-bearing.

THE MUTATION CALLS THE CODE UNDER TEST (CLAUDE.md rule 9). It edits
`scripts/bugbot-gate.py` on disk and re-runs the real suite, which imports that
same file by path. There is no second copy of the rule anywhere in here -- the
alternative shape, re-implementing the check inline and mutating the copy, is
indistinguishable from real coverage in a log and has bitten this org twice.

EVERY ANCHOR MUST MATCH EXACTLY ONCE. An anchor matching twice mutates an
arbitrary one, so the run reports "uncaught" for the wrong reason; an anchor
matching zero times is stale and fails the run exactly like an uncaught
mutation. That is the assertion that the anchor ACTUALLY APPLIED -- an inert
mutation and good coverage look identical in a log otherwise. `--dry` resolves
every anchor without running the suite, which is what belongs in the fast tier.

  bugbot-gate-mutations.py          run them all
  bugbot-gate-mutations.py --dry    resolve anchors only

THE BYTECODE CACHE HAD TO BE DISARMED, and finding out why cost the first real
debugging on this file -- so it is written down rather than left as a flag.

The suite imports the gate by path, which makes CPython write
`scripts/__pycache__/bugbot-gate.*.pyc`. A pyc is revalidated against the
source's (mtime-to-the-second, byte size) -- and THREE of the mutations below
lengthen the file by exactly the same 10 bytes, because they all disable a guard
by prefixing `False and `. Run back to back inside one second, the second such
mutation matches the pyc the FIRST one left behind, so the suite silently
executed the previous mutation's bytecode: two mutations reported UNCAUGHT that
are in fact caught, and nothing in the log said so.

That is the failure this whole tier exists to prevent, one level up -- an inert
mutation and real coverage look identical in a log (CLAUDE.md rule 5). So the
suite is launched with `-B` AND `PYTHONDONTWRITEBYTECODE`, and any pre-existing
cache file for the gate is removed first. Belt and braces on purpose: `-B` stops
this run writing one, the unlink stops a cache from a DEVELOPER's earlier plain
`python3 scripts/tests/bugbot-gate-selftest.py` being read.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "bugbot-gate.py"
SUITE = ROOT / "scripts" / "tests" / "bugbot-gate-selftest.py"

# (label, old, new)
MUTATIONS = [
    # --- (A) the load-bearing claim: a TERMINAL verdict on THIS head --------
    ("a missing Bugbot verdict reports PASS instead of PENDING",
     '    check = bugbot_check(pr)\n    if check is None:\n        return PENDING, [',
     '    check = bugbot_check(pr)\n    if check is None:\n        return PASS, ['),
    ("a still-RUNNING Bugbot counts as a terminal verdict",
     '    if check.get("status") not in TERMINAL_STATUSES:',
     '    if False and check.get("status") not in TERMINAL_STATUSES:'),
    ("every status is treated as terminal",
     'TERMINAL_STATUSES = {"COMPLETED"}',
     'TERMINAL_STATUSES = {"COMPLETED", "IN_PROGRESS", "QUEUED"}'),
    # The app-slug match is what makes a Bugbot RENAME harmless. Swapping it for
    # a display-name match is the exact drift the header argues against.
    ("the check is matched on its DISPLAY NAME instead of the producing app",
     '        slug = (((node.get("checkSuite") or {}).get("app") or {}) or {}).get("slug")\n'
     '        if slug == BUGBOT_APP_SLUG:',
     '        slug = node.get("name")\n'
     '        if slug == "Cursor Bugbot":'),

    # --- the one deliberate fail-open, pinned in both directions ----------
    ("every PR is treated as a draft, so the gate never fires",
     '    if pr.get("isDraft"):',
     '    if True or pr.get("isDraft"):'),
    ("the draft exemption is removed, so a draft is gated like a ready PR",
     '    if pr.get("isDraft"):',
     '    if False and pr.get("isDraft"):'),

    # --- (B) severity and the threshold -----------------------------------
    ("the threshold compares for EQUALITY, so a Critical slips past a `high` gate",
     'if not f["resolved"] and SEVERITY_RANK.index(f["severity"]) >= floor',
     'if not f["resolved"] and SEVERITY_RANK.index(f["severity"]) == floor'),
    ("the rank order is scrambled, so `high` no longer outranks `medium`",
     'SEVERITY_RANK = ["low", "medium", "high", "critical"]',
     'SEVERITY_RANK = ["low", "high", "medium", "critical"]'),
    ("a RESOLVED finding still blocks, so resolve-and-ship becomes impossible",
     'if not f["resolved"] and SEVERITY_RANK.index(f["severity"]) >= floor',
     'if SEVERITY_RANK.index(f["severity"]) >= floor'),
    ("the severity regex is anchored to one case, so `**high Severity**` misses",
     r'SEVERITY_RE = re.compile(r"\*\*([A-Za-z]+)\s+Severity\*\*")',
     r'SEVERITY_RE = re.compile(r"\*\*([A-Z][a-z]+) Severity\*\*")'),

    # --- what is a finding -------------------------------------------------
    ("any Bugbot comment counts as a finding, marker or not",
     '        if login != BUGBOT_LOGIN or FINDING_MARKER not in body:',
     '        if login != BUGBOT_LOGIN:'),
    ("a HUMAN thread quoting a severity line counts as a Bugbot finding",
     '        if login != BUGBOT_LOGIN or FINDING_MARKER not in body:',
     '        if FINDING_MARKER not in body:'),

    # --- fail closed: every one of these is a "cannot tell" turned quiet ----
    ("an unrecognised severity ranks as harmless instead of refusing",
     '    unknown = [f for f in found if f["severity"] not in SEVERITY_RANK]\n    if unknown:',
     '    unknown = [f for f in found if f["severity"] not in SEVERITY_RANK]\n    if False and unknown:'),
    # --- truncation: ONE helper, both connections, and the bug Bugbot found --
    ("the truncation test never fires, so a cut page reports clean",
     '    if total > len(nodes):',
     '    if False and total > len(nodes):'),
    # This mutation reintroduces the EXACT defect Bugbot caught on .github#305:
    # comparing against the cap instead of the node count, which refuses a
    # COMPLETE page. It is here so that regression cannot come back quietly.
    ("truncation compares against the CAP again, refusing an exactly-full page",
     '    if total > len(nodes):',
     '    if total >= PAGE_CAP:'),
    ("a missing totalCount no longer rules truncation out",
     """    if total is None:
        raise Unreadable(
            "%s did not report totalCount""",
     """    if False and total is None:
        raise Unreadable(
            "%s did not report totalCount"""),
    ("a non-connection is read as an empty one instead of refused",
     '    if not isinstance(conn, dict):',
     '    if False and not isinstance(conn, dict):'),
    ("PAGE_CAP stops being derived and is hardcoded wrong",
     r'PAGE_CAP = max([int(n) for n in re.findall(r"first:\s*(\d+)", QUERY)] or [0])',
     'PAGE_CAP = 50'),

    # --- the self-check that keeps the truncation guards honest -------------
    ("the totalCount self-check never reports a blind connection",
     '        if match is None or "totalCount" not in match.group(1):',
     '        if False and (match is None or "totalCount" not in match.group(1)):'),
    ("the self-check only looks at one connection, leaving the other unguarded",
     'PAGED_CONNECTIONS = ("contexts", "reviewThreads")',
     'PAGED_CONNECTIONS = ("contexts",)'),
    ("a PR with no commits is read as having nothing to check",
     '        raise Unreadable("PR reported no commits, so there is no head to check")',
     '        return None'),
    ("an inconsistent read (last commit != head) is accepted",
     '    if head != pr.get("headRefOid"):',
     '    if False and head != pr.get("headRefOid"):'),
    ("a threshold outside the declared rank silently becomes the lowest",
     '    if min_severity not in SEVERITY_RANK:',
     '    if False and min_severity not in SEVERITY_RANK:'),
    ("a finding with NO severity line is treated as harmless",
     '    return match.group(1).lower() if match else None',
     '    return match.group(1).lower() if match else "low"'),

    # --- the read seam -----------------------------------------------------
    ("a nonzero gh exit is ignored",
     '    if proc.returncode != 0:\n        raise Unreadable(',
     '    if False and proc.returncode != 0:\n        raise Unreadable('),
    ("a GraphQL errors[] payload at exit 0 is accepted",
     '    if payload.get("errors"):',
     '    if False and payload.get("errors"):'),
    ("pullRequest: null is read as an empty PR instead of refused",
     '    if pr is None:\n        raise Unreadable("no such pull request',
     '    if False and pr is None:\n        raise Unreadable("no such pull request'),
]


def _drop_bytecode_cache():
    """Remove any cached bytecode for the gate. See the header: a stale pyc makes
    a caught mutation report as uncaught."""
    try:
        cached = importlib.util.cache_from_source(str(GATE))
    except (ValueError, NotImplementedError):
        return
    try:
        os.unlink(cached)
    except OSError:
        pass


def apply_one(src, old, new):
    n = src.count(old)
    if n != 1:
        raise LookupError("anchor matched %d times, expected exactly 1: %r" % (n, old[:80]))
    out = src.replace(old, new, 1)
    return None if out == src else out


def main():
    dry = "--dry" in sys.argv
    pristine = GATE.read_text(encoding="utf-8")
    stale, uncaught = [], []

    for label, old, new in MUTATIONS:
        try:
            mutated = apply_one(pristine, old, new)
        except LookupError as exc:
            stale.append((label, str(exc)))
            continue
        if mutated is None:
            stale.append((label, "NO-OP: the mutation changed nothing"))
            continue
        if dry:
            print("  anchor ok  %s" % label)
            continue
        GATE.write_text(mutated, encoding="utf-8")
        _drop_bytecode_cache()
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            run = subprocess.run(
                [sys.executable, "-B", str(SUITE)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
        finally:
            # ALWAYS restore, including on a crash. A mutation left on disk makes
            # every later run measure the wrong script, and the tell is a suite
            # that reddens for reasons nobody typed.
            GATE.write_text(pristine, encoding="utf-8")
            _drop_bytecode_cache()
        caught = [
            line.strip()[6:].strip()
            for line in run.stdout.splitlines()
            if line.strip().startswith("FAIL:")
        ]
        # A crash counts as caught ONLY if the suite actually ran and reported;
        # a bare traceback with no assertion output means the mutation broke the
        # harness rather than being detected by a case, which is not coverage.
        reported = "bugbot-gate-selftest:" in run.stdout
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (label, ", ".join(caught)[:120]))
        elif not reported:
            uncaught.append((label, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % label)
        else:
            uncaught.append((label, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % label)

    if GATE.read_text(encoding="utf-8") != pristine:
        sys.stderr.write("::error::%s was left mutated. Restore it from git.\n" % GATE.name)
        return 2

    print("\n%d mutation(s): %d stale, %d uncaught" % (len(MUTATIONS), len(stale), len(uncaught)))
    for label, why in stale:
        sys.stderr.write("::error::STALE mutation `%s`: %s\n" % (label, why))
    for label, why in uncaught:
        sys.stderr.write(
            "::error::UNCAUGHT `%s`: %s. Add a case that fails under it, or delete "
            "the mutation and say why it is not worth pinning.\n" % (label, why)
        )
    return 1 if (stale or uncaught) else 0


if __name__ == "__main__":
    raise SystemExit(main())
