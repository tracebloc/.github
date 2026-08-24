#!/usr/bin/env python3
"""Mutation harness for the branch-ownership rule (backend#2365).

WHY. `branch-owner-selftest.py` asserts the rule's behaviour; this asserts the
SELFTEST. Break the rule, watch the suite redden, restore. A case that stays
green while the thing it names is broken is vacuous, and a green log cannot tell
you which of its cases are load-bearing. (No case count here: the suite prints its
own, and a tally in prose is exactly the number that goes stale.)

WHAT IS MUTATED, and why each one is a mutation somebody would really write: the
tip-author fall-through that the ticket was filed for, every fail-closed refusal
turned into a guess, and the two seams that decide whether "no PR" and "oldest
commit" mean what they say.

Every anchor must match EXACTLY ONCE. An anchor matching twice mutates an
arbitrary one of them, so an "uncaught" verdict is about the wrong site; an
anchor matching zero times is stale and fails the run, exactly like an uncaught
mutation. `--dry` resolves every anchor without running the suite, which is the
cheap check that belongs in `make lint`.

  branch-owner-mutations.py          run them all
  branch-owner-mutations.py --dry    resolve anchors only
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOD = ROOT / "scripts" / "branch_owner.py"
SUITE = ROOT / "scripts" / "tests" / "branch-owner-selftest.py"

# (label, old, new)
MUTATIONS = [
    # --- THE TICKET'S OWN BUG: the PR author stops winning -----------------
    ("the PR path is skipped, so a branch falls through to its commit author",
     "    if prs:\n        # A PR whose head oid IS the tip",
     "    if False and prs:\n        # A PR whose head oid IS the tip"),
    ("the exact-tip match is never found, so the overtaken PR decides",
     '            exact = [pr for pr in prs if (pr.get("headRefOid") or "") == tip_sha]',
     "            exact = []"),
    ("the tip sha is ignored, collapsing pr-exact into pr",
     "        if tip_sha:\n            exact =",
     "        if False:\n            exact ="),

    # --- fail-closed turned into a guess ----------------------------------
    ("an unread PR list no longer refuses",
     "    if pr_list_problem:\n        return _refuse(",
     "    if False and pr_list_problem:\n        return _refuse("),
    ("two authors at the exact tip are tie-broken instead of refused",
     '                if len(by) > 1:\n                    return _refuse(\n'
     '                        "two or more pull requests at this exact tip have different "',
     '                if len(by) > 1:\n                    return Attribution(\n'
     '                        sorted(by)[0], "pr-exact",\n'
     '                        "two or more pull requests at this exact tip have different "'),
    ("a reused head name is tie-broken instead of refused",
     "        if len(by) > 1:\n            # Branch names get reused.",
     "        if False:\n            # Branch names get reused."),
    ("a PR with no author falls through to the commit author",
     '        return _refuse(\n            f"the pull request(s) for this head ({_cite(prs)}) carry no author -- "',
     '        return _refuse_disabled(\n            f"the pull request(s) for this head ({_cite(prs)}) carry no author -- "'),
    ("a PR with no author AT THE TIP falls through to the name-reuse arm",
     '                return _refuse(\n                    f"the pull request(s) at this exact tip ({_cite(exact)}) carry no "',
     '                pass\n                _unused = (\n                    f"the pull request(s) at this exact tip ({_cite(exact)}) carry no "'),
    ("another branch's PRs are absorbed instead of refused",
     "    stray = [pr for pr in prs if",
     "    stray = [] and [pr for pr in prs if"),
    ("a branch with nothing to attribute is attributed to nobody, silently",
     '    return _refuse(\n        "no pull request, and no commit on this branch that is not already on the "',
     '    return Attribution("(nobody)", "first-commit",\n        "no pull request, and no commit on this branch that is not already on the "'),

    # --- the seams --------------------------------------------------------
    ("the commit author is the TIP again, because the log is not reversed",
     '    rc, out = _run(["git", "log", "--reverse", "--format=%an <%ae>",',
     '    rc, out = _run(["git", "log", "--format=%an <%ae>",'),
    ("the oldest line is no longer the one taken",
     "    return out.splitlines()[0].strip()",
     "    return out.splitlines()[-1].strip()"),
    ("a silently truncated PR list is reported as complete",
     "    if len(rows) >= PR_LIMIT:",
     "    if False and len(rows) >= PR_LIMIT:"),
    ("a failed gh call reports no problem",
     '        return {}, ("`gh pr list` failed -- no gh, no auth, or no network. "\n'
     '                    "Absence from a list that was never read proves nothing")',
     '        return {}, ""'),
    ("an unparseable PR list is reported as empty rather than unreadable",
     '        return {}, f"the pull-request list did not parse as JSON ({exc})"',
     '        return {}, ""'),
    ("a PR list that is not a list is accepted as empty",
     '        return {}, f"the pull-request list is a {type(rows).__name__}, not a list"',
     '        return {}, ""'),

    # --- the two defects the first live run turned up ----------------------
    ("origin/HEAD's bare short name is listed as a branch called `origin`",
     '        if "/" not in ref or ref.endswith("/HEAD"):',
     '        if ref.endswith("/HEAD"):'),
    # --- the two-empties distinction, on BOTH reads that have one -----------
    #
    # A test that only asserted "something empty came back" would pass in a world
    # where the failed and the genuine case refuse IDENTICALLY, so each pair is
    # mutated in both directions: collapse the failure into the success, and
    # collapse the success into the failure.
    ("first_commit_author collapses a failed read into 'no unique commits'",
     '    if rc != 0:\n        return "", (f"`git log {default}..{ref}` failed, so the oldest-commit "\n'
     '                    "signal could not be read")\n    if not out:\n        return "", ""',
     '    if rc != 0 or not out:\n        return "", ""'),
    ("first_commit_author's failure carries no reason",
     '        return "", (f"`git log {default}..{ref}` failed, so the oldest-commit "\n'
     '                    "signal could not be read")',
     '        return "", ""'),
    ("first_commit_author reports a GENUINELY empty history as a failed read",
     '    if not out:\n        return "", ""',
     '    if not out:\n        return "", "could not be read"'),
    ("main drops the failed-history reason, so it renders as 'no commits'",
     "        att = attribute(name, sha, prs.get(name, []), author,\n"
     "                        problem, default_problem or author_problem)",
     "        att = attribute(name, sha, prs.get(name, []), author,\n"
     "                        problem, default_problem)"),
    ("remote_branches reports a GENUINELY empty remote as a failed read",
     '        found.append((name, sha))\n    return found, ""',
     '        found.append((name, sha))\n    return found, ("`git for-each-ref` failed" if not found else "")'),

    ("a failed ref read is indistinguishable from an empty clone",
     '        return [], ("`git for-each-ref` failed, so this clone\'s branch list could "\n'
     '                    "not be read -- and an empty inventory would read as "\n'
     '                    "\'nothing to attribute\'")',
     '        return [], ""'),
    ("the ref list keeps going on a failed read, returning a partial sweep list",
     "    if rc != 0:\n        return [], (\"`git for-each-ref` failed",
     "    if False:\n        return [], (\"`git for-each-ref` failed"),
    ("a missing binary raises instead of failing closed",
     "    try:\n        proc = subprocess.run(args, capture_output=True, text=True)\n"
     "    except OSError as exc:\n        return 127, f\"{args[0]}: {exc}\"",
     "    proc = subprocess.run(args, capture_output=True, text=True)"),

    ("an absent tip is reported as an overtaken head",
     '            moved = ("the tip has moved past the PR head" if tip_sha\n'
     '                     else "no tip was supplied, so the PR head could not be compared")',
     '            moved = "the tip has moved past the PR head"'),

    # --- the two Bugbot findings on the first review ----------------------
    ("the reuse refusal claims a tip comparison it never made",
     '            tail = ("and none is at the current tip" if tip_sha\n'
     '                    else "and no tip was supplied to break the tie with")',
     '            tail = "and none is at the current tip"'),
    ("a withheld commit signal is reported as a finding about the branch",
     '    if first_commit_problem:\n        return _refuse(\n'
     '            "no pull request, and the oldest-commit signal was not measured: "\n'
     '            f"{first_commit_problem}"\n        )',
     '    if False:\n        return _refuse(\n'
     '            "no pull request, and the oldest-commit signal was not measured: "\n'
     '            f"{first_commit_problem}"\n        )'),
    ("the withheld and measured-empty refusals collapse into one wording",
     '            "no pull request, and the oldest-commit signal was not measured: "\n'
     '            f"{first_commit_problem}"',
     '            "no pull request, and no commit on this branch that is not already on the "\n'
     '            f"default branch, so there is nothing to attribute{first_commit_problem}"'),

    # --- the default branch, which the FIRST-COMMIT signal is measured against
    ("a stale origin/HEAD is returned as trustworthy",
     '        return out, (f"the remote\'s default branch could not be confirmed, and {out} "\n'
     '                     "is a clone-time cache that git never refreshes")',
     '        return out, ""'),
    ("the guessed default no longer admits to guessing",
     '            return guess, ("the remote\'s default branch could not be confirmed and "\n'
     '                           f"origin/HEAD is unset, so {guess} is a guess")',
     '            return guess, ""'),
    ("the remote is never asked, so the cache is the only source",
     '    args = ["gh", "repo", "view", "--json", "defaultBranchRef",\n'
     '            "--jq", ".defaultBranchRef.name"]',
     '    args = ["false"]'),

    # --- the structural guarantee -----------------------------------------
    ("a tip_author parameter is reintroduced",
     "    first_commit_author: str = \"\",\n    pr_list_problem: str = \"\",",
     "    first_commit_author: str = \"\",\n    tip_author: str = \"\",\n"
     "    pr_list_problem: str = \"\","),
    ("a declared signal is removed from SIGNALS, so the domain sweep shrinks",
     'SIGNALS = ("pr-exact", "pr", "first-commit", "unattributable")',
     'SIGNALS = ("pr-exact", "pr", "unattributable")'),
    ("UNATTRIBUTABLE stops being empty, so a refusal reads as a name",
     'UNATTRIBUTABLE = ""',
     'UNATTRIBUTABLE = "unknown"'),
]


def apply_one(src: str, old: str, new: str) -> "str | None":
    n = src.count(old)
    if n != 1:
        raise LookupError(f"anchor matched {n} times, expected exactly 1: {old[:70]!r}")
    out = src.replace(old, new, 1)
    return None if out == src else out


def main() -> int:
    dry = "--dry" in sys.argv
    pristine = MOD.read_text(encoding="utf-8")
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
            print(f"  anchor ok  {label}")
            continue
        MOD.write_text(mutated, encoding="utf-8")
        try:
            run = subprocess.run([sys.executable, str(SUITE)],
                                 capture_output=True, text=True, cwd=ROOT)
        finally:
            # ALWAYS restore, including on a crash. A mutation left on disk makes
            # every later run measure the wrong module, and the tell is a suite
            # that reddens for reasons nobody typed.
            MOD.write_text(pristine, encoding="utf-8")
        m = re.search(r"(\d+) passed, (\d+) failed", run.stdout)
        failed = int(m.group(2)) if m else (1 if run.returncode else 0)
        caught = [line.split("  ", 1)[-1].strip()
                  for line in run.stdout.splitlines() if line.startswith("FAIL  ")]
        # A crash counts as caught only because the suite still went red; say which
        # it was, so a mutation that merely breaks the import is not mistaken for a
        # case doing its job.
        if failed > 0:
            why = ", ".join(caught)[:110] if caught else f"exit {run.returncode}, no FAIL lines"
            print(f"  caught     {label}\n             by: {why}")
        else:
            uncaught.append(label)
            print(f"  UNCAUGHT   {label}")

    if MOD.read_text(encoding="utf-8") != pristine:
        sys.stderr.write(f"::error::{MOD.name} was left mutated. Restore it from git.\n")
        return 2

    print(f"\n{len(MUTATIONS)} mutation(s): {len(stale)} stale, {len(uncaught)} uncaught")
    for label, why in stale:
        sys.stderr.write(f"::error::STALE mutation `{label}`: {why}\n")
    for label in uncaught:
        sys.stderr.write(
            f"::error::UNCAUGHT `{label}`: the suite passed with this broken. Add a "
            "case that fails under it, or delete the mutation and say why it is not "
            "worth pinning.\n")
    return 1 if (stale or uncaught) else 0


if __name__ == "__main__":
    raise SystemExit(main())
