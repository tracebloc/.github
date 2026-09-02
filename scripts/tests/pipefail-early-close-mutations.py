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

# THE BASELINE THIS RUN MEASURES AGAINST MUST BE VERIFIABLE, NOT ASSUMED
# (backend#2441). The `finally` below restores the file on a crash; it cannot
# restore it after SIGKILL, a runner timeout, or a second harness racing this
# one in the same worktree -- and a mutation left on disk becomes the NEXT run's
# `pristine`, which then reports `0 uncaught` about a premise nobody typed.
# See scripts/tests/mutation_baseline.py.
#
# dont_write_bytecode BEFORE the import, deliberately: `selftests-cover` rejects
# anything under scripts/tests/ that is not a suite or a runner, and a
# `__pycache__/` left by this import is exactly that.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402


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
    # THE ANCHOR NAMES THE HEAD ARM. That terminator class is now shared with
    # the `sed q` and `read` arms added for backend#2967, so the bare class
    # string matches three times -- and an anchor matching more than once
    # mutates an arbitrary one of them, then reports about the wrong arm.
    ("\\001 dropped from the head terminator class (head||die)", AWK,
     'head([[:space:]]|$|[)"\'\\\'\'`;|&\\001])/',
     'head([[:space:]]|$|[)"\'\\\'\'`;|&])/'),

    # --- `|&` is a pipe ---------------------------------------------------
    ("|& no longer counts as a pipe on the head arm", AWK,
     '/\\|&?[[:space:]]*head(', '/\\|[[:space:]]*head('),

    # --- the sparing rules ------------------------------------------------
    ("the allow marker stops opting a line out", AWK,
     'if (line ~ /#[[:space:]]*pipefail-guard:[[:space:]]*allow/) next', 'if (0) next'),

    # --- option state is positional --------------------------------------
    ("apply_set reads the RAW line, so a set-line comment disarms the options", AWK,
     '  line = strip_trailing_comment(line)',
     '  # strip removed'),
    ("the `|| true` spare is unanchored again, covering the whole segment", AWK,
     '    if (segtext ~ /\\|\\|[[:space:]]*(true|:)[[:space:])"\']*[[:space:]]*$/) continue',
     '    if (segtext ~ /\\|\\|[[:space:]]*(true|:)/) continue'),
    ("the multi-line function opener `next`s again, skipping its own body", AWK,
     '      save_e = e_on; save_p = p_on; in_fn = 1\n    }',
     '      save_e = e_on; save_p = p_on; in_fn = 1; next\n    }'),
    ("the segment path reads the RAW line, so a trailing comment breaks the anchor", AWK,
     '  code = strip_trailing_comment(line)\n  nseg = split(code, seg, /;|&&/)',
     '  nseg = split(line, seg, /;|&&/)'),
    ("the comment strip is a blunt regex, cutting a '#' inside a string", AWK,
     '      if (i == 1 || substr(s, i - 1, 1) ~ /[[:space:]]/) return substr(s, 1, i - 1)',
     '      return substr(s, 1, i - 1)'),
    ("the set-line dispatch `next`s again, skipping code on the same line", AWK,
     '  if (line ~ /^[[:space:]]*set[[:space:]]/) apply_set(line)',
     '  if (line ~ /^[[:space:]]*set[[:space:]]/) { apply_set(line); next }'),
    ("apply_set does not split on ';', so `pipefail;` never registers", AWK,
     '  n = split(line, a, /[[:space:];]+/)', '  n = split(line, a, /[[:space:]]+/)'),
    ("errexit is assumed on everywhere", AWK, 'if (!(e_on && p_on)) next', 'if (!(p_on)) next'),
    ("pipefail is assumed on everywhere", AWK, 'if (!(e_on && p_on)) next', 'if (!(e_on)) next'),

    # --- the WRAPPER's half ----------------------------------------------
    ("the extractor stops stripping quotes, so a quoted target never matches", SH,
     '  sed -E \'s/#.*$//; s/["\'"\'"\']//g\' "$1" 2>/dev/null \\',
     '  sed -E \'s/#.*$//\' "$1" 2>/dev/null \\'),
    ("the seed anchors the option to the FIRST cluster, losing `set -eu -o pipefail`", SH,
     '  grep -qE \'\\-[a-zA-Z]*o[[:space:]]+pipefail\' <<<"$setlines" || continue',
     '  grep -qE \'^[[:space:]]*set[[:space:]]+-[a-zA-Z]*o[[:space:]]+pipefail\' <<<"$setlines" || continue'),
    ("the seed ignores the SIGN, so `set +o pipefail` reads as hazardous", SH,
     '  grep -qE \'\\-[a-zA-Z]*o[[:space:]]+pipefail\' <<<"$setlines" || continue',
     '  grep -qE \'pipefail\' <<<"$setlines" || continue'),
    ("the seed no longer strips trailing comments", SH,
     "setlines=$(sed -E 's/#.*$//' \"$f\" 2>/dev/null | grep -E '^[[:space:]]*set[[:space:]]')",
     "setlines=$(grep -E '^[[:space:]]*set[[:space:]]' \"$f\" 2>/dev/null)"),
    ("the inheritance fixpoint is skipped, so libs read as safe", SH,
     '  [ "$added" -eq 0 ] && break', '  break'),
    ("the file list is extension-only, losing shebang-classified files", SH,
     """      *) head -n 1 "$f" 2>/dev/null \\
           | grep -Eq '^#![[:space:]]*[^[:space:]]*(/|[[:space:]])(ba|da|k)?sh([[:space:]]|$)' \\
           && files+=("$f") ;;""",
     "      *) ;;"),
    # --- the arms added for the two MEASURED gaps (backend#2967) -----------
    ("the sed q arm never fires", AWK,
     '|| probe ~ /\\|&?[[:space:]]*sed[^|\\001]*q(',
     '|| (0 && probe ~ /\\|&?[[:space:]]*sed[^|\\001]*q('),
    ("the read arm never fires", AWK,
     '|| probe ~ /\\|&?[[:space:]]*([A-Za-z_][A-Za-z_0-9]*=[^[:space:]|\\001]*[[:space:]]+)*read(',
     '|| (0 && probe ~ /\\|&?[[:space:]]*([A-Za-z_][A-Za-z_0-9]*=[^[:space:]|\\001]*[[:space:]]+)*read('),
    # THE SED ARM'S TERMINATOR, which is the part that actually discriminates.
    # The mutation here used to loosen the script TOKEN and was reported
    # UNCAUGHT -- correctly, because the token shape changes nothing: the `q`
    # in `sed 's/a/q/'` is followed by `/`, which the terminator class rejects
    # either way. Drop the terminator and that substitution IS flagged, which
    # `sedsubst` catches. The measurement that settled it is in the awk.
    # TWO SEPARATE PROPERTIES OF THE SED TERMINATOR, because the first label
    # here was wrong: it said "stops requiring a terminator" while the diff
    # only dropped whitespace and EOL from the class. Caught, but by the
    # measured `sed q` rows rather than by the false-positive guard, so it
    # pinned the wrong half and said so in a misleading name.
    ("the sed terminator stops admitting whitespace and end-of-line", AWK,
     'sed[^|\\001]*q([[:space:]]|$|',
     'sed[^|\\001]*q('),
    # ...and the one that pins the DISCRIMINATION: with no terminator at all,
    # `sed 's/a/q/'` and `sed 'y/ab/qz/'` -- both measured non-members -- are
    # flagged, and `sedsubst` plus both measured rows redden.
    ("the sed arm drops its terminator entirely", AWK,
     'sed[^|\\001]*q([[:space:]]|$|[)"\'\\\'\'`;|&\\001])',
     'sed[^|\\001]*q'),
    # THE READ ARM'S PREFIX, and it takes TWO mutations because the arm makes
    # two claims that fail in opposite directions. It admits a run of
    # `VAR=value` assignments before `read` and nothing else, so it must catch
    # `| IFS= read -r` (a measured member) while still sparing `| while read`.
    # One mutation per direction; a single one would leave the other side
    # unpinned, which is how the sed terminator came to be mislabelled.
    #
    # DROP THE PREFIX: `| IFS= read -r line` stops being seen. Caught by that
    # row in CONSUMERS, which measures it a member at 260KB.
    ("the read arm stops admitting an assignment prefix, losing `IFS= read`", AWK,
     '([A-Za-z_][A-Za-z_0-9]*=[^[:space:]|\\001]*[[:space:]]+)*read(',
     'read('),
    # WIDEN THE PREFIX TO ANYTHING: `| while read` now reads as a hazard, which
    # is the opposite of this class -- the loop drains to EOF. Caught by the two
    # `| while read` / `| while IFS= read` spare assertions.
    ("the read arm's prefix admits any text, so `| while read` reads as a hazard", AWK,
     '|| probe ~ /\\|&?[[:space:]]*([A-Za-z_][A-Za-z_0-9]*=[^[:space:]|\\001]*[[:space:]]+)*read(',
     '|| probe ~ /\\|&?[^|\\001]*read('),

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

    # Refuse rather than measure against a baseline nothing vouches for. Only the
    # writing path: `--dry` writes nothing, so it has no restore to lose -- and it
    # is what `make check` runs on every push, where refusing on an uncommitted
    # edit would block the pre-push tier for whoever is editing the target.
    if not dry:
        rc = mutation_baseline.guard(ROOT, [AWK, SH])
        if rc:
            return rc

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
