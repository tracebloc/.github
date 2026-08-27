#!/usr/bin/env python3
"""Selftest for the merge-conflict gate (tracebloc/backend#2637).

HERMETIC: every case builds a PR payload by hand and hands it to the real
functions in `scripts/conflict-gate.py`. No token, no network, no `gh`. The two
seams that would touch the network -- `open_prs` and `post_status` -- are
exercised through monkeypatched stand-ins, so the unreadable-list and
status-did-not-land paths are covered by this suite rather than being the parts
nobody tests.

THE INPUT DOMAIN IS DERIVED, THE EXPECTATIONS ARE NOT (CLAUDE.md rules 6 and 9).

`test_every_declared_pair` walks the FULL CROSS PRODUCT of GitHub's two
mergeability enums -- 3 x 7 = 21 pairs -- taken from the gate's own
`MERGEABLE_STATES` / `MERGE_STATE_STATUSES`, because a hand-listed set of "the
cases I thought of" cannot see the pair I did not think of, and mutation coverage
cannot see a vocabulary gap either. That is the one thing iterated from the
module.

Every EXPECTATION is written down here as a literal instead: the expected verdict
per pair, the three status states, and the context string. Iterating the module's
own `STATE_FOR` to check `STATE_FOR` would be self-consistent and therefore
blind -- typo one and the fixture carries the typo too, and passes.

THE LOAD-BEARING CASE is `mergeable=CONFLICTING, mergeStateStatus=UNKNOWN`. That
is the shape a conflicted PR actually presents moments after its base moves, and
it is the one where an "unknown wins" rule would silently drop the finding the
whole file exists to raise.
"""
import importlib.util
import io
import contextlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

# NO BYTECODE. `exec_module` would write `scripts/__pycache__`, which
# `selftests-cover` correctly rejects -- and the mutation harness rewrites the
# gate many times per second with same-length edits, so a stale pyc would serve
# one mutation's bytecode to the next run and report a CAUGHT mutation as
# uncaught. Same reasoning as bugbot-gate-selftest.py, same fix.
sys.dont_write_bytecode = True

