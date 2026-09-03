#!/usr/bin/env python3
"""Mutation harness for the early-close gate (backend#2264).

WHY. `pipefail-early-close-selftest.sh` asserts the gate's behaviour; this
asserts the SELFTEST. Break a rule, watch the suite redden, restore. A case
that stays green under its own rule being deleted is vacuous, and a green
selftest log cannot tell you which of its cases are load-bearing.

THREE TARGETS, because the rule genuinely lives in three files. The `.awk`
decides which LINES are offenders; the `.sh` decides which FILES run under both
options (the inheritance fixpoint, the option-sign seed, the derived file list);
`pipefail-early-close-yaml.py` decides which YAML `run:` blocks are shell and
what options GitHub starts them with. A harness that only mutated the awk would
report full coverage while the wrapper's logic -- the half that made this gate
need a wrapper at all -- went unpinned.

AN `expect` FIELD PROVES WHICH CASE CAUGHT A MUTATION, and it exists for one
reason: backend#1729 rule 9. The YAML path is only worth anything if it is
judged by THE AWK UNDER TEST rather than by a second matcher living in the
extractor. "The suite went red" cannot tell those apart -- the shell cases alone
would redden any awk mutation while a copied YAML matcher sailed through. So the
awk-arm mutations below `expect` the f4d6fec YAML case by name: break the awk,
and the YAML regression MUST be among the failures. If the extractor ever grows
its own copy of the hazard rule, these mutations go from caught to WRONGLY
CAUGHT and say so.

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
YML = ROOT / "scripts" / "pipefail-early-close-yaml.py"
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


# (label, target, old, new[, expect]) -- `expect` is a substring that must
# appear among the case names that failed, not merely "something failed".
MUTATIONS = [
    # --- the hazard stops being detected ---------------------------------
    # `expect` NAMES THE YAML CASE: this is the rule-9 proof that the YAML
    # path calls the awk instead of copying it. The shell cases would redden
    # here regardless, which is exactly why "caught" alone is not evidence.
    ("the head arm never fires", AWK,
     'if (probe ~ /\\|&?[[:space:]]*head(', 'if (0 && probe ~ /\\|&?[[:space:]]*head(',
     "f4d6fec's literal"),
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
    # --- the YAML SCOPE, which is the hole the ticket found ----------------
    # THE ANCHOR CARRIES ITS NEIGHBOUR. The `*.yml|*.yaml` arm appears twice
    # -- once in the explicit-argument split, once in the derived classifier
    # -- and an anchor matching twice mutates an arbitrary one of them.
    ("YAML is not enumerated at all, so run blocks stay out of scope", SH,
     '      *.yml|*.yaml) is_workflow_yaml "$f" && yfiles+=("$f") ;;\n'
     '      *.bats|*.ps1|*.psm1|*.zsh) ;;',
     '      *.bats|*.ps1|*.psm1|*.zsh) ;;',
     "f4d6fec's literal"),
    # THE SCOPE PREDICATE ITSELF, because narrowing to the workflow paths is
    # what stopped three repos going rc 2 on every PR (@saadqbal) -- and a
    # predicate that answers `true` for everything silently restores that. The
    # arm above is still enumerated, so only the FILTER is gone: the mutation
    # is "offer every tracked YAML again", which is precisely the state the
    # review measured as red on client, backend and averaging-service.
    ("the workflow-path filter accepts every YAML, so Helm templates are rc 2",
     SH,
     '    action.yml|action.yaml|*/action.yml|*/action.yaml) return 0 ;;\n'
     '    *) return 1 ;;',
     '    action.yml|action.yaml|*/action.yml|*/action.yaml) return 0 ;;\n'
     '    *) return 0 ;;',
     "a Helm chart template is not a workflow and must not be parsed"),
    # ONE GLOB AT A TIME, because every whole-arm mutation above is satisfied by
    # a predicate that still admits ONE spelling -- and @saadqbal's gap was
    # precisely "losing `.yaml` while keeping `.yml`". The predicate lists SIX
    # globs, so there are six ways to lose exactly one, and each `expect` below
    # names a DIFFERENT case. That is the claim worth making: those cases are
    # not interchangeable coverage of one rule, each is the only evidence for
    # one glob. Written after the fixtures rather than before, because until
    # `hz.yaml` and the `action.y{a,}ml` spellings existed four of these six
    # could only ever have reported UNCAUGHT (rule 8).
    ("the workflow arm loses `.yaml`, keeping only `.yml`", SH,
     '    .github/workflows/*.yml|.github/workflows/*.yaml) return 0 ;;',
     '    .github/workflows/*.yml) return 0 ;;',
     "a .yaml workflow is discovered"),
    ("the workflow arm loses `.yml`, keeping only `.yaml`", SH,
     '    .github/workflows/*.yml|.github/workflows/*.yaml) return 0 ;;',
     '    .github/workflows/*.yaml) return 0 ;;',
     "f4d6fec's literal"),
    ("the composite arm loses the bare `action.yml`", SH,
     '    action.yml|action.yaml|*/action.yml|*/action.yaml) return 0 ;;',
     '    action.yaml|*/action.yml|*/action.yaml) return 0 ;;',
     "a composite action's action.yml is still scanned"),
    ("the composite arm loses the bare `action.yaml`", SH,
     '    action.yml|action.yaml|*/action.yml|*/action.yaml) return 0 ;;',
     '    action.yml|*/action.yml|*/action.yaml) return 0 ;;',
     "every composite spelling is in scope"),
    ("the composite arm loses `*/action.yml`", SH,
     '    action.yml|action.yaml|*/action.yml|*/action.yaml) return 0 ;;',
     '    action.yml|action.yaml|*/action.yaml) return 0 ;;',
     "every composite spelling is in scope"),
    ("the composite arm loses `*/action.yaml`", SH,
     '    action.yml|action.yaml|*/action.yml|*/action.yaml) return 0 ;;',
     '    action.yml|action.yaml|*/action.yml) return 0 ;;',
     "every composite spelling is in scope"),
    # THE BUG THIS FIX SHIPPED WITH, kept as a mutation because it is the exact
    # failure mode the gate exists to prevent: written outside the
    # substitution, the here-string feeds the ASSIGNMENT, the awk reads the
    # script's stdin, and every YAML finding vanishes at rc 0.
    ("the mapping here-string attaches to the assignment, dropping every finding", SH,
     '''END { if (bad) exit 3 }' "$MANIFEST" - <<<"$yout")''',
     '''END { if (bad) exit 3 }' "$MANIFEST" -) <<<"$yout"''',
     "f4d6fec's literal"),
    # NOT MUTATED: "an unmapped scanner row is dropped instead of refusing".
    # The branch is unreachable by construction, so a mutation of it can only
    # ever report UNCAUGHT and would train people to ignore the tier
    # (backend#1729 rule 8 -- say so with evidence rather than pin a path no
    # input reaches). The wrapper hands awk exactly the strings it read out of
    # the manifest, so `FILENAME` is always one of the manifest keys and the
    # `!(frag in real)` arm cannot fire. It stays in the code as a fail-closed
    # assertion about an internal invariant; it is not claimed as covered.
    ("a failing YAML extractor reads as 'no run blocks'", SH,
     '''  if ! python3 "$YAML_PROG" --out "$FRAG_DIR" "${yfiles[@]}"; then
    echo "pipefail-early-close: the YAML extractor failed — refusing to report clean" >&2
    exit 2
  fi''',
     '''  python3 "$YAML_PROG" --out "$FRAG_DIR" "${yfiles[@]}" || true'''),
    ("an unknown PIPEFAIL_SCOPE silently means 'all'", SH,
     '''  *) echo "pipefail-early-close: unknown PIPEFAIL_SCOPE '$SCOPE' (all|shell|yaml)" >&2; exit 2 ;;''',
     '''  *) SCOPE=all ;;'''),

    # --- the extractor's half: what the effective shell IS -----------------
    # EXPECT NAMES A CASE THAT ACTUALLY DEPENDS ON THIS MAPPING. It first named
    # the f4d6fec regression, and the harness reported MISCAUGHT -- correctly:
    # that fixture's body opens with `set -uo pipefail`, faithfully to the real
    # file, so the body re-arms what the mapping dropped. The rule-9 proof that
    # the YAML path calls the awk is the HEAD-ARM mutation above, which does
    # name f4d6fec and is caught by it.
    ("`shell: bash` loses pipefail", YML,
     '    "bash": "-eo pipefail",', '    "bash": "-e",',
     "'shell: bash' arms errexit AND pipefail"),
    ("every shell arms pipefail, so `shell: sh` invents hazards", YML,
     '    "sh": "-e",', '    "sh": "-eo pipefail",'),
    ("the default shell arms pipefail, which GitHub does not", YML,
     'DEFAULT_FLAGS = "-e"', 'DEFAULT_FLAGS = "-eo pipefail"'),
    # RE-ANCHORED: this block moved inside the `program in POSIX_SHELL_PROGRAMS`
    # branch for .github#404, so the old indentation no longer matched -- caught
    # by `--dry` as a stale anchor, which is what that pass is for.
    ("a custom command line is assumed to carry -e", YML,
     '        kept = []\n        for tok in tokens[1:]:',
     '        kept = ["-e"]\n        for tok in tokens[1:]:'),
    # THE MUTATION HAS TO ARM THE BLOCK, not merely stop excluding it. Emptying
    # NON_SHELL let `python` fall through to DEFAULT_FLAGS (`-e`) -- errexit
    # only, no pipefail -- so nothing was flagged and the mutation survived
    # while looking like coverage.
    # THE THREE BRANCHES OF THE UNKNOWN-SPEC PATH (Bugbot, .github#404), each
    # pinned by the case that depends on it.
    ("an unresolved ${{ }} expression falls back to the default -e", YML,
     '    if "${{" in spec:\n        return "-eo pipefail"',
     '    if False:\n        return "-eo pipefail"'),
    ("a command line is only recognised when it carries {0}", YML,
     '    if program in POSIX_SHELL_PROGRAMS:',
     '    if program in POSIX_SHELL_PROGRAMS and "{0}" in spec:'),
    ("a custom interpreter (perl, node) is armed as shell", YML,
     '    if "{0}" in spec:\n        return None',
     '    if False:\n        return None'),
    ("a non-POSIX shell (python, pwsh) is scanned as armed shell", YML,
     '    if spec in NON_SHELL:\n        return None',
     '    if spec in NON_SHELL:\n        return "-eo pipefail"'),
    # THE `env` PEEL, pinned in BOTH directions. It shipped with no mutation
    # and no selftest case, so nothing proved it caught anything; and a peel
    # can fail two opposite ways -- not peeling (the original skip) and peeling
    # past the shell (`sudo bash` read as bash). One mutation per direction,
    # each named by the case that depends on it.
    ("a wrapped `/usr/bin/env bash` is skipped entirely", YML,
     '    while i < len(tokens) and os.path.basename(tokens[i]) == "env":',
     '    while False:',
     "'/usr/bin/env bash -eo pipefail {0}' IS armed, not skipped"),
    ("the peel walks past ANY program, so `sudo bash` reads as bash", YML,
     '    while i < len(tokens) and os.path.basename(tokens[i]) == "env":',
     '    while i < len(tokens) and os.path.basename(tokens[i]) != "\x00":',
     "'sudo bash -eo pipefail {0}' is NOT treated as plain bash"),
    # And the value-flag list, which is wrong in both directions too: `-S`
    # takes no separate value (its argument is the rest of the command), while
    # `-C DIR` does.
    ("`-S` eats the program, so `env -S bash` is skipped", YML,
     '            if char == "S":\n                return False',
     '            if char == "\x00":\n                return False',
     "'env -S bash -eo pipefail {0}' IS armed (-S takes no separate value)"),
    ("`-C` stops consuming its DIR, so the DIR becomes the program", YML,
     '    _VALUE_SHORT = "uC"',
     '    _VALUE_SHORT = ""',
     "'env -C /tmp bash -eo pipefail {0}' IS armed (-C consumes its DIR)"),
    # AND THE CLUSTER RULE ITSELF (Bugbot Medium, .github#404). `-iu` is not the
    # token `-u`; the exact-token match that preceded this consumed one token,
    # `HOME` became the program, and the step was SKIPPED with its `| head`
    # unreported. Mutating back to that match is mutating back to the bug.
    ("a clustered value flag is read as a no-value flag, losing the program", YML,
     '            i += 2 if _consumes_next(tokens[i]) else 1',
     '            i += 2 if tokens[i] in ("-u", "--unset", "-C", "--chdir") else 1',
     "...and a CLUSTERED value flag, `env -iu HOME bash \u2026`"),
    # The attached form is the other half of the same decision: `-iuHOME`
    # carries its own value, so consuming the next token eats the program.
    ("an ATTACHED cluster value consumes the next token too", YML,
     '                return pos == len(body) - 1',
     '                return True',
     "...and an ATTACHED value, `env -iuHOME bash \u2026`, which consumes nothing"),
    ("composite-action `runs.steps` are not walked", YML,
     '''    runs = _mapping_get(root, "runs")
    if isinstance(runs, yaml.MappingNode):''',
     '''    runs = None
    if isinstance(runs, yaml.MappingNode):'''),
    # The jobs mapping is iterated, so it needs the merge-aware ITEMS helper --
    # `_mapping_get` being merge-aware does nothing for a key nobody looks up.
    ("the jobs mapping is iterated directly, skipping merged-in jobs", YML,
     "        for _name, job in _mapping_items(jobs):",
     "        for _name, job in jobs.value:"),
    # ONLY THE LAST `<<:` IS SEARCHED -- the state this shipped in, and the
    # one `_mapping_items` never shared (Bugbot Medium, #404).
    ("`_mapping_get` keeps only the last merge key", YML,
     "            merges.append(v)\n    # Document order, matching `_mapping_items`",
     "            merges = [v]\n    # Document order, matching `_mapping_items`",
     "a step whose shell/run arrive via the FIRST of two '<<:' keys IS scanned"),
    ("the job/workflow `defaults.run.shell` layer is ignored", YML,
     '            job_default = _defaults_shell(job) or workflow_default',
     '            job_default = None'),
    ("a single-line `run:` yields an empty body and is skipped", YML,
     '''        body = [_scalar(node) or ""]
        body_first = first + 1''',
     '''        body = []
        body_first = first + 1'''),
    ("the body offset is off by one, so findings point at the wrong line", YML,
     '        body_first = first + 2            # 1-based line of the body\'s line 1',
     '        body_first = first + 1'),
    ("unparseable YAML is skipped instead of refusing", YML,
     '''            sys.stderr.write(f"pipefail-early-close-yaml: cannot parse {path}: {first} — "
                             "refusing to report clean\\n")
            return 2''',
     '''            continue'''),
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
        rc = mutation_baseline.guard(ROOT, [AWK, SH, YML])
        if rc:
            return rc

    pristine = {t: t.read_text(encoding="utf-8") for t in (AWK, SH, YML)}
    stale, uncaught, miscaught = [], [], []

    for entry in MUTATIONS:
        label, target, old, new = entry[:4]
        expect = entry[4] if len(entry) > 4 else None
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
            # stdin IS /dev/null, and one of the mutations below makes that
            # load-bearing: "the mapping here-string attaches to the
            # assignment" leaves an `awk … -` reading the INHERITED stdin.
            # Run from an interactive shell, that awk blocks on the TTY and
            # the harness hangs instead of reporting. Same trap caller-drift.py
            # documents for `gh`.
            run = subprocess.run(["bash", str(SUITE)], capture_output=True,
                                 text=True, cwd=ROOT, stdin=subprocess.DEVNULL)
        finally:
            # ALWAYS restore, including on a crash. A mutation left on disk makes
            # every later run measure the wrong script, and the tell is a suite
            # that reddens for reasons nobody typed.
            target.write_text(pristine[target], encoding="utf-8")
        m = re.search(r"(\d+) passed, (\d+) failed", run.stdout)
        failed = int(m.group(2)) if m else (1 if run.returncode else 0)
        caught = [line.split("  ", 2)[-1].strip()
                  for line in run.stdout.splitlines() if line.startswith("FAIL  ")]
        if failed > 0 and expect and not any(expect in c for c in caught):
            # Red for the WRONG REASON. The mutation broke the shared rule, the
            # suite noticed via some other case, and the case that was supposed
            # to depend on this rule did not. For the YAML cases that means the
            # extractor has grown its own matcher -- rule 9, exactly.
            miscaught.append((label, expect, caught))
            print(f"  MISCAUGHT  {label}\n             expected `{expect}` to fail; "
                  f"got: {', '.join(caught)[:90]}")
        elif failed > 0:
            print(f"  caught     {label}\n             by: {', '.join(caught)[:110]}")
        else:
            uncaught.append(label)
            print(f"  UNCAUGHT   {label}")

    for target, text in pristine.items():
        if target.read_text(encoding="utf-8") != text:
            sys.stderr.write(f"::error::{target.name} was left mutated. Restore it from git.\n")
            return 2

    print(f"\n{len(MUTATIONS)} mutation(s): {len(stale)} stale, {len(uncaught)} uncaught, "
          f"{len(miscaught)} miscaught")
    for label, why in stale:
        sys.stderr.write(f"::error::STALE mutation `{label}`: {why}\n")
    for label in uncaught:
        sys.stderr.write(
            f"::error::UNCAUGHT `{label}`: the suite passed with this broken. Add a "
            "case that fails under it, or delete the mutation and say why it is not "
            "worth pinning.\n")
    for label, expect, caught in miscaught:
        sys.stderr.write(
            f"::error::MISCAUGHT `{label}`: the suite reddened, but `{expect}` was not "
            f"among the failures ({', '.join(caught)[:90]}). The case that should depend "
            "on this rule does not — most likely a second copy of the rule (rule 9).\n")
    return 1 if (stale or uncaught or miscaught) else 0


if __name__ == "__main__":
    raise SystemExit(main())
