#!/usr/bin/env python3
"""Decision-table tests for scripts/stale-backlog.py (backend#1979, #1597 item 1).

WHY THIS EXISTS
---------------
This sweep CLOSES issues on a weekly cron, unattended. Every failure mode is
destructive and silent: a wrong eligibility rule does not error, it archives work
nobody meant to archive, and the only trace is a closed issue in a repo nobody was
watching that week.

The bug being fixed is exactly that. `actions/stale` exempts on LABELS ONLY, so an
item in `North Stars`, `Ready for prod` or `In progress` auto-closed after 8 weeks of
silence like any Backlog item. `kanban-reconcile.yml` carries a "Shield North Stars
from the stale sweep" step, which is the workaround admitting it.

So the cases below are mostly about what must NOT happen. The eligibility rule is an
ALLOW-list of one -- exactly `Backlog` -- and the most important assertions are that
every other value, including an unreadable one, is skipped.
"""
import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sb", ROOT / "scripts" / "stale-backlog.py")
sb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sb)

PASS = FAIL = 0
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


def record(cond, name, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok    {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}\n        {detail}")


def card(status="Backlog", project=2, archived=False, drop_archived=False):
    """One project card, in the shape ISSUES_Q's `projectItems.nodes` returns.

    `isArchived` IS PART OF THAT SHAPE TOO (Bugbot, #292), for the same reason
    `totalCount` is: a fixture that omits a field the real payload carries tests a
    query nobody runs. `archived=True` builds the card the sweep used to close --
    off the board, still stamped `Backlog`. `drop_archived` omits the field entirely,
    which is what the payload looks like if ISSUES_Q stops asking for it, and must
    read as UNKNOWN rather than as "not archived".
    """
    node = {
        "project": {"number": project},
        "fieldValueByName": ({"name": status} if status is not None else None),
    }
    if not drop_archived:
        node["isArchived"] = archived
    return node


def issue(days_idle=100, status="Backlog", labels=(), project=2, number=1,
          updated=None, no_card=False, label_total=None, item_total=None,
          drop_label_total=False, archived=False, drop_archived=False, cards=None):
    """One issue, in the shape ISSUES_Q returns.

    `totalCount` IS PART OF THAT SHAPE and defaults to the node count, i.e. "not
    truncated" (Bugbot, #288). `label_total` / `item_total` override it to construct
    a truncated read; `drop_label_total` omits the field entirely, which is what the
    payload looks like if the query stops asking -- and must read as UNKNOWN rather
    than as a complete list.

    Adding `totalCount` here reddened exactly two cases -- the two that WARN and
    CLOSE -- while every skip case stayed green. That is the shape of the fix: the
    only paths truncation blocks are the ones that write.

    `cards` takes an explicit list built by `card()` for the MULTI-CARD cases, which
    the single-card kwargs cannot express: two cards on one project, or a card on
    another board alongside this one.
    """
    if cards is not None:
        items = list(cards)
    elif no_card:
        items = []
    else:
        items = [card(status=status, project=project, archived=archived,
                      drop_archived=drop_archived)]
    stamp = updated if updated is not None else \
        (NOW - timedelta(days=days_idle)).isoformat().replace("+00:00", "Z")
    label_nodes = [{"name": n} for n in labels]
    label_conn = {"nodes": label_nodes}
    if not drop_label_total:
        label_conn["totalCount"] = (label_total if label_total is not None
                                    else len(label_nodes))
    return {
        "number": number, "title": "t", "updatedAt": stamp,
        "labels": label_conn,
        "projectItems": {"nodes": items,
                         "totalCount": (item_total if item_total is not None
                                        else len(items))},
    }


def act(i):
    return sb.decide(i, 2, NOW)[0]


# --- POSITIVE CONTROL: the thing the sweep is FOR ---------------------------
record(act(issue(days_idle=50)) == "stale",
       "an idle Backlog issue past 42d is warned", f"got {act(issue(days_idle=50))}")
record(act(issue(days_idle=20)) is None,
       "a Backlog issue inside 42d is left alone")
record(act(issue(days_idle=20, labels=["stale"])) == "close",
       "a warned issue silent for 14d+ is closed")
record(act(issue(days_idle=5, labels=["stale"])) is None,
       "a warned issue with recent activity is NOT closed — the clock reset")

# --- THE FIX: every non-Backlog column is skipped ---------------------------
# The bug this ticket exists for. `North Stars` is the one that motivated the
# shield step in kanban-reconcile; the others are listed because an allow-list of
# one has to be tested as one, not as "the columns we remembered".
for col in ("North Stars", "Ready", "In progress", "Code review", "On dev",
            "Staging (agent review)", "FR on staging", "Ready for prod", "Prod",
            "Done", "Cancelled"):
    record(act(issue(days_idle=500, status=col)) is None,
           f"an ancient issue in {col!r} is NOT touched",
           f"got {act(issue(days_idle=500, status=col))}")

# --- FAIL CLOSED means SKIP, because the destructive direction is closing ---
record(act(issue(days_idle=500, status=None)) is None,
       "a card with NO Status value is skipped, not closed")
record(act(issue(days_idle=500, no_card=True)) is None,
       "an issue with NO CARD at all is skipped, not closed")
record(act(issue(days_idle=500, status=123)) is None,
       "a non-string Status is skipped, not closed")
record(act(issue(days_idle=500, project=99)) is None,
       "a card on a DIFFERENT project does not make it eligible")
record(act(issue(updated="not-a-date")) is None,
       "an unreadable updatedAt is skipped, not closed")

# --- AN ARCHIVED CARD IS NOT A LIVE CARD (Bugbot, #292) ---------------------
# The worst case this suite has held, because it ENDED IN `issue close` rather than
# in a wrong log line. `projectItems` returns archived items by default
# (`includeArchived: true`), and archiving a card DOES NOT CLEAR ITS STATUS --
# `kanban-archive.yml` archives terminal items and leaves the field alone, so a card
# archived out of `Backlog` reads `Backlog` forever. The sweep therefore matched work
# somebody had deliberately taken OFF the board and closed it, and an issue whose card
# is archived is precisely an issue nobody is watching. The trigger is ordinary, not
# exotic: one bulk board tidy-up mints a batch of archived-but-`Backlog` cards.
#
# Both directions are asserted as a PAIR, on the same fixture but for the archived
# flag, because either half alone is passable by a broken implementation: "archived is
# skipped" alone passes if the sweep skips everything, and "live is warned" alone is
# the pre-fix behaviour.
record(act(issue(days_idle=500, archived=True)) is None,
       "an ancient archived Backlog card is NOT warned",
       f"got {act(issue(days_idle=500, archived=True))!r} — before the fix this was 'stale'")
record(act(issue(days_idle=500, archived=False)) == "stale",
       "...while the same card un-archived still IS warned",
       "the pair is the test; skipping everything would pass the half above")

# `close` specifically, not "some action": an already-warned archived card was the
# path that destroyed work, and asserting `is None` against a fixture that would
# merely have been re-warned understates what the fix prevents.
record(act(issue(days_idle=20, labels=["stale"], archived=True)) is None,
       "an archived card already carrying `stale` is NOT closed",
       f"got {act(issue(days_idle=20, labels=['stale'], archived=True))!r} — "
       "before the fix this was 'close', i.e. somebody's tracked work destroyed")
record(act(issue(days_idle=20, labels=["stale"], archived=False)) == "close",
       "...while the same card un-archived still IS closed")

# `continue`, NOT `return None`, on hitting an archived card. GitHub can leave an
# issue holding an archived card AND a live one on the SAME project; bailing out on
# the first archived node hides the live card behind it, and the sweep then skips an
# issue that is genuinely due. Node order is the part nobody controls, so both orders
# are built.
record(act(issue(days_idle=500, cards=[card(status="Backlog", archived=True),
                                       card(status="Backlog", archived=False)])) == "stale",
       "a live Backlog card BEHIND an archived one is still found",
       "bailing on the first archived node would skip a due issue")
record(act(issue(days_idle=500, cards=[card(status="Backlog", archived=False),
                                       card(status="Backlog", archived=True)])) == "stale",
       "...and in the other node order too")

# The mirror of that: archived cards only, nothing live to fall through to.
record(act(issue(days_idle=500, cards=[card(status="Backlog", archived=True),
                                       card(status="Backlog", archived=True)])) is None,
       "an issue whose ONLY cards are archived is skipped")

# MULTI-PROJECT: the target board's card is archived, another board still says
# `Backlog`. The `project.number` test already selects the target board, so this is a
# guard against a fix that filtered archived cards while loosening that test -- the
# sweep must never borrow eligibility from a board it was not pointed at.
record(act(issue(days_idle=500, cards=[card(status="Backlog", project=2, archived=True),
                                       card(status="Backlog", project=99)])) is None,
       "another project's live Backlog does not resurrect an archived target card")

# NO isArchived AT ALL means no answer, the same rule `totalCount` gets. If ISSUES_Q
# stops asking, every card must read as un-established and the sweep must go loudly
# useless -- counted as unreadable and failing `--strict` -- rather than quietly
# resuming the close.
record(act(issue(days_idle=500, drop_archived=True)) is None,
       "a card with no isArchived field is UNKNOWN, not 'not archived'",
       f"got {act(issue(days_idle=500, drop_archived=True))!r}")
record(sb.decide(issue(days_idle=500, archived=True), 2, NOW)[2] is None,
       "an archived card reports status None, so main() counts it as unreadable",
       "it must land in the `unknown` counter that --strict fails on, not in `skipped`")

# `Backlog` must match exactly — a near-miss is unknown, not eligible.
for near in ("backlog", "BACKLOG", " Backlog", "Backlog ", "Backlogged"):
    record(act(issue(days_idle=500, status=near)) is None,
           f"{near!r} does not count as Backlog", f"got {act(issue(days_idle=500, status=near))}")

# --- exempt labels still win, and BEFORE the board is consulted -------------
record(act(issue(days_idle=500, labels=["keep-open"])) is None,
       "keep-open exempts a Backlog issue")
record(act(issue(days_idle=500, labels=["blocked"])) is None,
       "blocked exempts a Backlog issue")

# --- the status is returned structurally, not parsed out of the reason ------
# An earlier version counted unreadable statuses by string-matching the
# human-readable reason, coupling a counter to prose that exists to be reworded.
record(sb.decide(issue(status=None, no_card=True), 2, NOW)[2] is None,
       "decide() reports an unknown status as None, for counting")
record(sb.decide(issue(status="Backlog"), 2, NOW)[2] == "Backlog",
       "decide() reports a known status verbatim")

# --- the ceiling refuses rather than executes -------------------------------
record(sb.MAX_ACTIONS > 0 and sb.ELIGIBLE_STATUS == "Backlog"
       and sb.EXEMPT_LABELS == {"keep-open", "blocked"},
       "the constants are the ones the workflow documents",
       f"MAX_ACTIONS={sb.MAX_ACTIONS} status={sb.ELIGIBLE_STATUS!r} exempt={sb.EXEMPT_LABELS}")

# --- a truncated read is COUNTED, not folded into "skipped" ---------------------
# decide() must report truncation as its own status so main() can count it. Folding it
# into None -- "no card", an ordinary outcome -- is what let a query regression print
# `0 due` and exit 0 on an unattended cron.
act, why, st = sb.decide(issue(days_idle=100, labels=[f"l{i}" for i in range(40)],
                              label_total=41), 2, NOW)
record(st is sb.TRUNCATED, "a truncated read reports status TRUNCATED, not None",
       f"got {st!r} — None means 'no card', which is normal; this is a failed read")
record(sb.TRUNCATED is not None,
       "TRUNCATED is a distinct sentinel, not None or a string",
       f"{sb.TRUNCATED!r} — a string could collide with a real column name")

# An ordinary no-card issue must NOT be counted as truncated, or the new counter
# fires on every repo with an unfiled issue and the signal is worthless.
act, why, st = sb.decide(issue(days_idle=100, no_card=True), 2, NOW)
record(st is None, "an ordinary no-card issue is None, not TRUNCATED", f"got {st!r}")

# --- main() ACTUALLY DRIVEN, because two mutations lived where no case looked -----
# The cases above call decide() directly. Mutations that stopped incrementing the
# `cut` counter, or removed truncation from `--strict`, left every one of them green
# -- and the whole point of this finding was that a skip nobody counts reads as a
# clean sweep. The gap was in main(), so main() is what gets driven.
#
# Behavioural, not a source assertion: fetch_issues and apply are the only network
# seams, so stubbing them is enough to run the real argument parsing, the real
# counters and the real exit codes.
def run_main(issues, argv, label=(True, "exists"), calls=None):
    """Drive main() with every network seam stubbed.

    `ensure_label` IS ONE OF THOSE SEAMS, and forgetting it was caught by this suite
    rather than by review: the clean-sweep case started returning 2 because
    ensure_label ran a real `gh` against the fixture repo `o/r`. A stub list that is
    one short is a test measuring the machine it runs on.

    `calls` collects the label probes so a case can assert one did NOT happen.
    """
    import contextlib
    import io
    real_fetch, real_apply, real_label = sb.fetch_issues, sb.apply, sb.ensure_label

    def _label(repo, dry):
        if calls is not None:
            calls.append((repo, dry))
        return label
    sb.fetch_issues = lambda owner, name: issues
    sb.apply = lambda repo, num, action, dry: (True, "")
    sb.ensure_label = _label
    old = sys.argv
    sys.argv = ["stale-backlog.py", *argv]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = sb.main()
    finally:
        sb.fetch_issues, sb.apply, sb.ensure_label = real_fetch, real_apply, real_label
        sys.argv = old
    return rc, buf.getvalue()

_cut = issue(days_idle=100, labels=[f"l{i}" for i in range(40)], label_total=41)

rc, out = run_main([_cut], ["--repo", "o/r", "--strict"])
record(rc == 2, "--strict FAILS when a label read was truncated", f"rc={rc}")
record("TRUNCATED label read" in out, "...and the summary counts it separately",
       out.strip().splitlines()[0] if out.strip() else "(no output)")

rc, out = run_main([_cut], ["--repo", "o/r"])
record(rc == 0 and "::warning::" in out,
       "without --strict it still WARNS rather than passing silently",
       "a truncated sweep printing `0 due` and exiting 0 is the finding itself")

# And the counter must not fire on an ordinary sweep, or the signal is noise.
rc, out = run_main([issue(days_idle=100)], ["--repo", "o/r", "--strict"])
record(rc == 0 and "TRUNCATED" not in out,
       "a clean sweep reports no truncation and passes --strict", f"rc={rc}")

# --- main() DRIVEN on the archived card, because "nothing is closed" is the claim ---
# The decide() cases above pin the verdict; this pins the SWEEP. It is the same
# function-versus-wiring split this suite keeps catching: a verdict of None is worth
# nothing if main() still logs a CLOSE, and an archived-but-`Backlog` board must show
# up as "could not tell" rather than as a clean run -- otherwise the one mode that
# exists to make an incomplete sweep visible reports success over a board full of
# cards it declined to judge.
_arch = issue(days_idle=500, labels=["stale"], archived=True)   # pre-fix: CLOSE

rc, out = run_main([_arch], ["--repo", "o/r"])
record(rc == 0 and "0 due" in out and "CLOSE" not in out,
       "main() closes NOTHING on an archived Backlog issue",
       out.strip().splitlines()[0] if out.strip() else "(no output)")
record("no readable Status" in out,
       "...and counts it as unreadable, not as an ordinary skip",
       "an archived card the sweep declined to judge is not the same event as "
       "an issue that simply is not in Backlog")

rc, out = run_main([_arch], ["--repo", "o/r", "--strict"])
record(rc == 2, "--strict FAILS on an archived Backlog card", f"rc={rc}")

# --- ensure_label ITSELF, not just its call site ---------------------------------
# The cases below stub `ensure_label`, so they pin WHEN it is called and nothing about
# what it does. A mutation making it report success on a failed `gh label create` left
# all 49 green -- the same function-versus-wiring split that bit .github#289 and the
# `cut` counter earlier on this PR, now in the third place. So the real function runs
# here, with `subprocess.run` stubbed instead.
class _Proc:
    def __init__(self, rc, err=""):
        self.returncode, self.stderr, self.stdout = rc, err, ""


def _drive_ensure(probe_rc, create_rc, create_err="", dry=False):
    """Run the real ensure_label with both `gh` calls faked. Returns (result, argv)."""
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        if cmd[:2] == ["gh", "api"]:
            return _Proc(probe_rc)
        return _Proc(create_rc, create_err)
    real = sb.subprocess.run
    sb.subprocess.run = fake_run
    try:
        return sb.ensure_label("o/r", dry), seen
    finally:
        sb.subprocess.run = real

(ok, detail), seen = _drive_ensure(probe_rc=0, create_rc=0)
record(ok and detail == "exists" and len(seen) == 1,
       "an existing label is detected and nothing is created",
       f"{detail!r}, {len(seen)} gh call(s) — eleven of nineteen repos are this case")

(ok, detail), seen = _drive_ensure(probe_rc=1, create_rc=0)
record(ok and "created" in detail and len(seen) == 2,
       "a missing label is created", f"{detail!r}, {len(seen)} gh call(s)")

(ok, detail), seen = _drive_ensure(probe_rc=1, create_rc=1, create_err="HTTP 403 forbidden")
record(ok is False and "403" in detail,
       "a FAILED create reports False and carries the reason",
       f"{(ok, detail)!r} — reporting True here is a warn loop that fails per issue")

(ok, detail), seen = _drive_ensure(probe_rc=1, create_rc=0, dry=True)
record(ok and "dry run" in detail and len(seen) == 1,
       "--dry-run does not create the label",
       f"{detail!r}, {len(seen)} gh call(s) — a dry run must change nothing")

# --- the `stale` label must exist before the first warn (Bugbot, #288) ----------
# `actions/stale` created this label; `gh issue edit --add-label` does not -- it fails
# on a label the repo has never defined. Measured across the org 2026-08-20: only
# 11 of 19 repos have it, and the eight without include `.github`, where this reusable
# lives. So the warn step would have failed on the first repo it ran in.
_warn = issue(days_idle=100)          # 100d idle, no stale label yet -> warn is due
_close = issue(days_idle=20, labels=["stale"])   # already warned -> close is due

rc, out = run_main([_warn], ["--repo", "o/r"], label=(False, "no permission"))
record(rc == 2 and "Refusing to warn" in out,
       "a label that cannot be created REFUSES the warn instead of failing per-issue",
       f"rc={rc} — per-issue failures would redden the run and warn nobody")

_calls = []
rc, out = run_main([_close], ["--repo", "o/r"], calls=_calls)
record(rc == 0 and _calls == [],
       "a close-only run does not touch the label at all",
       f"calls={_calls} — a repo with nothing to warn should not acquire the label")

_calls = []
rc, out = run_main([_warn], ["--repo", "o/r"], calls=_calls)
record(rc == 0 and len(_calls) == 1,
       "a warn-due run ensures the label exactly once",
       f"calls={_calls} — once per run, not once per issue")

# --- the LIVE QUERY must actually ask for totalCount (Bugbot, #288) -------------
# The cases below build their own payloads, so they say nothing about whether
# ISSUES_Q requests the field they all supply. Drop `totalCount` from the live query
# and every fixture stays green while production treats EVERY label list as cut and
# sweeps nothing. That is the same restate-instead-of-derive shape as the meta
# fixture on .github#289, in the other direction: the test was richer than the query.
#
# Asserted against the query TEXT, which is the producer, not against a copy here.
record("totalCount" in sb.ISSUES_Q,
       "ISSUES_Q asks for totalCount at all",
       "without it `truncated()` reports True for every issue and nothing is swept")
_lab = sb.ISSUES_Q[sb.ISSUES_Q.index("labels("):]
record("totalCount" in _lab[:_lab.index("}")],
       "...on the LABELS connection specifically",
       _lab[:_lab.index("}") + 1])
_it = sb.ISSUES_Q[sb.ISSUES_Q.index("projectItems("):]
record("totalCount" in _it[:60],
       "...and on the projectItems connection",
       _it[:60])

# The same producer-side check for `isArchived` (Bugbot, #292). Every archived-card
# fixture above SUPPLIES the field, so all of them stay green if ISSUES_Q stops
# asking for it -- and production then reads every card as un-established and sweeps
# nothing. `includeArchived` defaults to true, so the field is the only thing standing
# between the sweep and cards that are off the board: it is asserted against the query
# TEXT, not against a copy of the shape in this file.
record("isArchived" in _it[:120],
       "ISSUES_Q asks whether each project card is archived",
       _it[:120])

# --- truncation is fail-open on the destructive path (Bugbot, #288) -----------
# A `keep-open` label past the page would read as ABSENT and the issue would be
# closed. Unlike an unreadable Status -- which lands on "not Backlog" and skips
# anyway -- this one PROCEEDED. So it is checked before the exempt test and refuses.
act, why, st = sb.decide(issue(days_idle=100, labels=[f"l{i}" for i in range(40)],
                              label_total=41), 2, NOW)
record(act is None, "a truncated label list is skipped, not warned", f"got {act!r}")
record("truncated" in why, "...and the reason names the truncation", why)

# Exactly-full is NOT truncated. Without this the guard could be `len(nodes) >=
# page` and skip every issue with a full label page -- silently unswept, which is
# the opposite failure and just as invisible.
act, why, st = sb.decide(issue(days_idle=100, labels=[f"l{i}" for i in range(40)],
                              label_total=40), 2, NOW)
# `stale`, named specifically rather than `is not None`: an assertion that any
# action came back would pass on a CLOSE, which is a different and worse outcome
# for a first-warning issue.
record(act == "stale", "a label list that is exactly full is NOT truncated", f"got {act!r} — {why}")

# NO totalCount AT ALL means no answer. If the query stops asking, every issue must
# skip -- loudly useless rather than quietly closing things on a partial list.
act, why, st = sb.decide(issue(days_idle=100, drop_label_total=True), 2, NOW)
record(act is None, "a label connection with no totalCount is UNKNOWN, not complete", f"got {act!r}")

# THE ORDER OF THE TWO SKIP TESTS IS ABOUT THE REASON, NOT THE ACTION, and this is
# the case that makes it testable. A mutation that OR'd them together left every
# other assertion green -- both paths skip -- while an `keep-open` issue would then
# be reported as "label list truncated". The action is right and the log lies about
# why, which is the shape that sends someone to fix the wrong thing.
#
# So the exempt reason is asserted specifically, not merely that something skipped.
act, why, st = sb.decide(issue(days_idle=100, labels=["keep-open"]), 2, NOW)
# `startswith`, NOT `in`. The truncation message contains the words "exempt label"
# ("cannot rule out an exempt label"), so `"exempt label" in why` passes on the
# WRONG reason -- the substring collision that made this assertion inert on its
# first attempt, and the same shape as an earlier inert check in this repo.
record(act is None and why.startswith("exempt label") and "truncated" not in why,
       "an exempt issue is skipped FOR BEING EXEMPT, not for anything else",
       f"got {act!r} — {why!r}")

# The card page fails SAFE -- no card lands on "not Backlog" -- but it is checked by
# the same rule rather than by accident.
act, why, st = sb.decide(issue(days_idle=100, no_card=True, item_total=3), 2, NOW)
record(act is None and st is None,
       "a truncated card page yields no status, so nothing is done", f"got {act!r}/{st!r}")


print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