SPEC = importlib.util.spec_from_file_location(
    "conflict_gate", ROOT / "scripts" / "conflict-gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

FAILURES = []
COUNT = 0


def check(label, condition, detail=""):
    global COUNT
    COUNT += 1
    if not condition:
        FAILURES.append("%s%s" % (label, (" -- " + detail) if detail else ""))


def pr(mergeable="MERGEABLE", state="CLEAN", number=1, sha="deadbeef",
       draft=False, title="a change", base="develop"):
    return {
        "number": number,
        "title": title,
        "isDraft": draft,
        "mergeable": mergeable,
        "mergeStateStatus": state,
        "headRefOid": sha,
        "baseRefName": base,
        "url": "https://github.com/tracebloc/x/pull/%d" % number,
    }


# --- the literal expectation table, written independently of the matcher ------
#
# One entry per member of MergeableState. The value is the verdict expected for
# EVERY MergeStateStatus paired with it, except where `mergeStateStatus` is
# itself affirmative conflict evidence (DIRTY), which overrides.
#
# Spelled out rather than computed: this is the requirement, and a table derived
# from the gate's own conditions would agree with a broken gate.
EXPECTED_BY_MERGEABLE = {
    "CONFLICTING": "conflicted",   # affirmative: there ARE conflicts
    "MERGEABLE": "clear",          # affirmative: there are NOT
    "UNKNOWN": "undetermined",     # GitHub has not answered
}
# DIRTY says "conflicts" on the other field, so it wins wherever `mergeable` is
# not itself affirmative-clean... and even there the two cannot both be true, so
# a MERGEABLE+DIRTY payload is contradictory and treated as conflicted. Stated
# explicitly because it is the one pair the row above does not cover.
DIRTY_OVERRIDES_TO = "conflicted"

# --- (1) every declared pair, the derived input domain ------------------------
seen_verdicts = set()
for m in sorted(gate.MERGEABLE_STATES):
    for s in sorted(gate.MERGE_STATE_STATUSES):
        want = DIRTY_OVERRIDES_TO if s == "DIRTY" else EXPECTED_BY_MERGEABLE[m]
        got, why = gate.classify(pr(mergeable=m, state=s))
        seen_verdicts.add(got)
        check("classify(mergeable=%s, mergeStateStatus=%s)" % (m, s),
              got == want, "want %r, got %r (%s)" % (want, got, why))

check("the 21 declared pairs exercise all three verdicts",
      seen_verdicts == {"conflicted", "clear", "undetermined"},
      "got %r" % (sorted(seen_verdicts),))

# The count is asserted so a shrunken domain cannot quietly reduce coverage.
check("the cross product really is 3 x 7",
      len(gate.MERGEABLE_STATES) * len(gate.MERGE_STATE_STATUSES) == 21,
      "got %d x %d" % (len(gate.MERGEABLE_STATES), len(gate.MERGE_STATE_STATUSES)))

# --- (2) THE LOAD-BEARING CASE -----------------------------------------------
#
# A conflict with the other field still UNKNOWN. An "any UNKNOWN wins" rule
# passes every other case in this file and drops exactly this one.
_v, _why = gate.classify(pr(mergeable="CONFLICTING", state="UNKNOWN"))
check("a CONFLICTING PR whose mergeStateStatus is still UNKNOWN is CONFLICTED",
      _v == "conflicted", "got %r (%s)" % (_v, _why))
_v, _why = gate.classify(pr(mergeable="UNKNOWN", state="DIRTY"))
check("a DIRTY PR whose mergeable is still UNKNOWN is CONFLICTED",
      _v == "conflicted", "got %r (%s)" % (_v, _why))

# --- (3) a clean PR is not judged unclean by its OTHER blockers ---------------
#
# BLOCKED is the ordinary state of an unconflicted PR still awaiting its review.
# Keying CLEAR on `mergeStateStatus == CLEAN` would paint most of the fleet red,
# which is the always-red check that trains people to ignore the tier.
for s in ("BLOCKED", "BEHIND", "UNSTABLE", "HAS_HOOKS", "CLEAN"):
    _v, _why = gate.classify(pr(mergeable="MERGEABLE", state=s))
    check("MERGEABLE + %s is CLEAR, not a conflict" % s,
          _v == "clear", "got %r (%s)" % (_v, _why))

# --- (4) vocabulary drift and absent fields are findings, not health ----------
for label, payload in (
    ("a mergeable value GitHub never declared", pr(mergeable="SOMETHING_NEW")),
    ("a mergeStateStatus value GitHub never declared", pr(state="SOMETHING_NEW")),
    ("mergeable absent", {"number": 1, "mergeStateStatus": "CLEAN", "headRefOid": "a"}),
    ("mergeStateStatus absent", {"number": 1, "mergeable": "MERGEABLE", "headRefOid": "a"}),
    ("mergeable is not a string", pr(mergeable=42)),
    ("an empty payload", {}),
):
    _v, _why = gate.classify(payload)
    check("%s is UNDETERMINED" % label, _v == "undetermined",
          "got %r (%s)" % (_v, _why))
    # It must SAY WHY, not merely refuse -- an unexplained `pending` is a brick
    # nobody can act on.
    check("%s explains itself" % label, isinstance(_why, str) and len(_why) > 10,
          "got %r" % (_why,))

# A novel value must be named in the reason, so the next reader knows which enum
# to re-derive rather than guessing.
_v, _why = gate.classify(pr(mergeable="SOMETHING_NEW"))
check("the drift reason names the offending value",
      "SOMETHING_NEW" in _why, "got %r" % (_why,))

# --- (5) the verdict -> status-state mapping, as literals ---------------------
check("conflicted maps to the failure state", gate.STATE_FOR["conflicted"] == "failure",
      "got %r" % (gate.STATE_FOR["conflicted"],))
check("clear maps to the success state", gate.STATE_FOR["clear"] == "success",
      "got %r" % (gate.STATE_FOR["clear"],))
# `pending` and NOT `failure`: it blocks a merge just the same if required, but
# does not assert a conflict that was never observed.
check("undetermined maps to pending, not failure",
      gate.STATE_FOR["undetermined"] == "pending",
      "got %r" % (gate.STATE_FOR["undetermined"],))
check("every verdict has a state", set(gate.STATE_FOR) == {
    "conflicted", "clear", "undetermined"}, "got %r" % (sorted(gate.STATE_FOR),))
check("every verdict has a description", set(gate.DESCRIPTION_FOR) == {
    "conflicted", "clear", "undetermined"},
    "got %r" % (sorted(gate.DESCRIPTION_FOR),))

# The context string is what branch protection would name. If it ever changes,
# every PR requiring it bricks -- so it is pinned here as a literal.
check("the status context is the pinned string",
      gate.CONTEXT == "conflict-gate / mergeable", "got %r" % (gate.CONTEXT,))

# --- (6) a healthy PR still gets a status ------------------------------------
#
# The half that makes the context requireable at all. A gate that only speaks
# when something is wrong leaves a healthy PR at "Expected -- waiting" forever,
# which is the brick `bricked-prs.py` exists to hunt.
_plan = gate.plan([pr(number=7, mergeable="MERGEABLE", state="CLEAN")])
check("a clear PR is planned a success status, not skipped",
      len(_plan) == 1 and _plan[0]["state"] == "success",
      "got %r" % (_plan,))

# --- (7) drafts are swept too ------------------------------------------------
#
# NOTHING BELOW INDEXES A LIST A MUTATION COULD EMPTY. `_plan[0]` on an empty
# plan raises IndexError, which crashes the suite instead of reporting a FAIL --
# and the mutation harness scores a crash as "broke the harness", NOT as caught,
# because a traceback proves nothing about coverage. Both draft mutations landed
# here first and were correctly refused as non-coverage until this was `.get()`.
_plan = gate.plan([pr(number=8, mergeable="CONFLICTING", draft=True)])
check("a draft is swept, not skipped", len(_plan) == 1, "got %r" % (_plan,))
_first = _plan[0] if _plan else {}
check("a swept draft is still classified conflicted",
      _first.get("verdict") == "conflicted", "got %r" % (_first,))
check("the plan records draftness", _first.get("isDraft") is True,
      "got %r" % (_first.get("isDraft"),))

# --- (8) plan() carries the head sha through, or nothing can be written ------
_plan = gate.plan([pr(number=9, sha="cafe1234")])
_first = _plan[0] if _plan else {}
check("the plan targets the PR's head sha", _first.get("sha") == "cafe1234",
      "got %r" % (_first.get("sha"),))

# --- (9) retries: an UNKNOWN is re-read, an answered PR is not ---------------
_calls = []


def _fake_reread_resolves(org, name, number):
    _calls.append(number)
    return pr(number=number, mergeable="CONFLICTING", state="DIRTY")


_real_reread = gate.reread
_slept = []
try:
    gate.reread = _fake_reread_resolves
    out = gate.resolve_undetermined(
        "tracebloc", "x",
        [pr(number=1, mergeable="UNKNOWN", state="UNKNOWN"),
         pr(number=2, mergeable="MERGEABLE", state="CLEAN")],
        retries=3, sleep_for=0.0, sleeper=_slept.append)
    check("an UNKNOWN PR is re-read", 1 in _calls, "calls %r" % (_calls,))
    check("a PR GitHub already answered is NOT re-read", 2 not in _calls,
          "calls %r" % (_calls,))
    check("the re-read result replaces the stale payload",
          gate.classify(out[0])[0] == "conflicted",
          "got %r" % (gate.classify(out[0]),))
    check("the retry loop stops once nothing is UNKNOWN", len(_calls) == 1,
          "calls %r" % (_calls,))

    # The suite must not spend real seconds; assert the injected sleeper was the
    # one used, or a future `time.sleep` would make this suite slow and nobody
    # would know why.
    check("the retry slept through the injected sleeper", _slept == [0.0],
          "got %r" % (_slept,))

    # AN UNRESOLVABLE UNKNOWN STAYS UNDETERMINED. The retry is a courtesy, not a
    # way to eventually call it clean.
    _calls.clear()
    gate.reread = lambda org, name, number: None  # the re-read itself failed
    out = gate.resolve_undetermined(
        "tracebloc", "x", [pr(number=3, mergeable="UNKNOWN", state="UNKNOWN")],
        retries=2, sleep_for=0.0, sleeper=lambda _s: None)
    check("a PR that stays UNKNOWN after every retry is still UNDETERMINED",
          gate.classify(out[0])[0] == "undetermined",
          "got %r" % (gate.classify(out[0]),))

    # retries=0 must not sleep or call at all.
    _calls.clear()
    _slept2 = []
    gate.reread = _fake_reread_resolves
    gate.resolve_undetermined(
        "tracebloc", "x", [pr(number=4, mergeable="UNKNOWN", state="UNKNOWN")],
        retries=0, sleep_for=9.0, sleeper=_slept2.append)
    check("retries=0 re-reads nothing and sleeps not at all",
          _calls == [] and _slept2 == [], "calls %r slept %r" % (_calls, _slept2))
finally:
    gate.reread = _real_reread

# --- (10) an unreadable PR list is an ERROR, never a clean repo --------------
_real_open = gate.open_prs
try:
    def _boom(org, name):
        raise gate.CD.GhError(502, "upstream exploded")

    gate.open_prs = _boom
    statuses, errors = gate.sweep_repo("tracebloc", "x", retries=0,
                                       sleep_for=0.0, dry_run=True)
    check("an unreadable PR list yields no statuses", statuses == [],
          "got %r" % (statuses,))
    check("an unreadable PR list IS an error", len(errors) == 1,
          "got %r" % (errors,))
    # `.join` rather than `errors[0]`: a mutation that returns no error at all
    # would make the index crash the suite, and the harness scores a crash as
    # broke-the-harness rather than as caught.
    check("the error names the repo and the cause",
          "x" in " ".join(errors) and "upstream exploded" in " ".join(errors),
          "got %r" % (errors,))
finally:
    gate.open_prs = _real_open

# --- (11) a status that did not land is an error -----------------------------
#
# The failure this file exists to fix, re-armed one level up: the PR still reads
# empty-green and nothing said so.
_real_post = gate.post_status
try:
    gate.open_prs = lambda org, name: [pr(number=11, mergeable="CONFLICTING",
                                          state="DIRTY")]

    def _post_fails(org, name, sha, state, description, target_url):
        raise gate.CD.GhError(403, "resource not accessible")

    gate.post_status = _post_fails
    statuses, errors = gate.sweep_repo("tracebloc", "x", retries=0,
                                       sleep_for=0.0, dry_run=False)
    check("a status write that failed is recorded as unwritten",
          statuses and statuses[0]["written"] is False,
          "got %r" % (statuses,))
    check("a status write that failed IS an error",
          any("could not write" in e for e in errors), "got %r" % (errors,))
    check("the write error names the state it tried to set",
          any("failure" in e for e in errors), "got %r" % (errors,))

    # And the happy path: the sha, state and context actually reach the API layer.
    seen = {}

    def _post_ok(org, name, sha, state, description, target_url):
        seen.update(sha=sha, state=state, description=description)

    gate.post_status = _post_ok
    statuses, errors = gate.sweep_repo("tracebloc", "x", retries=0,
                                       sleep_for=0.0, dry_run=False)
    check("a conflicted PR gets a failure status on its own head sha",
          seen.get("state") == "failure" and seen.get("sha") == "deadbeef",
          "got %r" % (seen,))
    check("the description names the real cause",
          "no pull_request check ran" in (seen.get("description") or "").lower()
          or "merge conflict" in (seen.get("description") or "").lower(),
          "got %r" % (seen.get("description"),))
    check("a successful write is recorded as written",
          statuses and statuses[0]["written"] is True, "got %r" % (statuses,))
    check("a successful write of a conflict is not an error", errors == [],
          "got %r" % (errors,))

    # --- (12) --dry-run writes NOTHING -------------------------------------
    seen.clear()
    statuses, errors = gate.sweep_repo("tracebloc", "x", retries=0,
                                       sleep_for=0.0, dry_run=True)
    check("--dry-run writes no status at all", seen == {}, "got %r" % (seen,))
    check("--dry-run still classifies",
          bool(statuses) and statuses[0]["verdict"] == "conflicted",
          "got %r" % (statuses,))

    # --- (13) an UNDETERMINED PR is an error AND gets a pending status ------
    gate.open_prs = lambda org, name: [pr(number=13, mergeable="UNKNOWN",
                                          state="UNKNOWN")]
    seen.clear()
    gate.post_status = _post_ok
    statuses, errors = gate.sweep_repo("tracebloc", "x", retries=0,
                                       sleep_for=0.0, dry_run=False)
    check("an undetermined PR gets a pending status",
          seen.get("state") == "pending", "got %r" % (seen,))
    check("an undetermined PR is ALSO a run-level error",
          any("NOT judged" in e for e in errors), "got %r" % (errors,))

    # --- (13b) AN UNCHANGED STATUS IS NOT REWRITTEN --------------------------
    #
    # GitHub caps statuses at 1000 per sha AND context. A 30-minute sweep is 48
    # writes a day onto an unchanged head, so a PR left open three weeks would
    # exhaust the cap and every later write would 422 -- the gate going silent on
    # exactly the stalest PRs. So a write happens only when it changes something.
    def with_rollup(entries, **kw):
        p = pr(**kw)
        p["statusCheckRollup"] = entries
        return p

    ours = gate.CONTEXT

    # The case fold is load-bearing: GraphQL reports `SUCCESS`, the Statuses API
    # takes `success`. Comparing unfolded makes every status look changed.
    check("existing_state folds GraphQL's upper case to the API's lower",
          gate.existing_state(with_rollup([{"context": ours, "state": "SUCCESS"}]))
          == "success",
          "got %r" % (gate.existing_state(
              with_rollup([{"context": ours, "state": "SUCCESS"}])),))
    check("existing_state is None when our context is absent",
          gate.existing_state(with_rollup([{"context": "other", "state": "SUCCESS"}]))
          is None,
          "got %r" % (gate.existing_state(
              with_rollup([{"context": "other", "state": "SUCCESS"}])),))
    check("existing_state is None on a rollup-less payload",
          gate.existing_state(pr()) is None,
          "got %r" % (gate.existing_state(pr()),))

    # unchanged -> no write
    gate.open_prs = lambda org, name: [with_rollup(
        [{"context": ours, "state": "FAILURE"}],
        number=131, mergeable="CONFLICTING", state="DIRTY")]
    seen.clear()
    statuses, errors = gate.sweep_repo("tracebloc", "x", retries=0,
                                       sleep_for=0.0, dry_run=False)
    check("a status already saying the same thing is NOT rewritten", seen == {},
          "got %r" % (seen,))
    check("an unrewritten status is marked unchanged",
          bool(statuses) and statuses[0].get("unchanged") is True,
          "got %r" % (statuses,))
    check("skipping an unchanged write is not an error", errors == [],
          "got %r" % (errors,))
    # ...and the verdict is still reported, so the run still exits 1.
    check("an unchanged conflicted PR is still a conflict finding",
          bool(statuses) and statuses[0]["verdict"] == "conflicted",
          "got %r" % (statuses,))

    # CHANGED -> write. A conflict that has just been resolved must be cleared,
    # which is the direction that matters most: a stale `failure` left on a fixed
    # PR blocks it for no reason.
    gate.open_prs = lambda org, name: [with_rollup(
        [{"context": ours, "state": "FAILURE"}],
        number=132, mergeable="MERGEABLE", state="CLEAN")]
    seen.clear()
    statuses, errors = gate.sweep_repo("tracebloc", "x", retries=0,
                                       sleep_for=0.0, dry_run=False)
    check("a resolved conflict overwrites the stale failure with success",
          seen.get("state") == "success", "got %r" % (seen,))
    check("a changed write is recorded as written",
          bool(statuses) and statuses[0]["written"] is True, "got %r" % (statuses,))

    # No status yet -> write. `None` must not compare equal to any state.
    gate.open_prs = lambda org, name: [with_rollup(
        [], number=133, mergeable="CONFLICTING", state="DIRTY")]
    seen.clear()
    statuses, errors = gate.sweep_repo("tracebloc", "x", retries=0,
                                       sleep_for=0.0, dry_run=False)
    check("a head with no status of ours yet gets one written",
          seen.get("state") == "failure", "got %r" % (seen,))

    # --- (14) a PR with no head sha cannot be written, and says so ----------
    gate.open_prs = lambda org, name: [
        {"number": 14, "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}]
    seen.clear()
    statuses, errors = gate.sweep_repo("tracebloc", "x", retries=0,
                                       sleep_for=0.0, dry_run=False)
    check("a PR with no head sha writes no status", seen == {}, "got %r" % (seen,))
    check("a PR with no head sha IS an error",
          any("no head sha" in e for e in errors), "got %r" % (errors,))
finally:
    gate.post_status = _real_post
    gate.open_prs = _real_open

# --- (15) exit codes ---------------------------------------------------------
#
# The ranking is load-bearing: an unevaluable PR must outrank a clean sweep, or a
# partially-read fleet reports "nothing conflicted".
_real_open = gate.open_prs
_real_post = gate.post_status
_real_inv = gate.CD.load_inventory
try:
    gate.CD.load_inventory = lambda path: {"repos": {"x": {}}}
    gate.post_status = lambda *a, **k: None

    def run(prs):
        gate.open_prs = lambda org, name: prs
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = gate.main(["--retries", "0"])
        return code, buf.getvalue()

    code, out = run([pr(number=1, mergeable="MERGEABLE", state="CLEAN")])
    check("a clean sweep exits 0", code == 0, "got %d: %s" % (code, out))
    check("a clean sweep says so", "No open PR is conflicted" in out,
          "got %r" % (out,))

    code, out = run([pr(number=2, mergeable="CONFLICTING", state="DIRTY")])
    check("a conflicted PR exits 1", code == 1, "got %d: %s" % (code, out))
    check("a conflicted PR is NAMED in the output", "CONFLICTED" in out and "#2" in out,
          "got %r" % (out,))
    check("the output states no check could run",
          "no pull_request check can run" in out, "got %r" % (out,))

    code, out = run([pr(number=3, mergeable="UNKNOWN", state="UNKNOWN")])
    check("an undetermined PR exits 2, outranking a clean sweep", code == 2,
          "got %d: %s" % (code, out))
    check("an undetermined PR is reported as un-evaluated",
          "COULD NOT EVALUATE" in out, "got %r" % (out,))

    # BOTH a conflict and an error: 2 wins, and the conflict is still printed.
    code, out = run([pr(number=4, mergeable="CONFLICTING", state="DIRTY"),
                     pr(number=5, mergeable="UNKNOWN", state="UNKNOWN")])
    check("an error outranks a conflict in the exit code", code == 2,
          "got %d: %s" % (code, out))
    check("the conflict is still reported alongside the error",
          "CONFLICTED" in out and "COULD NOT EVALUATE" in out, "got %r" % (out,))

    # An unknown --repo is a refusal, not a silent empty sweep.
    gate.open_prs = lambda org, name: []
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = gate.main(["--repo", "not-a-repo", "--retries", "0"])
    check("an unknown --repo exits 2", code == 2, "got %d" % (code,))
    check("an unknown --repo says which one", "not-a-repo" in err.getvalue(),
          "got %r" % (err.getvalue(),))
finally:
    gate.open_prs = _real_open
    gate.post_status = _real_post
    gate.CD.load_inventory = _real_inv

# --- (16) the PR-list cap is a refusal, not a partial sweep ------------------
_real_gh = gate.CD.gh
try:
    import json as _json
    gate.CD.gh = lambda args: _json.dumps(
        [pr(number=i) for i in range(gate.PR_LIST_LIMIT)])
    raised = None
    try:
        gate.open_prs("tracebloc", "x")
    except Exception as exc:  # noqa: BLE001 - the type is asserted next
        raised = exc
    check("a PR list at the cap raises GhError, not a partial list",
          isinstance(raised, gate.CD.GhError),
          "got %r" % (type(raised).__name__ if raised else None,))
    check("the cap refusal explains that the view is partial",
          raised is not None and "partial" in str(raised), "got %r" % (raised,))

    # Unparseable JSON is a GhError too, naming the cause.
    gate.CD.gh = lambda args: "not json at all"
    raised = None
    try:
        gate.open_prs("tracebloc", "x")
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("an unparseable PR list raises GhError",
          isinstance(raised, gate.CD.GhError), "got %r" % (raised,))
    check("the unparseable refusal says so",
          raised is not None and "unparseable" in str(raised), "got %r" % (raised,))
finally:
    gate.CD.gh = _real_gh

# --- (17) THE WORKFLOW IS ARMED, not merely present -------------------------
#
# Everything above proves the SCRIPT is right. None of it proves anything RUNS
# it. A gate whose workflow does not invoke it is the org's own rule 7 shape --
# a file that claims a guarantee nothing checks -- and "assert a caller is ARMED,
# not merely present" is already a rule here (backend#1977).
#
# Read as YAML rather than grepped: a `run:` line inside a commented-out block,
# or under an `if: false`, greps identically to a live one.
_WF = ROOT / ".github" / "workflows" / "conflict-gate.yml"
try:
    import yaml  # noqa: E402
    _wf = yaml.safe_load(_WF.read_text(encoding="utf-8"))
except Exception as _exc:  # noqa: BLE001 - unreadable is a finding, not a skip
    _wf = None
    check("conflict-gate.yml is readable YAML", False, "got %r" % (_exc,))

if _wf is not None:
    # `on` is parsed by PyYAML 1.1 semantics as the boolean True, not the string
    # "on". Both are looked up so this does not silently find nothing and pass.
    _on = _wf.get("on", _wf.get(True)) or {}
    _jobs = _wf.get("jobs") or {}
    _steps = [s for j in _jobs.values() for s in (j.get("steps") or [])]
    _runs = " ".join(s.get("run", "") for s in _steps)

    check("the workflow actually invokes the gate",
          "scripts/conflict-gate.py" in _runs, "runs: %r" % (_runs,))

    # THE TRIGGER MUST NOT BE `pull_request`. That is the defect this whole file
    # works around: a conflicted PR dispatches no `pull_request` run at all, so a
    # `pull_request`-triggered conflict gate is inert on exactly its target.
    # Pinning this is what stops a future "why isn't this on PRs?" edit from
    # quietly reintroducing the bug.
    check("the workflow is NOT triggered by pull_request",
          "pull_request" not in _on, "on: %r" % (sorted(_on),))
    check("the workflow has a trigger that fires without a merge ref",
          "schedule" in _on, "on: %r" % (sorted(_on),))
    check("the workflow can be run on demand",
          "workflow_dispatch" in _on, "on: %r" % (sorted(_on),))

    # A sweep cancelled mid-run leaves the statuses it had not reached stale,
    # including `success` rows it was about to clear.
    check("the sweep is not cancelled in progress",
          (_wf.get("concurrency") or {}).get("cancel-in-progress") is False,
          "got %r" % (_wf.get("concurrency"),))

    # The token must be able to WRITE statuses, or every sweep reports findings it
    # cannot act on -- green run, no red row, the fail-open intact.
    _mint = [s for s in _steps
             if "create-github-app-token" in (s.get("uses") or "")]
    check("the workflow mints a token", len(_mint) == 1, "got %d" % (len(_mint),))
    _with = (_mint[0].get("with") if _mint else {}) or {}
    check("the mint asks for statuses: write",
          _with.get("permission-statuses") == "write",
          "got %r" % (_with.get("permission-statuses"),))
    check("the mint asks for pull-requests: read",
          _with.get("permission-pull-requests") == "read",
          "got %r" % (_with.get("permission-pull-requests"),))
    # Scoped, not broad: this job reads PRs and writes statuses, nothing else.
    check("the mint asks for nothing beyond those two permissions",
          sorted(k for k in _with if k.startswith("permission-"))
          == ["permission-pull-requests", "permission-statuses"],
          "got %r" % (sorted(k for k in _with if k.startswith("permission-")),))

if FAILURES:
    print("conflict-gate-selftest: %d/%d FAILED" % (len(FAILURES), COUNT))
    for f in FAILURES:
        print("  FAIL: " + f)
    sys.exit(1)
print("conflict-gate-selftest: %d assertions, all passed" % COUNT)
