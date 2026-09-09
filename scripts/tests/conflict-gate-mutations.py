#!/usr/bin/env python3
"""Mutation harness for the merge-conflict gate (tracebloc/backend#2637).

`conflict-gate-selftest.py` asserts the gate's behaviour; this asserts the
SELFTEST. Break a rule in `scripts/conflict-gate.py`, watch the suite redden,
restore. A case that stays green under its own rule being deleted is vacuous,
and a green log cannot tell you which of its 91 assertions are load-bearing.

THE MUTATION CALLS THE CODE UNDER TEST (CLAUDE.md rule 9). It edits
`scripts/conflict-gate.py` on disk and re-runs the real suite, which imports that
same file by path. There is no second copy of the rule in here -- the alternative
shape, re-implementing the check inline and mutating the copy, is
indistinguishable from real coverage in a log and has bitten this org twice.

EVERY ANCHOR MUST MATCH EXACTLY ONCE. An anchor matching twice mutates an
arbitrary one, so the run reports "uncaught" for the wrong reason; an anchor
matching zero times is stale and fails the run exactly like an uncaught
mutation. That is the assertion that the anchor ACTUALLY APPLIED -- an inert
mutation and good coverage look identical in a log otherwise. `--dry` resolves
every anchor without running the suite, which is what belongs in the fast tier.

  conflict-gate-mutations.py          run them all
  conflict-gate-mutations.py --dry    resolve anchors only

THE BYTECODE CACHE IS DISARMED for the reason bugbot-gate-mutations.py documents
at length: a pyc is revalidated on (mtime-to-the-second, byte size), several
mutations below change the file by the same number of bytes, and back-to-back
runs inside one second would otherwise execute the PREVIOUS mutation's bytecode
-- reporting a caught mutation as uncaught with nothing in the log to say so.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "conflict-gate.py"
WORKFLOW = ROOT / ".github" / "workflows" / "conflict-gate.yml"
SUITE = ROOT / "scripts" / "tests" / "conflict-gate-selftest.py"

# TWO TARGETS, NOT ONE. The script being correct and something ACTUALLY RUNNING it
# are separate claims, and the second lives in YAML. Rule 5 does not exempt a
# guarantee for being written in a different language -- if the workflow stops
# invoking the gate, or acquires a `pull_request` trigger that cannot fire on the
# PRs it targets, the suite must redden. So the harness mutates both files, and
# each mutation declares which one it edits.
TARGETS = {"gate": GATE, "workflow": WORKFLOW}

# See scripts/tests/mutation_baseline.py: the `finally` below restores the file on
# a crash but cannot after SIGKILL, a runner timeout, or a second harness racing
# this one -- and a mutation left on disk becomes the NEXT run's `pristine`.
#
# dont_write_bytecode BEFORE the import: `selftests-cover` rejects anything under
# scripts/tests/ that is not a suite or a runner, and a `__pycache__/` is exactly
# that.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402


# (label, old, new)
MUTATIONS = [
    # --- (A) the load-bearing claim: affirmative conflict evidence wins -----
    #
    # The cheapest way to get this file wrong. A conflicted PR presents
    # `mergeable=CONFLICTING` with `mergeStateStatus=UNKNOWN` for the first
    # moments after its base moves, so an "any UNKNOWN wins" rule drops exactly
    # the finding the gate exists to raise -- and passes every other case.
    ("an UNKNOWN in either field outranks affirmative conflict evidence",
     '    for field, value in CONFLICT_EVIDENCE:',
     '    if "UNKNOWN" in (mergeable, state):\n'
     '        return UNDETERMINED, "unknown wins"\n'
     '    for field, value in CONFLICT_EVIDENCE:'),

    # Only ONE of the two fields consulted. `mergeStateStatus=DIRTY` with
    # `mergeable` not yet computed is a real payload, and this drops it.
    ("only `mergeable` is consulted, so a DIRTY-only payload reads clean",
     'CONFLICT_EVIDENCE = (("mergeable", "CONFLICTING"), ("mergeStateStatus", "DIRTY"))',
     'CONFLICT_EVIDENCE = (("mergeable", "CONFLICTING"),)'),

    # --- (B) fail-closed: "cannot tell" must not become "fine" --------------
    ("a value GitHub never declared is treated as CLEAR",
     '        if not isinstance(value, str) or value not in domain:\n'
     '            return UNDETERMINED, (',
     '        if not isinstance(value, str) or value not in domain:\n'
     '            return CLEAR, ('),

    ("a field absent from the payload is treated as CLEAR",
     '        if value is None:\n'
     '            return UNDETERMINED, f"{field} absent from the PR payload"',
     '        if value is None:\n'
     '            return CLEAR, f"{field} absent from the PR payload"'),

    ("an undetermined mergeability writes a SUCCESS status",
     '    UNDETERMINED: "pending",',
     '    UNDETERMINED: "success",'),

    ("an undetermined PR stops being a run-level error",
     '        if st["verdict"] == UNDETERMINED:\n            errors.append(',
     '        if False and st["verdict"] == UNDETERMINED:\n            errors.append('),

    # --- (C) the status states themselves ----------------------------------
    ("a conflicted PR is marked SUCCESS",
     '    CONFLICTED: "failure",',
     '    CONFLICTED: "success",'),

    # The half that makes the context requireable. If a healthy PR stops getting
    # `success`, requiring this context bricks every clean PR at
    # "Expected -- waiting" -- the exact failure bricked-prs.py hunts.
    ("a healthy PR stops getting a clearing SUCCESS status",
     '    CLEAR: "success",',
     '    CLEAR: "pending",'),

    # A renamed context does not stop being required, it stops being REPORTED.
    ("the status context is renamed, which would brick every PR requiring it",
     'CONTEXT = "conflict-gate / mergeable"',
     'CONTEXT = "conflict-gate / conflicts"'),

    # --- (D) coverage of the PR set ----------------------------------------
    ("drafts are skipped, so a draft has no status when it turns ready",
     '    out = []\n    for pr in prs:\n        verdict, why = classify(pr)',
     '    out = []\n    for pr in prs:\n        if pr.get("isDraft"):\n'
     '            continue\n        verdict, why = classify(pr)'),

    ("a PR list at the truncation cap is swept partially and called clean",
     '    if len(prs) >= PR_LIST_LIMIT:',
     '    if False and len(prs) >= PR_LIST_LIMIT:'),

    # --- (E) unreadable is not clean ---------------------------------------
    ("an unreadable PR list reports the repo as having no PRs",
     '        return [], [f"{name}: PR list unreadable ({exc.detail})"]',
     '        return [], []'),

    ("a check run that failed to write is swallowed, so the PR stays empty-green",
     "            errors.append(\n"
     "                f\"{name}#{st['number']}: could not write the {st['state']} check run \"",
     "            _swallowed = (\n"
     "                f\"{name}#{st['number']}: could not write the {st['state']} check run \""),

    ("a PR with no head sha is skipped silently",
     '            errors.append(f"{name}#{st[\'number\']}: no head sha, so no check run could be written")',
     '            pass'),

    # --- (F) the exit-code ranking -----------------------------------------
    #
    # If an error stops outranking a clean sweep, a partially-read fleet reports
    # "nothing conflicted" and exits 0 -- the fail-open, in the gate.
    ("an un-evaluated PR no longer outranks a clean sweep",
     '    if errors:\n        return 2\n'
     '    return 1 if any(s["verdict"] == CONFLICTED for s in statuses) else 0',
     '    return 1 if any(s["verdict"] == CONFLICTED for s in statuses) else 0'),

    # --- (F2) the 1000-statuses-per-sha-and-context cap ---------------------
    #
    # Without the dedup a PR open three weeks exhausts the cap at 48 writes a day
    # and every later write 422s -- the gate going silent on the stalest PRs.
    ("the unchanged-status skip is removed, so every sweep burns a write",
     '        if st["existing"] == st["state"]:',
     '        if False and st["existing"] == st["state"]:'),

    # The inverse is worse: it writes only when nothing would change, so a
    # resolved conflict never gets cleared and a new one is never reported.
    ("the skip is inverted, so the status is only ever written when identical",
     '        if st["existing"] == st["state"]:',
     '        if st["existing"] != st["state"]:'),

    # The check-runs REST endpoint answers lower case. Unfolded, an upper-case
    # conclusion never matches and the dedup silently does nothing -- invisible,
    # because everything still works, it just writes every time.
    ("the case fold goes, so an upper-case state never matches",
     '    state = check_run_state(run)\n'
     '    return state.lower() if isinstance(state, str) else None',
     '    state = check_run_state(run)\n'
     '    return state if isinstance(state, str) else None'),

    ("_latest_own_run matches ANY check-run name, so another check's state is read as ours",
     '            if isinstance(run, dict) and run.get("name") == CONTEXT]',
     '            if isinstance(run, dict)]'),

    # THE PERMISSION CLASS THIS GATE TURNS ON (backend#3242). Reading the current
    # verdict from ANY commit-status source -- the rollup (which resolves
    # `commit.status` in GraphQL, .github#359) or the combined-status REST
    # endpoint -- needs `statuses: read`, the scope the App does not hold and the
    # whole reason the commit-status design never ran. The read must stay on
    # check-runs, so a mutation back to a status source must redden.
    ("the current state is read from the rollup again, not the check-runs endpoint",
     '        listing = CD.gh_json(["api", "--method", "GET",\n'
     '                              f"repos/{org}/{name}/commits/{sha}/check-runs",\n'
     '                              "-f", f"check_name={CONTEXT}",\n'
     '                              "-f", "filter=latest"])',
     '        listing = CD.gh_json(["pr", "view", sha, "--json", "statusCheckRollup"])'),

    # `gh api` POSTs the instant any `-f` is passed, and there is no POST route on
    # check-runs, so dropping `--method GET` 404s the read on EVERY call:
    # _latest_own_run returns None forever, the dedup dies, and post_status only
    # ever CREATEs (the accumulation bug 97436c5 fixed, re-armed). The method is
    # part of the contract, not decoration (@saadqbal on #446).
    ("the read drops --method GET, so gh api POSTs it and the read 404s",
     '        listing = CD.gh_json(["api", "--method", "GET",\n'
     '                              f"repos/{org}/{name}/commits/{sha}/check-runs",',
     '        listing = CD.gh_json(["api",\n'
     '                              f"repos/{org}/{name}/commits/{sha}/check-runs",'),

    # An unreadable current state must produce a WRITE. Turning it into a skip
    # would silently stop reporting whenever the check-runs read flakes.
    # _latest_own_run returns a run dict or None, so the mutation returns a run
    # dict (not a bare string, which would AttributeError in check_run_state and
    # score UNCAUGHT). A completed-success run makes existing_state read "success"
    # instead of None on an unreadable head, turning the mandatory write into a skip.
    ("an unreadable current state is treated as agreeing, so no check run is written",
     '    except CD.GhError:\n        return None\n    if not isinstance(listing, dict):',
     '    except CD.GhError:\n        return {"status": "completed", "conclusion": "success"}\n    if not isinstance(listing, dict):'),

    # --- (G) the retry loop -------------------------------------------------
    ("every PR is re-read, not only the ones GitHub would not answer",
     '        pending = [i for i, pr in enumerate(resolved)\n'
     '                   if classify(pr)[0] == UNDETERMINED]',
     '        pending = [i for i, pr in enumerate(resolved)]'),

    ("the retry sleeps for real instead of through the injected sleeper",
     '        sleeper(sleep_for)',
     '        time.sleep(sleep_for)'),
]

# --- (H) THE WORKFLOW, which is where "something runs this" is declared -----
WORKFLOW_MUTATIONS = [
    # The whole gate, disarmed by one line. Everything else in the suite would
    # still pass: the script stays perfect and nothing invokes it.
    ("the workflow stops invoking the gate",
     '        run: python3 scripts/conflict-gate.py',
     '        run: echo skipped'),

    # THE REGRESSION MOST LIKELY TO BE MADE IN GOOD FAITH. Someone asks "why
    # doesn't this run on PRs?", adds the trigger, and the gate is now inert on
    # exactly the conflicted PRs it exists for -- while looking more thorough.
    ("the workflow acquires a pull_request trigger it cannot be dispatched by",
     '  workflow_dispatch: {}',
     '  pull_request:\n  workflow_dispatch: {}'),

    # THE SCHEDULE IS PAUSED (2026-09-09, backend#3468) because the App's
    # installation grants neither `statuses` nor `checks`, and every scheduled run
    # 422'd at the mint -- run 34333719757 included, made after the mint moved to
    # `permission-checks: write`. The suite pins the pause from both sides, so
    # both regressions are mutated here: re-arming the cron before the permission
    # exists, and deleting the commented block so the pause quietly becomes a
    # removal.
    ("the schedule is re-armed while the App still cannot write check runs",
     '  # schedule:\n  #   - cron: "*/30 * * * *"',
     '  schedule:\n    - cron: "*/30 * * * *"'),

    ("the paused schedule is deleted outright instead of kept for re-arming",
     '  # schedule:\n  #   - cron: "*/30 * * * *"\n',
     ''),

    ("the sweep becomes cancellable, leaving half its statuses stale",
     '  cancel-in-progress: false',
     '  cancel-in-progress: true'),

    # Without checks:write every sweep finds conflicts it cannot report: a green
    # run, no red row, and the fail-open perfectly intact.
    ("the mint loses checks: write, so no finding can ever reach a PR",
     '          permission-checks: write',
     '          permission-checks: read'),

    # The class regression: someone re-adds `statuses`, the scope the App's
    # installation does not grant -- reintroducing the mint refused fleet-wide
    # (backend#3242).
    ("the mint re-adds statuses: write, the scope the App cannot grant",
     '          permission-pull-requests: read\n          permission-checks: write',
     '          permission-pull-requests: read\n          permission-checks: write\n'
     '          permission-statuses: write'),
]

# One flat list of (target, label, old, new). Derived from the two lists rather
# than hand-written a third time.
ALL_MUTATIONS = ([("gate", *m) for m in MUTATIONS]
                 + [("workflow", *m) for m in WORKFLOW_MUTATIONS])


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

    # Refuse rather than measure against a baseline nothing vouches for. Only the
    # writing path: `--dry` writes nothing, so it has no restore to lose -- and it
    # is what `make check` runs on every push, where refusing on an uncommitted
    # edit would block the pre-push tier for whoever is editing the target.
    if not dry:
        # BOTH targets, or a mutation left in the workflow by a killed run becomes
        # the next run's premise just as silently as one left in the script.
        rc = mutation_baseline.guard(ROOT, list(TARGETS.values()))
        if rc:
            return rc

    pristine_by_target = {
        name: path.read_text(encoding="utf-8") for name, path in TARGETS.items()
    }
    stale, uncaught = [], []

    for target, label, old, new in ALL_MUTATIONS:
        path = TARGETS[target]
        pristine = pristine_by_target[target]
        try:
            mutated = apply_one(pristine, old, new)
        except LookupError as exc:
            stale.append((label, str(exc)))
            continue
        if mutated is None:
            stale.append((label, "NO-OP: the mutation changed nothing"))
            continue
        if dry:
            print("  anchor ok  [%s] %s" % (target, label))
            continue
        path.write_text(mutated, encoding="utf-8")
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
            path.write_text(pristine, encoding="utf-8")
            _drop_bytecode_cache()
        caught = [
            line.strip()[6:].strip()
            for line in run.stdout.splitlines()
            if line.strip().startswith("FAIL:")
        ]
        # A crash counts as caught ONLY if the suite actually ran and reported; a
        # bare traceback with no assertion output means the mutation broke the
        # harness rather than being detected by a case, which is not coverage.
        reported = "conflict-gate-selftest:" in run.stdout
        shown = "[%s] %s" % (target, label)
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (shown, ", ".join(caught)[:120]))
        elif not reported:
            uncaught.append((shown, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % shown)
        else:
            uncaught.append((shown, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % shown)

    for name, path in TARGETS.items():
        if path.read_text(encoding="utf-8") != pristine_by_target[name]:
            sys.stderr.write(
                "::error::%s was left mutated. Restore it from git.\n" % path.name)
            return 2

    print("\n%d mutation(s) across %d file(s): %d stale, %d uncaught"
          % (len(ALL_MUTATIONS), len(TARGETS), len(stale), len(uncaught)))
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
