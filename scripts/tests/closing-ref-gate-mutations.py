#!/usr/bin/env python3
"""Mutation harness for the closing-ref gate (tracebloc/backend#2364).

`closing-ref-gate-selftest.py` asserts the gate's behaviour; this asserts the
SELFTEST. Break a rule in `scripts/closing-ref-gate.py`, watch the suite redden,
restore. A case that stays green under its own rule being deleted is vacuous, and
a green log cannot tell you which of a hundred assertions are load-bearing.

THE MUTATION CALLS THE CODE UNDER TEST (CLAUDE.md rule 9). It edits
`scripts/closing-ref-gate.py` on disk and re-runs the real suite, which imports
that same file by path. There is no second copy of any rule in here -- the
alternative shape, re-implementing the check inline and mutating the copy, is
indistinguishable from real coverage in a log and has bitten this org twice
(e2e-test-agent#114, #115).

EVERY ANCHOR MUST MATCH EXACTLY ONCE. An anchor matching twice mutates an
arbitrary one, so the run reports "uncaught" for the wrong reason; an anchor
matching zero times is stale and fails the run exactly like an uncaught
mutation. That is the assertion that the anchor ACTUALLY APPLIED -- an inert
mutation and good coverage look identical in a log otherwise. `--dry` resolves
every anchor without running the suite, which is what belongs in the fast tier.

  closing-ref-gate-mutations.py          run them all
  closing-ref-gate-mutations.py --dry    resolve anchors only

A MUTATION MUST BREAK THE RULE, NOT THE HARNESS. Several of the guards below
raise `Unreadable` from a path where removing the guard leads to a TypeError or
an AttributeError instead. Those still count as caught, and deliberately so: the
suite's `expect_unreadable` reports "raised TypeError instead of Unreadable" as a
FAILURE rather than letting it escape, so the suite still reports and the
mutation is still detected by a named case. What does NOT count is a mutation
that stops the suite reporting at all -- that is scored UNCAUGHT with the reason
said out loud, because a broken harness is not coverage.

THE BYTECODE CACHE IS DISARMED for the reason bugbot-gate-mutations.py records
at length: a pyc is revalidated on (mtime-to-the-second, byte size), several of
these mutations change the file by the same number of bytes, and back-to-back
runs inside one second would otherwise serve the previous mutation's bytecode --
turning a caught mutation into an uncaught report with nothing in the log saying
so.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "closing-ref-gate.py"
SUITE = ROOT / "scripts" / "tests" / "closing-ref-gate-selftest.py"

# (label, old, new)
MUTATIONS = [
    # --- what the TITLE names: precision, and the measured negative case -----
    ("a loose `#N` anywhere in the title counts as a ticket again",
     r'PAREN_BARE_RE = re.compile(r"\(\s*#(\d+)\s*\)")',
     r'PAREN_BARE_RE = re.compile(r"#(\d+)")'),
    ("the type list is hardcoded, so a declared type stops being read",
     r'SCOPE_RE = re.compile(r"^\s*[A-Za-z]+\s*\(([^()]*)\)\s*!?:")',
     r'SCOPE_RE = re.compile(r"^\s*(?:fix|feat)\s*\(([^()]*)\)\s*!?:")'),
    ("a breaking-change `!` hides the scope",
     r'SCOPE_RE = re.compile(r"^\s*[A-Za-z]+\s*\(([^()]*)\)\s*!?:")',
     r'SCOPE_RE = re.compile(r"^\s*[A-Za-z]+\s*\(([^()]*)\)\s*:")'),
    ("a dotted repo name (`.github`) stops parsing",
     r'PAREN_REPO_RE = re.compile(r"\(\s*(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9.][A-Za-z0-9._-]*)#(\d+)\s*\)")',
     r'PAREN_REPO_RE = re.compile(r"\(\s*(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)\s*\)")'),
    ("a `#N` scope stops being a ticket",
     r'SCOPE_BARE_RE = re.compile(r"^#?(\d+)$")',
     r'SCOPE_BARE_RE = re.compile(r"^(\d+)$")'),
    ("a scope that is a word is read as a repo plus a number",
     r'SCOPE_REPO_RE = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)$")',
     r'SCOPE_REPO_RE = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9][A-Za-z0-9._-]*)#?(\d+)$")'),
    ("the same ticket named twice becomes two references",
     "    unique = []\n    for ref in refs:",
     "    unique = list(refs)\n    for ref in []:"),
    ("a blank title reads as `names no ticket` instead of a cannot-tell",
     "    if title is None or not str(title).strip():",
     "    if False and (title is None or not str(title).strip()):"),

    # --- the comparison: one function, three verdicts ------------------------
    # A bare number names NO repo. Resolving it locally is the mapping this file
    # is forbidden to hold, and it is what makes client-runtime's `fix(2218)`
    # (backend#2218) look wrong.
    ("a bare number stops being satisfied by another repo",
     "            return LINKED\n        if ref.repo.lower() != name.lower():",
     "            return WRONG_REPO\n        if ref.repo.lower() != name.lower():"),
    ("the wrong-repo verdict collapses into `missing`, losing the trap's name",
     "        number_seen = True",
     "        number_seen = False"),
    ("repo comparison becomes case-sensitive",
     "        if ref.repo.lower() != name.lower():",
     "        if ref.repo != name:"),
    ("an owner mismatch is ignored, so another org's issue satisfies the title",
     "        if ref.owner is not None and ref.owner.lower() != owner.lower():",
     "        if False and ref.owner is not None and ref.owner.lower() != owner.lower():"),
    ("the number no longer has to match at all",
     "        if number != ref.number:",
     "        if False and number != ref.number:"),

    # --- the three reported states stay three --------------------------------
    ("a title naming no ticket is treated as a checked PASS",
     "    if not refs:",
     "    if False and not refs:"),
    ("the wrong-repo finding is reported as a plain missing link",
     "        if verdict == WRONG_REPO:",
     "        if False and verdict == WRONG_REPO:"),

    # --- the one deliberate fail-open, pinned in BOTH directions -------------
    ("every PR is treated as a draft, so the check never fires",
     '    if pr.get("isDraft"):',
     '    if True or pr.get("isDraft"):'),
    ("the draft exemption is removed, so a draft is checked like a ready PR",
     '    if pr.get("isDraft"):',
     '    if False and pr.get("isDraft"):'),

    # --- fail closed: each of these is a "cannot tell" turned quiet ----------
    # THE ONE THAT MATTERS MOST. A link the token cannot read is missing from
    # `nodes` while `totalCount` still counts it -- identical to "not linked".
    ("the truncation/permission-filter test never fires, so a filtered graph reads as unlinked",
     "    if total > len(nodes):",
     "    if False and total > len(nodes):"),
    ("truncation compares against the CAP, refusing an exactly-full page",
     "    if total > len(nodes):",
     "    if total >= PAGE_CAP:"),
    ("a missing totalCount no longer rules truncation out",
     '    if total is None:\n        raise Unreadable(',
     '    if False and total is None:\n        raise Unreadable('),
    ("a non-connection is read as an empty one instead of refused",
     "    if not isinstance(conn, dict):",
     "    if False and not isinstance(conn, dict):"),
    ("a null node in the link graph is accepted",
     "        if not isinstance(node, dict):",
     "        if False and not isinstance(node, dict):"),
    ("a node with no owner/name repository is accepted",
     '        if "/" not in full or not isinstance(number, int):',
     '        if False and ("/" not in full or not isinstance(number, int)):'),
    ("PAGE_CAP stops being derived and is hardcoded wrong",
     r'PAGE_CAP = max([int(n) for n in re.findall(r"first:\s*(\d+)", QUERY)] or [0])',
     "PAGE_CAP = 100"),
    ("the totalCount self-check never reports a blind connection",
     '        if match is None or "totalCount" not in match.group(1):',
     '        if False and (match is None or "totalCount" not in match.group(1)):'),
    ("the guarded-connection list is emptied, so the self-check checks nothing",
     'PAGED_CONNECTIONS = ("closingIssuesReferences",)',
     "PAGED_CONNECTIONS = ()"),
    ("the preflight no longer refuses a query that stopped asking for totalCount",
     "    if blind:",
     "    if False and blind:"),

    # --- the read seam ------------------------------------------------------
    ("a nonzero gh exit is ignored",
     "    if proc.returncode != 0:",
     "    if False and proc.returncode != 0:"),
    ("a GraphQL errors[] payload at exit 0 is accepted",
     '    if payload.get("errors"):',
     '    if False and payload.get("errors"):'),
    ("pullRequest: null is read as an empty PR instead of refused",
     '    if pr is None:\n        raise Unreadable("no such pull request',
     '    if False and pr is None:\n        raise Unreadable("no such pull request'),

    # --- SOFT_FAIL governs FINDINGS, never the check's own integrity ---------
    ("SOFT_FAIL starts softening a cannot-tell",
     "        _emit(FAIL, [], exc)\n        return 2",
     "        _emit(FAIL, [], exc)\n        return 0 if soft else 2"),
    ("SOFT_FAIL is read as any non-empty value",
     '    soft = (os.environ.get("SOFT_FAIL") or "").strip().lower() == "true"',
     '    soft = (os.environ.get("SOFT_FAIL") or "").strip() != ""'),
    ("a finding always exits 0, so nothing is ever reported red",
     "    return 0 if soft else 1",
     "    return 0"),
    ("a malformed REPO/PR_NUMBER is guessed at instead of refused",
     '    if "/" not in repo or not number.isdigit():',
     '    if False and ("/" not in repo or not number.isdigit()):'),
    # --- the remedy must not guess a repo (Bugbot, .github#314) --------------
    # Reverts the FIX, not the prose around it: put a guessed repo back into the
    # remedy a bare title number gets. This is the shape that greened the gate on a
    # wrong link and closed the wrong ticket.
    ("the bare-number remedy names a guessed repo instead of a placeholder",
     '"which repo owns it -- and will accept a link to any repo at that "\n                "number. Add `Closes <owner>/<repo>#%d` for the repo that actually "',
     '"which repo owns it -- and will accept a link to any repo at that "\n                "number. Add `Closes tracebloc/backend#%d` for the repo that actually "'),
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
        # A crash counts as caught ONLY if the suite actually ran and reported; a
        # bare traceback with no assertion output means the mutation broke the
        # harness rather than being detected by a case, which is not coverage.
        reported = "closing-ref-gate-selftest:" in run.stdout
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
