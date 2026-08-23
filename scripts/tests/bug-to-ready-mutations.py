#!/usr/bin/env python3
"""Mutation harness for the bug-label promotion (tracebloc/backend#2348).

`bug-to-ready-selftest.py` asserts the job's behaviour; this asserts the
SELFTEST. Break a rule in the real artefact, watch the suite redden, restore. A
case that stays green under its own rule being deleted is vacuous, and a green
log cannot tell you which of the suite's assertions are load-bearing.

THE MUTATION EDITS THE CODE UNDER TEST (CLAUDE.md rule 9). Every anchor below
lands in `.github/workflows/customer-priority-bump.yml` or in `org-standards.md`
-- the two files the suite reads -- and the suite is then re-run against them.
There is no second copy of the decision anywhere in here. The alternative shape,
re-implementing the rule inline and mutating the copy, is indistinguishable from
real coverage in a log and has bitten this org twice (.github#114, #115).

EVERY ANCHOR MUST MATCH EXACTLY ONCE. An anchor that matches twice mutates an
arbitrary one, so an "uncaught" verdict is about the wrong line; an anchor that
matches zero times is stale and fails the run exactly like an uncaught mutation.
That is the assertion that the anchor ACTUALLY APPLIED -- otherwise an inert
mutation and good coverage look identical (CLAUDE.md rule 5). `--dry` resolves
every anchor without running the suite, which is what belongs in the fast tier:
it catches the way this file really breaks, a refactor moving a line an anchor
matched on.

  bug-to-ready-mutations.py          run them all
  bug-to-ready-mutations.py --dry    resolve anchors only

WHAT IS DELIBERATELY NOT MUTATED, stated because an unstated gap is how a suite
comes to be trusted for more than it proves: the network seam. The board read,
the 5x5s retry, the "card is not on the board" fail-closed and the
`updateProjectV2ItemFieldValue` write are driven by no case, so a mutation to
them would report UNCAUGHT for a reason that is about the suite's scope rather
than about its rigour. Those paths are asserted by inspection and by copying the
shape of the sibling workflows; they are named in the PR body as uncovered.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows" / "customer-priority-bump.yml"
CANON = ROOT / "org-standards.md"
SUITE = ROOT / "scripts" / "tests" / "bug-to-ready-selftest.py"

# (label, file, old, new)
MUTATIONS = [
    # --- the monotonic guard ------------------------------------------------
    ("the monotonic guard: promote from ANY placeable column, not just the source",
     WF,
     '  if [ "$_c" -eq "$_s" ]; then echo promote; else echo hold; fi',
     '  if [ "$_c" -ge 0 ]; then echo promote; else echo hold; fi'),
    ("the anchor ORDER half: check existence only, not that the target follows the source",
     WF,
     'if [ "$_s" -lt 0 ] || [ "$_t" -lt 0 ] || [ "$_s" -ge "$_t" ]; then echo noboard; return; fi',
     'if [ "$_s" -lt 0 ] || [ "$_t" -lt 0 ]; then echo noboard; return; fi'),
    # The ORDER of the two checks is itself load-bearing: with the shortcut first,
    # an unplaced card on an inverted board is promoted by a board nothing could
    # place it on.
    ("the anchor check runs SECOND, after the no-Status shortcut",
     WF,
     '            _s=$(col_index "${SOURCE_COLUMN}"); _t=$(col_index "${TARGET_COLUMN}")\n'
     '            if [ "$_s" -lt 0 ] || [ "$_t" -lt 0 ] || [ "$_s" -ge "$_t" ]; then echo noboard; return; fi\n'
     '            if [ "${2:-}" = "true" ]; then echo hold; return; fi\n'
     '            case "${1:-}" in\n'
     '              ""|"No status") echo promote; return ;;\n'
     '            esac\n',
     '            case "${1:-}" in\n'
     '              ""|"No status") echo promote; return ;;\n'
     '            esac\n'
     '            _s=$(col_index "${SOURCE_COLUMN}"); _t=$(col_index "${TARGET_COLUMN}")\n'
     '            if [ "$_s" -lt 0 ] || [ "$_t" -lt 0 ] || [ "$_s" -ge "$_t" ]; then echo noboard; return; fi\n'
     '            if [ "${2:-}" = "true" ]; then echo hold; return; fi\n'),
    ("the unplaceable column falls open and is promoted",
     WF,
     '  if [ "$_c" -lt 0 ]; then echo unknown; return; fi',
     '  if [ "$_c" -lt 0 ]; then echo promote; return; fi'),
    ("an ARCHIVED card is promoted like a live one",
     WF,
     '  if [ "${2:-}" = "true" ]; then echo hold; return; fi',
     '  if [ "${2:-}" = "__never__" ]; then echo hold; return; fi'),
    ("a card with NO Status is left unplaced, which is what kept the defect invisible",
     WF,
     '    ""|"No status") echo promote; return ;;',
     '    ""|"No status") echo hold; return ;;'),
    # The absence sentinel. `// 0` makes every absent column read as position 0,
    # which is the source column's own index.
    ("col_index reports an ABSENT column as position 0 instead of -1",
     WF,
     '| index($s) // -1',
     '| index($s) // 0'),

    # --- the policy: the fail-closed DIRECTION this job chose ---------------
    ("noboard exits 0, so an unreadable board order becomes a green no-op",
     WF,
     '"written. Check the column order and names on the board." >&2\n'
     '              exit 1 ;;',
     '"written. Check the column order and names on the board." >&2\n'
     '              exit 0 ;;'),
    ("unknown exits 0, the router's direction rather than this job's",
     WF,
     '"does not report as a Status option - refusing to guess whether \'${TARGET_COLUMN}\' is forward" >&2\n'
     '              exit 1 ;;',
     '"does not report as a Status option - refusing to guess whether \'${TARGET_COLUMN}\' is forward" >&2\n'
     '              exit 0 ;;'),
    ("the case falls through to the write on an unrecognised verdict",
     WF,
     '              echo "::error::unrecognised promotion verdict \'${_d}\' - refusing to write" >&2\n'
     '              exit 1 ;;',
     '              _write=yes ;;'),

    # --- the label/event gate ----------------------------------------------
    ("the label is matched as a SUBSTRING, so 'work-type:bugfix' promotes",
     WF,
     '    if [ "$2" != "$3" ]; then echo "refuse:other-label"; return; fi',
     '    case "$2" in *"$3"*) ;; *) echo "refuse:other-label"; return ;; esac'),
    ("an unreadable label reads as 'some other label' instead of failing closed",
     WF,
     '    if [ -z "${2:-}" ]; then echo "refuse:unreadable-label"; return; fi',
     '    if [ -z "${2:-x}" ]; then echo "refuse:unreadable-label"; return; fi'),
    ("an empty configured label matches everything instead of refusing",
     WF,
     '    if [ -z "${3:-}" ]; then echo "refuse:no-configured-label"; return; fi',
     '    if [ -z "${3:-x}" ]; then echo "refuse:no-configured-label"; return; fi'),
    ("a pull_request payload is accepted",
     WF,
     '    if [ "${4:-}" = "true" ]; then echo "refuse:pull-request-payload"; return; fi',
     '    if [ "${4:-}" = "__never__" ]; then echo "refuse:pull-request-payload"; return; fi'),
    ("any event may promote, not only `issues`",
     WF,
     '    if [ "${1:-}" != "issues" ]; then echo "refuse:not-an-issues-event"; return; fi',
     '    if [ "${1:-}" = "__never__" ]; then echo "refuse:not-an-issues-event"; return; fi'),

    # --- the job `if:` and the mint ----------------------------------------
    ("the job `if:` becomes a contains(), so the cost gate is LOOSER than the decision",
     WF,
     "    if: github.event_name == 'issues' && github.event.label.name == inputs.bug-label",
     "    if: github.event_name == 'issues' && contains(github.event.label.name, inputs.bug-label)"),
    ("the job `if:` stops pinning the event name",
     WF,
     "    if: github.event_name == 'issues' && github.event.label.name == inputs.bug-label",
     "    if: github.event.label.name == inputs.bug-label"),
    ("the new mint drops its scopes and takes the App's full grant",
     WF,
     "          permission-issues: read\n          permission-organization-projects: write\n",
     ""),

    # --- the vocabulary is really derived from the canon --------------------
    # Both directions, because a derivation is only live if BOTH sides moving is
    # a finding: the workflow drifting from the rule, and the rule being reworded
    # out from under the workflow.
    ("the workflow's label default drifts from the written rule",
     WF,
     '        default: "work-type:bug"',
     '        default: "work-type:bugs"'),
    ("the workflow writes a different column than the rule names",
     WF,
     '          TARGET_COLUMN: "Ready"',
     '          TARGET_COLUMN: "Ready for prod"'),
    ("the CANON renames the label and the workflow is not updated with it",
     CANON,
     "label them `work-type:bug`",
     "label them `type:bug`"),
]


def apply_one(src, old, new):
    n = src.count(old)
    if n != 1:
        raise LookupError("anchor matched %d times, expected exactly 1: %r"
                          % (n, old[:90]))
    out = src.replace(old, new, 1)
    return None if out == src else out


def main():
    dry = "--dry" in sys.argv
    pristine = {p: p.read_text(encoding="utf-8") for p in (WF, CANON)}
    stale, uncaught = [], []

    for label, path, old, new in MUTATIONS:
        try:
            mutated = apply_one(pristine[path], old, new)
        except LookupError as exc:
            stale.append((label, str(exc)))
            continue
        if mutated is None:
            stale.append((label, "NO-OP: the mutation changed nothing"))
            continue
        if dry:
            print("  anchor ok  %s" % label)
            continue
        path.write_text(mutated, encoding="utf-8")
        try:
            run = subprocess.run(
                [sys.executable, "-B", str(SUITE)],
                capture_output=True, text=True, cwd=str(ROOT),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        finally:
            # ALWAYS restore, including on a crash. A mutation left on disk makes
            # every later run measure the wrong file, and the tell is a suite that
            # reddens for reasons nobody typed.
            path.write_text(pristine[path], encoding="utf-8")
        caught = [ln.strip()[5:].strip() for ln in run.stdout.splitlines()
                  if ln.strip().startswith("FAIL:")]
        # A crash counts as caught ONLY if the suite actually ran and reported. A
        # bare traceback, or the extractor's own `sys.exit`, means the mutation
        # broke the harness rather than being detected by a case -- which is not
        # coverage, and must not be logged as if it were.
        reported = "bug-to-ready-selftest:" in run.stdout
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (label, "; ".join(caught)[:150]))
        elif not reported:
            uncaught.append((label, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % label)
        else:
            uncaught.append((label, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % label)

    for path, text in pristine.items():
        if path.read_text(encoding="utf-8") != text:
            sys.stderr.write("::error::%s was left mutated. Restore it from git.\n" % path.name)
            return 2

    print("\n%d mutation(s): %d stale, %d uncaught" % (len(MUTATIONS), len(stale), len(uncaught)))
    for label, why in stale:
        sys.stderr.write("::error::STALE mutation `%s`: %s\n" % (label, why))
    for label, why in uncaught:
        sys.stderr.write(
            "::error::UNCAUGHT `%s`: %s. Add a case that fails under it, or delete "
            "the mutation and say why it is not worth pinning.\n" % (label, why))
    return 1 if (stale or uncaught) else 0


if __name__ == "__main__":
    raise SystemExit(main())
