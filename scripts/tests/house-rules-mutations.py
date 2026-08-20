#!/usr/bin/env python3
"""Break each house rule; require the selftest to notice.

WHY THIS EXISTS (tracebloc/backend#1788)
---------------------------------------
`scripts/tests/house-rules-selftest.sh` says the matcher works. This says the
selftest would NOTICE if it stopped. Those are different claims, and this repo has
learned the difference the expensive way: a suite that cannot fail is the thing
#1788 is about, one level in.

It earned that on its first run. Two mutations passed the whole suite --

  * loosening `--tlsv1\\.[23]` to `--tlsv1`, because every case used a COMPLIANT
    flag, so the version FLOOR (the actual rule) was never exercised;
  * reading the raw line instead of the masked one, re-introducing a documented,
    already-fixed bug where `set -o pipefail` inside a string marks a whole file
    safe -- which nothing pinned.

Neither was visible in the suite's output. Both are cases now.

WHAT A MUTATION HERE MUST BE
----------------------------
It applies to the REAL script and runs the REAL suite. No re-implementation of a
rule inline: that reads as satisfied while breaking the real thing reddens nothing
(the corollary this org keeps re-learning -- `e2e-test-agent` #114/#115).

AND THE ANCHOR MUST APPLY. An inert mutation and good coverage are
indistinguishable in a log, so a mutation whose text is unchanged is reported STALE
and fails the run, exactly like an uncaught one. `--dry` resolves every anchor
without running the suite, which answers the stale question in milliseconds after a
refactor moves a line.

USAGE
  house-rules-mutations.py            apply each mutation, require the suite to redden
  house-rules-mutations.py --dry      resolve anchors only (no suite runs)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "scripts" / "house-rules.sh"
SUITE = ROOT / "scripts" / "tests" / "house-rules-selftest.sh"

# (label, old, new). `old` must appear EXACTLY ONCE -- an anchor that matches twice
# would mutate an arbitrary one of them, which is how a mutation lands on code no
# case drives and reports "uncaught" for the wrong reason. That happened while
# writing this file: an `if dry_run:` anchor in a sibling script matched a different
# function's early return.
MUTATIONS = [
    # --- the four rules stop being rules ---------------------------------
    ("curl-tls no longer requires a TLS floor",
     "            if (segx !~ /--tlsv1\\.[23]/)", "            if (0)"),
    ("curl-tls accepts ANY --tlsv1, so the version floor is gone",
     "/--tlsv1\\.[23]/", "/--tlsv1/"),
    ("helm-timeout no longer fires on --wait",
     'report(FILENAME, lno, "helm-timeout"', 'if (0) report(FILENAME, lno, "helm-timeout"'),
    ("pipefail: the `set -o pipefail` detector never matches",
     "(-[a-zA-Z]+[[:space:]]+)*-[a-zA-Z]*o[a-zA-Z]*[[:space:]]+pipefail/) pf_has = 1",
     "(-[a-zA-Z]+[[:space:]]+)*-[a-zA-Z]*o[a-zA-Z]*[[:space:]]+pipefail/) pf_has = 0"),

    # --- the lexer's precision promises stop holding ----------------------
    # Each of these makes the checker LOUDER, which is the direction that blocks
    # every merge in the org rather than the direction that misses a bug.
    ("pipefail reads the RAW line, so a quoted string marks a file safe",
     "if (lmask ~ /(^|[[:space:];])set[[:space:]]+",
     "if (lraw ~ /(^|[[:space:];])set[[:space:]]+"),

    # --- the suppression pragmas stop being precise ------------------------
    # `ignore=a,b` degrading to a bare `ignore` is the mutation Bugbot's finding on
    # .github#291 implies: under the first version of the suite's `expect` helper --
    # which asked "does this rule appear somewhere" instead of "which rules fired" --
    # the scoped-pragma case passed with the scoping disabled, because the OTHER rule
    # fired either way. The case existed to prove `ignore=` narrows to one rule and
    # could not have failed if it didn't. It reddens now.
    ("a scoped `ignore=` degrades to silencing every rule on the line",
     "if (match(line_txt, /house-rules:[[:space:]]*ignore=[A-Za-z0-9_,.-]+/)) {",
     "if (0) {"),

    # --- a config directive is silently ignored ---------------------------
    # `timeout-wrapper:` is the one whose case was VACUOUS until .github#291: a
    # compliant curl behind the wrapper exits 0 whether the directive is honoured
    # (curl is a command, timeout waived) or ignored (curl is a bare argument). The
    # fixture now drops the TLS flag, which splits the two -- honoured gives
    # rules=[curl-tls], ignored gives none -- so dropping the directive on the floor
    # reddens.
    ("the `timeout-wrapper:` directive is parsed and thrown away",
     'timeout-wrapper) CFG_TWRAPPERS="$CFG_TWRAPPERS $val" ;;',
     'timeout-wrapper) : ;;'),
]


def read() -> str:
    return TARGET.read_text(encoding="utf-8")


def apply_one(src: str, old: str, new: str) -> "str | None":
    n = src.count(old)
    if n != 1:
        raise LookupError(f"anchor matched {n} times, expected exactly 1: {old[:60]!r}")
    out = src.replace(old, new, 1)
    return None if out == src else out


def main() -> int:
    dry = "--dry" in sys.argv
    pristine = read()
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
        TARGET.write_text(mutated, encoding="utf-8")
        try:
            run = subprocess.run(["bash", str(SUITE)], capture_output=True, text=True, cwd=ROOT)
        finally:
            # ALWAYS restore, including on a crash. A mutation left on disk would
            # make every later run measure the wrong script -- and the tell would be
            # a suite that reddens for reasons nobody typed.
            TARGET.write_text(pristine, encoding="utf-8")
        m = re.search(r"(\d+) passed, (\d+) failed", run.stdout)
        failed = int(m.group(2)) if m else (1 if run.returncode else 0)
        caught = [line.split("  ", 2)[-1].strip()
                  for line in run.stdout.splitlines() if line.startswith("FAIL  ")]
        if failed > 0:
            print(f"  caught     {label}\n             by: {', '.join(caught)[:110]}")
        else:
            uncaught.append(label)
            print(f"  UNCAUGHT   {label}")

    if read() != pristine:  # belt and braces; the finally above should make this dead
        sys.stderr.write("::error::house-rules.sh was left mutated. Restore it from git.\n")
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
