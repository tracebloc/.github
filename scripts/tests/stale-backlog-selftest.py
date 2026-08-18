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


def issue(days_idle=100, status="Backlog", labels=(), project=2, number=1,
          updated=None, no_card=False):
    items = [] if no_card else [{
        "project": {"number": project},
        "fieldValueByName": ({"name": status} if status is not None else None),
    }]
    stamp = updated if updated is not None else \
        (NOW - timedelta(days=days_idle)).isoformat().replace("+00:00", "Z")
    return {
        "number": number, "title": "t", "updatedAt": stamp,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "projectItems": {"nodes": items},
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

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
