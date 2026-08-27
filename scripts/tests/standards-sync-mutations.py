#!/usr/bin/env python3
"""Mutation harness for the sync's PR-author split (tracebloc/backend#2590).

`standards-sync-selftest.py` asserts the sync's behaviour; this asserts the
SELFTEST. Break a rule in `scripts/standards-sync.py`, watch the suite redden,
restore. The identity a PR is opened with is INVISIBLE on the result -- same
title, same body, same diff, only a different author -- so these cases are
exactly the kind that can sit green and load-bearing-looking while asserting
nothing, and a green log of 37 checks cannot tell you which ones matter.

THE MUTATION CALLS THE CODE UNDER TEST (CLAUDE.md rule 9). It edits
`scripts/standards-sync.py` on disk and re-runs the real suite, which imports
that same file by path. No rule is re-implemented here.

EVERY ANCHOR MUST MATCH EXACTLY ONCE, and the result must COMPILE. Both halves
were learned the hard way while writing this file, and the second is not in the
sibling harnesses:

  * an anchor matching zero or many times fails the run like an uncaught
    mutation -- that is the assertion that the mutation actually applied;
  * a mutation can apply cleanly and still be MALFORMED. The first draft of the
    `pr list` case spliced `token=...` into the middle of a call, producing
    `positional argument follows keyword argument`. The suite could not import
    the file, crashed, printed no `FAIL:` line, and the run recorded UNCAUGHT --
    identical in the log to a mutation a vacuous test missed. So each mutation is
    `compile()`d before it is written, and a mutation that will not compile is
    reported as MALFORMED rather than counted either way.

A CRASH IS NOT A CATCH. The other half of the same lesson: the empty-PAT case
originally scripted only `gh pr list`, so a fallback over-ran the stub and raised
out of the suite. Red, but with no assertion naming the behaviour -- scaffolding,
not a verdict (CLAUDE.md rule 10). The selftest now scripts the whole happy path
so what reddens is the assertion, and this harness reports CRASHED separately so
that distinction can never be silently re-lost.

  standards-sync-mutations.py          run them all
  standards-sync-mutations.py --dry    resolve anchors only
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "standards-sync.py"
SUITE = ROOT / "scripts" / "tests" / "standards-sync-selftest.py"

# Same reasoning as bugbot-gate-mutations.py: the baseline this measures against
# must be verifiable rather than assumed (backend#2441), and no __pycache__ may
# be left under scripts/tests/.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402


# (label, old, new, the selftest case that must redden)
MUTATIONS = [
    # --- (A) the author identity, which is the whole ticket -------------------
    (
        "pr create loses token= and reverts to the App installation identity",
        '''                        "--body", body,
                        # As the human, so the PR has an author Bugbot can see.
                        token=author_token)''',
        '''                        "--body", body)''',
        "runs as the author PAT",
    ),
    (
        "an empty PAT silently falls back to the App token",
        '''    if not author_token:
        return (f"{AUTHOR_TOKEN_ENV} is empty, so this PR could only be opened as the "
                "App, whose PRs Bugbot never reviews (backend#2590). Refusing to open it.")''',
        '''    if not author_token:
        author_token = None''',
        "an empty PAT opens NO PR",
    ),
    # --- (B) the half a careless fix breaks: backend#2036's org-scoped read ----
    # Appended at the END of the call, so the mutation is valid Python. See the
    # MALFORMED note in the docstring.
    (
        "the PAT leaks onto the fleet-read path, reverting backend#2036",
        '''                        "--jq", r'.[0] | select(.) | "\\(.number)\\t\\(.author.login)\\t\\(.author.is_bot)"')''',
        '''                        "--jq", r'.[0] | select(.) | "\\(.number)\\t\\(.author.login)\\t\\(.author.is_bot)"',
                        token=os.environ.get(AUTHOR_TOKEN_ENV) or None)''',
        "only PR creation changes identity",
    ),
    (
        # Bugbot on #348. The existing-PR path is the one that runs for every
        # sync PR already open, and all fourteen of those are bot-authored.
        # Dropping the check restores the exact defect: roles repaired, None
        # returned, repo reported ensured, PR still unreviewable for ever.
        "an existing BOT-authored PR is repaired and reported as ensured",
        '''        if is_bot != "false":''',
        '''        if False:''',
        "an existing BOT-authored PR is an ERROR",
    ),
    (
        # The other half: an author that could not be read must not fall through
        # to the happy path. `len(row) != 3` is what separates "GitHub said
        # human" from "we could not tell".
        #
        # THE MUTATION PADS TO EXACTLY THREE rather than deleting the branch,
        # and both halves of that are load-bearing:
        #
        #   * deleting it outright makes the tuple unpack raise, and a crash is
        #     not a catch (see the docstring) -- the harness would report
        #     CRASHED and name no assertion;
        #   * padding to the WRONG width is worse than useless. The first draft
        #     appended three defaults to a one-field row and sliced to 3, which
        #     yields `is_bot=""` -- still caught, but by the `is_bot != "false"`
        #     branch, so the mutation came back UNCAUGHT while looking like it
        #     had exercised the len guard.
        #
        # Padding to a literal `false` is the plausible wrong fix -- the one a
        # developer writes after seeing the unpack crash -- and it fails the way
        # the defect would: an unreadable row is read as a human author and the
        # repo is reported ensured.
        "an unreadable author row is padded into a false all-clear",
        '''        if len(row) != 3:''',
        '''        row = (row + ["", "false"])[:3] if len(row) < 3 else row
        if False:''',
        "an unreadable author fails closed",
    ),
    (
        # Bugbot on #348, the second round. The gate belongs in `main()`, but
        # `die()`ing there also skipped the read-only audit -- which
        # standards-sync.yml argues against in prose twenty lines above the
        # secret. Restoring the abort is the regression this pins.
        "a refused PAT aborts the run instead of disarming the writes",
        '''        if author_refusal:
            sys.stderr.write(f"::error::{author_refusal}\\n")''',
        '''        if author_refusal:
            die(author_refusal)
            sys.stderr.write(f"::error::{author_refusal}\\n")''',
        "a refused PAT still AUDITS the fleet",
    ),
    (
        # Bugbot (High) on the staging->main promotion, .github#363 /
        # backend#2735. The SAME condition as the refusal above, found later:
        # a token with the wrong fine-grained scopes, or never SSO-authorized,
        # passes every read-only identity check and fails at `pr create`. That
        # path used to `break`, so the fleet table covered only the repos
        # before the failure and read as a complete sweep of a smaller fleet.
        # Restoring the truncation is the regression this pins.
        "a mid-loop create failure truncates the audit instead of disarming",
        '''                                 "cannot open PRs anywhere"))
                    continue''',
        '''                                 "cannot open PRs anywhere"))
                    break''',
        "a create failure still AUDITS every target",
    ),
    (
        # The other direction, and the more dangerous one: keeping the report
        # but letting the writes through. The gate would then be pure
        # narration -- a run that says REMEDIATION DISABLED while pushing a
        # branch to every drifted repo.
        "the refusal is reported but the writes run anyway",
        '''    remediating = args.create_prs and not author_refusal''',
        '''    remediating = args.create_prs''',
        "a refused PAT pushes nothing",
    ),
    (
        # And the exit code, which is what the workflow's final step fails the
        # run from. Falling through to the `drifted` branch returns 0 under
        # --create-prs on the premise that every drifted repo now has a PR --
        # false here, and green.
        "a refused PAT exits 0 as though every drifted repo had a PR",
        '''    if unreadable or write_errors or author_refusal:''',
        '''    if unreadable or write_errors:''',
        "a refused PAT exits 2",
    ),
    # --- (C) the reviewer, whose absence deadlocks the PR ----------------------
    (
        "reviewer reverts to GITHUB_ACTOR, who is now the author",
        '''    code, _, err = gh("pr", "edit", pr_ref, "-R", full, "--add-reviewer",
                      SYNC_REVIEWER, token=author_token)''',
        '''    code, _, err = gh("pr", "edit", pr_ref, "-R", full, "--add-reviewer",
                      os.environ.get("GITHUB_ACTOR", "x"), token=author_token)''',
        "requests review from SYNC_REVIEWER",
    ),
    (
        # RE-AIMED (#348). This used to flip the SYNC_REVIEWER literal to the
        # PAT owner's login and expect a literal-vs-literal check to catch it.
        # That check is gone, and deliberately: the invariant is now enforced
        # at RUN TIME against the token's real owner, which means flipping the
        # literal is survivable -- `check_author_identity` refuses the run
        # instead. So the mutation that matters is removing the comparison, not
        # changing one of its operands.
        "the author-is-not-the-reviewer comparison is dropped",
        '''    if login.lower() == SYNC_REVIEWER.lower():''',
        '''    if False:''',
        "author == SYNC_REVIEWER is refused",
    ),
    (
        # The other half of the same gate, aimed at `author_login` rather than
        # at the `is None` branch: skipping that branch crashes on
        # `login.lower()`, which is red WITHOUT a verdict and tells the harness
        # nothing. Making the resolver return a value it could not resolve is
        # the same defect with a readable outcome -- the gate then compares a
        # login GitHub never confirmed and lets the run proceed.
        "an unresolvable token resolves to something anyway",
        '''    return login if code == 0 and login else None''',
        '''    return login or "unknown"''',
        "an unresolvable token is None, not a guess",
    ),
]


def drop_cache():
    try:
        cached = importlib.util.cache_from_source(str(GUARD))
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
    if out == src:
        return None
    # The compile gate. A mutation that cannot be imported is not evidence about
    # any assertion, in either direction.
    compile(out, str(GUARD), "exec")
    return out


def main():
    dry = "--dry" in sys.argv

    if not dry:
        rc = mutation_baseline.guard(ROOT, [GUARD])
        if rc:
            return rc

    pristine = GUARD.read_text(encoding="utf-8")
    stale, malformed, uncaught = [], [], []

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        for label, old, new, expect in MUTATIONS:
            try:
                mutated = apply_one(pristine, old, new)
            except LookupError as exc:
                stale.append((label, str(exc)))
                print("  STALE      %s" % label)
                continue
            except SyntaxError as exc:
                malformed.append((label, "%s (line %s)" % (exc.msg, exc.lineno)))
                print("  MALFORMED  %s" % label)
                continue
            if mutated is None:
                stale.append((label, "NO-OP: the mutation changed nothing"))
                print("  STALE      %s (no-op)" % label)
                continue
            if dry:
                print("  anchor ok  %s" % label)
                continue

            drop_cache()
            GUARD.write_text(mutated, encoding="utf-8")
            run = subprocess.run([sys.executable, "-B", str(SUITE)],
                                 capture_output=True, text=True, env=env, cwd=str(ROOT))
            caught = [line.replace("  FAIL: ", "")
                      for line in run.stdout.splitlines() if line.startswith("  FAIL:")]
            if run.returncode == 0:
                uncaught.append((label, "the suite passed with this broken"))
                print("  UNCAUGHT   %s" % label)
            elif not caught:
                uncaught.append((label, "red, but no assertion named it -- scaffolding, "
                                        "not a verdict (rule 10)"))
                print("  CRASHED    %s (red without a verdict)" % label)
            elif not any(expect in line for line in caught):
                uncaught.append((label, "reddened the wrong case(s): %s" % ", ".join(caught)[:90]))
                print("  WRONG      %s" % label)
            else:
                print("  caught     %s\n             by: %s" % (label, ", ".join(caught)[:110]))
    finally:
        GUARD.write_text(pristine, encoding="utf-8")
        drop_cache()

    if GUARD.read_text(encoding="utf-8") != pristine:
        sys.stderr.write("::error::%s was left mutated. Restore it from git.\n" % GUARD.name)
        return 2

    print("\n%d mutation(s): %d stale, %d malformed, %d uncaught"
          % (len(MUTATIONS), len(stale), len(malformed), len(uncaught)))
    for label, why in stale:
        sys.stderr.write("::error::STALE mutation `%s`: %s\n" % (label, why))
    for label, why in malformed:
        sys.stderr.write("::error::MALFORMED mutation `%s`: %s. Fix the mutation -- an "
                         "unimportable file proves nothing about the suite.\n" % (label, why))
    for label, why in uncaught:
        sys.stderr.write("::error::UNCAUGHT `%s`: %s. Add a case that fails under it, or "
                         "delete the mutation and say why it is not worth pinning.\n"
                         % (label, why))
    return 1 if (stale or malformed or uncaught) else 0


if __name__ == "__main__":
    raise SystemExit(main())
