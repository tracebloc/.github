#!/usr/bin/env python3
"""Mutation harness for the reason-citation check (tracebloc/backend#2449).

`reason-citations-selftest.py` asserts the check's behaviour; this asserts the
SELFTEST. Break a rule in `scripts/reason-citations.py`, watch the suite redden,
restore. A case that stays green while the rule it names is deleted is vacuous,
and a green selftest log cannot tell you which of its assertions are
load-bearing.

THE MUTATION CALLS THE CODE UNDER TEST (CLAUDE.md rule 9). It edits
`scripts/reason-citations.py` on disk and re-runs the real suite, which executes
that same file as a subprocess. There is no second copy of the rule in here --
the alternative shape, re-implementing the rule inline and mutating the copy,
is indistinguishable from real coverage in a log and has bitten this org twice.

EVERY ANCHOR MUST MATCH EXACTLY ONCE. An anchor matching twice mutates an
arbitrary one, so the run reports "uncaught" for the wrong reason; an anchor
matching zero times is stale and fails the run exactly like an uncaught
mutation. That is the assertion that the mutation ACTUALLY APPLIED -- an inert
mutation and good coverage look identical in a log otherwise. `--dry` resolves
every anchor without running the suite, which is what belongs in the fast tier.

  reason-citations-mutations.py          run them all
  reason-citations-mutations.py --dry    resolve anchors only
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "reason-citations.py"
SUITE = ROOT / "scripts" / "tests" / "reason-citations-selftest.py"

# (label, old, new)
MUTATIONS = [
    # --- the load-bearing claim: a CLOSED issue is a finding ----------------
    ("a CLOSED issue counts as live, so backend#2449 case 1 goes unreported",
     'LIVE = {\n    ("Issue", "OPEN"),',
     'LIVE = {\n    ("Issue", "CLOSED"),\n    ("Issue", "OPEN"),'),
    ("the dead-state table is emptied, so nothing is ever a finding",
     '    if state in DEAD:\n        return DEAD[state]',
     '    if state in DEAD:\n        return None'),

    # --- the issue / pull-request distinction -------------------------------
    ("a MERGED pull request is treated as staleness, so provenance reads as a finding",
     '    ("PullRequest", "MERGED"),\n}',
     '}'),
    ("a pull request CLOSED WITHOUT MERGING counts as live",
     '    ("PullRequest", "OPEN"),',
     '    ("PullRequest", "OPEN"),\n    ("PullRequest", "CLOSED"),'),
    ("a state with no verdict passes instead of being refused",
     '    raise Finding(\n        f"unrecognised citation state {state!r}.',
     '    return None\n    raise Finding(\n        f"unrecognised citation state {state!r}.'),

    # --- which repo a bare `#N` means: the .github#314 defect ---------------
    ("a bare `#N` is resolved against `backend` again (.github#314)",
     "        repo = source_repo",
     '        repo = "backend"'),
    ("the bare-citation premise is never checked against GITHUB_REPOSITORY",
     '        if here and here.strip().lower() != f"{org}/{source_repo}".lower():',
     "        if False:"),
    ("`org:` and `source_repo:` fall back to hardcoded values instead of refusing",
     '    org, src = doc.get("org"), doc.get("source_repo")',
     '    org, src = doc.get("org") or "tracebloc", doc.get("source_repo") or ".github"'),

    # --- the matcher --------------------------------------------------------
    ("a dot is dropped from the prefix charset, so `.github#N` stops being parsed",
     r'PREFIX_CHARS = re.compile(r"[A-Za-z0-9._/-]")',
     r'PREFIX_CHARS = re.compile(r"[A-Za-z0-9_/-]")'),
    ("a malformed citation is silently dropped instead of reported",
     "            if not c.legal or c.number < 1:",
     "            if False and (not c.legal or c.number < 1):"),
    ("`#0` is accepted as a real ticket number",
     "c.number < 1",
     "c.number < 0"),
    ("`divergent:` stops being a place a reason can live",
     'REASON_KEYS = ("exempt", "divergent", "reason")',
     'REASON_KEYS = ("exempt", "reason")'),
    ("`shared_reasons:` is not scanned, so an anchor not yet aliased is invisible",
     '    shared = doc.get("shared_reasons")',
     "    shared = None"),
    ("the walk stops descending, so every nested reason is missed",
     '                else:\n                    walk(value, f"{where}.{key}")',
     "                else:\n                    pass"),

    # --- fail closed --------------------------------------------------------
    ("ZERO citations found reports clean instead of refusing",
     "    if not seen and not malformed:",
     "    if False and not seen and not malformed:"),
    ("an unreadable inventory is swallowed instead of refused",
     '        raise Finding(f"{path} could not be read ({exc}) -- refusing to report clean")',
     "        return {}"),
    ("an unparseable inventory is swallowed instead of refused",
     '        raise Finding(f"{path} could not be parsed ({exc}) -- refusing to report clean")',
     "        return {}"),
    ("an inventory that is not a mapping is accepted",
     "    if not isinstance(doc, dict):",
     "    if False and not isinstance(doc, dict):"),
    # NOTE: this one must REPLACE the raise, not precede it. The first draft
    # inserted the assignment above the `raise` and reported UNCAUGHT -- correctly,
    # because the mutation was inert. That is the harness doing its job: an inert
    # mutation and a missing case look identical until the anchor is read.
    ("a citation the API will not resolve is read as OPEN -- the guess rule 3 forbids",
     '''            raise Finding(
                f"{key} could not be read (no issue or pull request came back). That is "
                "\'cannot tell\', not \'still open\' -- fix the citation or the token\'s reach"
            )''',
     '            node = {"__typename": "Issue", "state": "OPEN"}'),
    ("a GraphQL payload with no data is treated as an empty read",
     '    data = payload.get("data")\n    if not isinstance(data, dict):',
     '    data = payload.get("data") or {}\n    if False and not isinstance(data, dict):'),
    ("a response that is not JSON is treated as no citations at all",
     "    except (ValueError, TypeError):\n        raise Finding(",
     "    except (ValueError, TypeError):\n        return {}\n        raise Finding("),

    # --- the exemption map, both halves -------------------------------------
    ("the exemption map is ignored, so this lands as a red gate",
     "    offenders = [f for f in findings if f[0] not in exempt] + \\\n"
     "                [m for m in malformed if m[0] not in exempt]",
     "    offenders = list(findings) + list(malformed)"),
    ("a stale exemption stops being reported, so the list becomes cover",
     "    return sorted(set(_exempt()) - {key for key, _, _ in findings})",
     "    return []"),
]


def apply_one(src, old, new):
    n = src.count(old)
    if n != 1:
        raise LookupError("anchor matched %d times, expected exactly 1: %r" % (n, old[:80]))
    out = src.replace(old, new, 1)
    return None if out == src else out


def main():
    dry = "--dry" in sys.argv
    pristine = GUARD.read_text(encoding="utf-8")
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
        GUARD.write_text(mutated, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            run = subprocess.run(
                [sys.executable, "-B", str(SUITE)],
                capture_output=True, text=True, cwd=str(ROOT), env=env,
            )
        finally:
            # ALWAYS restore, including on a crash. A mutation left on disk makes
            # every later run measure the wrong script, and the tell is a suite
            # that reddens for reasons nobody typed.
            GUARD.write_text(pristine, encoding="utf-8")
        caught = [line.strip()[6:].strip() for line in run.stdout.splitlines()
                  if line.strip().startswith("FAIL  ")]
        # A crash counts as caught ONLY if the suite actually ran and reported. A
        # bare traceback with no case output means the mutation broke the harness
        # rather than being detected by a case, which is not coverage.
        reported = "reason-citations-selftest:" in run.stdout
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (label, ", ".join(caught)[:140]))
        elif not reported:
            uncaught.append((label, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % label)
        else:
            uncaught.append((label, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % label)

    if GUARD.read_text(encoding="utf-8") != pristine:
        sys.stderr.write("::error::%s was left mutated. Restore it from git.\n" % GUARD.name)
        return 2

    print("\n%d mutation(s): %d stale, %d uncaught" % (len(MUTATIONS), len(stale), len(uncaught)))
    for label, why in stale:
        sys.stderr.write("::error::STALE mutation `%s`: %s\n" % (label, why))
    for label, why in uncaught:
        sys.stderr.write(
            "::error::UNCAUGHT `%s`: %s. Add a case that fails under it, or delete the "
            "mutation if the rule is genuinely not worth pinning.\n" % (label, why))
    return 1 if (stale or uncaught) else 0


if __name__ == "__main__":
    raise SystemExit(main())
