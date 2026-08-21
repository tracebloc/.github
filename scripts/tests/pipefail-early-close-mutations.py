#!/usr/bin/env python3
"""Mutation harness for the early-close gate (backend#2264).

WHY. `pipefail-early-close-selftest.sh` asserts the gate's behaviour; this
asserts the SELFTEST. Break a rule, watch the suite redden, restore. A case
that stays green under its own rule being deleted is vacuous, and a green
selftest log cannot tell you which of its cases are load-bearing.

TWO TARGETS, because the rule genuinely lives in two files. The `.awk` decides
which LINES are offenders; the `.sh` decides which FILES run under both options
(the inheritance fixpoint, the option-sign seed, the derived file list). A
harness that only mutated the awk would report full coverage while the
wrapper's logic -- the half that made this gate need a wrapper at all -- went
unpinned.

Every anchor must match EXACTLY ONCE. An anchor matching twice mutates an
arbitrary one of them, so the run reports "uncaught" for the wrong reason; an
anchor matching zero times is stale and fails the run, exactly like an uncaught
mutation. `--dry` resolves every anchor without running the suite, which is the
cheap check that belongs in `make lint`.

  pipefail-early-close-mutations.py          run them all
  pipefail-early-close-mutations.py --dry    resolve anchors only
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AWK = ROOT / "scripts" / "pipefail-early-close.awk"
SH = ROOT / "scripts" / "pipefail-early-close.sh"
SUITE = ROOT / "scripts" / "tests" / "pipefail-early-close-selftest.sh"

# (label, target, old, new)
MUTATIONS = [
    # --- the hazard stops being detected ---------------------------------
    ("the head arm never fires", AWK,
     'if (probe ~ /\\|&?[[:space:]]*head(', 'if (0 && probe ~ /\\|&?[[:space:]]*head('),
    ("the grep -q arm never fires", AWK,
     '|| probe ~ /\\|&?[[:space:]]*grep[^|\\001]*[[:space:]]-[a-zA-Z]*q/',
     '|| (0 && probe ~ /\\|&?[[:space:]]*grep[^|\\001]*[[:space:]]-[a-zA-Z]*q/)'),
    ("the grep -m arm never fires", AWK,
     '|| probe ~ /\\|&?[[:space:]]*grep[^|\\001]*[[:space:]]-[a-zA-Z]*m[[:space:]]*[0-9]/',
     '|| (0 && probe ~ /\\|&?[[:space:]]*grep[^|\\001]*[[:space:]]-[a-zA-Z]*m[[:space:]]*[0-9]/)'),

    # --- the `||` boundary work, all three reviewer-found shapes ---------
    ("the || neutralisation is removed", AWK,
     '  gsub(/\\|\\|/, "\\001\\001", probe)', '  # gsub removed'),
    ("\\001 dropped from the grep -q class (the span leak)", AWK,
     'grep[^|\\001]*[[:space:]]-[a-zA-Z]*q/', 'grep[^|]*[[:space:]]-[a-zA-Z]*q/'),
    ("\\001 dropped from the grep -m class", AWK,
     'grep[^|\\001]*[[:space:]]-[a-zA-Z]*m[[:space:]]*[0-9]/', 'grep[^|]*[[:space:]]-[a-zA-Z]*m[[:space:]]*[0-9]/'),
    ("\\001 dropped from the head terminator class (head||die)", AWK,
     '`;|&\\001])/', '`;|&])/'),

    # --- `|&` is a pipe ---------------------------------------------------
    ("|& no longer counts as a pipe on the head arm", AWK,
     '/\\|&?[[:space:]]*head(', '/\\|[[:space:]]*head('),

    # --- the sparing rules ------------------------------------------------
    ("`|| true` no longer discards the status", AWK,
     'if (line ~ /\\|\\|[[:space:]]*(true|:)([[:space:]]|$|\\))/) next',
     'if (0) next'),
    ("comments are scanned as code", AWK,
     'if (line ~ /^[[:space:]]*#/) next', 'if (0) next'),
    ("the allow marker stops opting a line out", AWK,
     'if (line ~ /#[[:space:]]*pipefail-guard:[[:space:]]*allow/) next', 'if (0) next'),

    # --- option state is positional --------------------------------------
    ("errexit is assumed on everywhere", AWK, 'if (!(e_on && p_on)) next', 'if (!(p_on)) next'),
    ("pipefail is assumed on everywhere", AWK, 'if (!(e_on && p_on)) next', 'if (!(e_on)) next'),

    # --- the WRAPPER's half ----------------------------------------------
    ("the extractor leaves the quote on a basename-only `source \"worker.sh\"`", SH,
     "s|.*/||; s|^.*[[:space:]]||; s|^\"||", "s|.*/||; s|^.*[[:space:]]||"),
    ("the inheritance fixpoint is skipped, so libs read as safe", SH,
     '  [ "$added" -eq 0 ] && break', '  break'),
    ("the seed anchors the option to the FIRST cluster, losing `set -eu -o pipefail`", SH,
     "'^[[:space:]]*set[[:space:]].*-[a-zA-Z]*o[[:space:]]+pipefail'",
     "'^[[:space:]]*set[[:space:]]+-[a-zA-Z]*o?[[:space:]]+pipefail'"),
    ("the seed ignores the SIGN, so `set +o pipefail` reads as hazardous", SH,
     "grep -qE '^[[:space:]]*set[[:space:]].*-[a-zA-Z]*o[[:space:]]+pipefail' \"$f\" 2>/dev/null",
     "grep -qE 'pipefail' \"$f\" 2>/dev/null"),
    ("the file list is extension-only, losing shebang-classified files", SH,
     """      *) head -n 1 "$f" 2>/dev/null \\
           | grep -Eq '^#![[:space:]]*[^[:space:]]*(/|[[:space:]])(ba|da|k)?sh([[:space:]]|$)' \\
           && files+=("$f") ;;""",
     "      *) ;;"),
    ("an unreadable tree reports clean instead of failing closed", SH,
     """    echo "pipefail-early-close: 'git ls-files' failed in $ROOT — refusing to report clean" >&2
    exit 2""",
     """    exit 0"""),
]


def apply_one(src: str, old: str, new: str) -> "str | None":
    n = src.count(old)
    if n != 1:
        raise LookupError(f"anchor matched {n} times, expected exactly 1: {old[:70]!r}")
    out = src.replace(old, new, 1)
    return None if out == src else out


def main() -> int:
    dry = "--dry" in sys.argv
    pristine = {AWK: AWK.read_text(encoding="utf-8"), SH: SH.read_text(encoding="utf-8")}
    stale, uncaught = [], []

    for label, target, old, new in MUTATIONS:
        try:
            mutated = apply_one(pristine[target], old, new)
        except LookupError as exc:
            stale.append((label, str(exc)))
            continue
        if mutated is None:
            stale.append((label, "NO-OP: the mutation changed nothing"))
            continue
        if dry:
            print(f"  anchor ok  {label}")
            continue
        target.write_text(mutated, encoding="utf-8")
        try:
            run = subprocess.run(["bash", str(SUITE)], capture_output=True, text=True, cwd=ROOT)
        finally:
            # ALWAYS restore, including on a crash. A mutation left on disk makes
            # every later run measure the wrong script, and the tell is a suite
            # that reddens for reasons nobody typed.
            target.write_text(pristine[target], encoding="utf-8")
        m = re.search(r"(\d+) passed, (\d+) failed", run.stdout)
        failed = int(m.group(2)) if m else (1 if run.returncode else 0)
        caught = [line.split("  ", 2)[-1].strip()
                  for line in run.stdout.splitlines() if line.startswith("FAIL  ")]
        if failed > 0:
            print(f"  caught     {label}\n             by: {', '.join(caught)[:110]}")
        else:
            uncaught.append(label)
            print(f"  UNCAUGHT   {label}")

    for target, text in pristine.items():
        if target.read_text(encoding="utf-8") != text:
            sys.stderr.write(f"::error::{target.name} was left mutated. Restore it from git.\n")
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
