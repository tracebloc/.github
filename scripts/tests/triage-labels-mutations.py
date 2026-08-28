#!/usr/bin/env python3
"""Mutation harness for the triage-label existence check (tracebloc/backend#2598).

`triage-labels-selftest.py` asserts the check's behaviour; this asserts the
SELFTEST. Break a rule in the real artefact, watch the suite redden, restore. A
case that stays green under its own rule being deleted is vacuous, and a green
log cannot tell you which of the suite's assertions are load-bearing.

THE MUTATION EDITS THE CODE UNDER TEST (CLAUDE.md rule 9). Every anchor below
lands in `scripts/triage-labels-check.py` or in `org-standards.md` -- the files
the suite reads -- and the suite is then re-run against them. There is no second
copy of the rule anywhere in here. The alternative shape, re-implementing the
rule inline and mutating the copy, is indistinguishable from real coverage in a
log and has bitten this org twice (.github#114, #115).

EVERY ANCHOR MUST MATCH EXACTLY ONCE. An anchor that matches twice mutates an
arbitrary one, so an "uncaught" verdict is about the wrong line; an anchor that
matches zero times is stale and fails the run exactly like an uncaught mutation.
That is the assertion that the anchor ACTUALLY APPLIED -- otherwise an inert
mutation and good coverage look identical (CLAUDE.md rule 5). `--dry` resolves
every anchor without running the suite, which is what belongs in the fast tier:
it catches the way this file really breaks, a refactor moving a line an anchor
matched on.

  triage-labels-mutations.py          run them all
  triage-labels-mutations.py --dry    resolve anchors only

WHAT IS DELIBERATELY NOT MUTATED, stated because an unstated gap is how a suite
comes to be trusted for more than it proves: the LIVE fleet read. `main()`'s
20-repo sweep is driven by no case -- the suite stubs `gh` and drives `audit()`
with an injected reader instead -- so a mutation to the live path would report
UNCAUGHT for a reason about the suite's scope rather than its rigour. The
comparison itself IS covered, because `audit()` is the one function both the
live path and the suite call.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "triage-labels-check.py"
CANON = ROOT / "org-standards.md"
SUITE = ROOT / "scripts" / "tests" / "triage-labels-selftest.py"

# dont_write_bytecode BEFORE the import, deliberately: `selftests-cover` rejects
# anything under scripts/tests/ that is not a suite or a runner, and a
# `__pycache__/` left by this import is exactly that.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402


# (label, file, old, new)
MUTATIONS = [
    # --- the fleet scope ----------------------------------------------------
    ("an EXEMPT repo is audited as if enrolled, so an exemption changes the verdict "
     "instead of the scope",
     GUARD,
     '        if callers.get(reusable) == "required":',
     '        if reusable in callers:'),
    ("a repo that left the scope is no longer reported, so the audit can shrink "
     "silently",
     GUARD,
     '        if reusable not in callers:\n'
     '            out.append(f"{name}: no `{reusable}` entry at all")',
     '        if reusable not in callers:\n'
     '            continue'),
    ("an inventory with no `repos:` mapping is accepted, so an empty fleet passes",
     GUARD,
     '    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):',
     '    if False:'),
    ("ZERO enrolled repos becomes a pass instead of a refusal — the vacuous green "
     "this whole file exists to refuse",
     GUARD,
     '        if not repos:\n'
     '            raise CannotTell("ZERO repos declare the caller',
     '        if False:\n'
     '            raise CannotTell("ZERO repos declare the caller'),
    ("the reusable no longer has to be inventory-declared, so the check may audit "
     "against a workflow the fleet does not call",
     GUARD,
     '        if REUSABLE not in (inv.get("reusables") or []):',
     '        if False:'),

    # --- the caller derivation ---------------------------------------------
    ("the `*-label` suffix becomes a prefix, so no input contributes and the "
     "domain silently loses the labels the workflow keys on",
     GUARD,
     '            if k.endswith(LABEL_INPUT_SUFFIX)}',
     '            if k.startswith(LABEL_INPUT_SUFFIX)}'),
    ("a `*-label` input with no default is skipped instead of refused — rule 6's "
     "vocabulary gap, shrinking the domain to whatever happened to have a default",
     GUARD,
     '            raise CannotTell(f"{path.name}: input `{name}` names a label but '
     'has no "',
     '            continue  # noqa\n            raise CannotTell(f"{path.name}: '
     'input `{name}` names a label but has no "'),
    ("the `--add-label` write stops contributing, so the label the workflow "
     "ADDS is not asserted to exist",
     GUARD,
     '    for label in sorted(set(ADD_LABEL.findall(run_scripts(doc)))):',
     '    for label in []:'),
    ("caller_labels stops refusing on zero `*-label` inputs, so it returns the "
     "`--add-label` half as if it were the whole domain",
     GUARD,
     '    if not inputs:\n'
     '        # REFUSES ON ITS OWN',
     '    if False:\n'
     '        # REFUSES ON ITS OWN'),
    ("ADD_LABEL only matches the `=` form, so `--add-label priority` is invisible",
     GUARD,
     r'ADD_LABEL = re.compile(r"--add-label[= ]+([A-Za-z0-9:_.\-]+)")',
     r'ADD_LABEL = re.compile(r"--add-label=([A-Za-z0-9:_.\-]+)")'),
    ("an unparseable workflow yields an empty document instead of refusing",
     GUARD,
     '        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}\n'
     '    except (OSError, yaml.YAMLError) as exc:\n'
     '        raise CannotTell(f"{path.name} could not be read or parsed: {exc}") from exc',
     '        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}\n'
     '    except (OSError, yaml.YAMLError):\n'
     '        return {}'),

    # --- the staleness guard ------------------------------------------------
    ("the stale-idiom guard stops noticing an `--add-label` it can no longer see, "
     "so the check reports a clean sweep of a subset",
     GUARD,
     '    if "--add-label" in scripts and not ADD_LABEL.findall(scripts):',
     '    if False:'),
    ("the stale-idiom guard stops noticing that no `*-label` input exists any more",
     GUARD,
     '    if not inputs:\n'
     '        out.append(f"{path.name} declares no',
     '    if False:\n'
     '        out.append(f"{path.name} declares no'),
    ("stale_idiom swallows an unreadable workflow and returns no finding",
     GUARD,
     '    except CannotTell as exc:\n'
     '        return [f"{path.name} is unreadable: {exc}"]\n'
     '    out = []',
     '    except CannotTell:\n'
     '        return []\n'
     '    out = []'),
    # --- the defect Bugbot found on .github#364 -----------------------------
    # Reverting each half to the RAW-TEXT shape it had. Both are satisfied by a
    # comment, and this file's own comments contain both strings -- so the guard
    # would have gone quiet about the real producer while looking identical in a
    # log. The cost is specific: the caller family loses `from:customer`, which no
    # template applies, and the check goes green over a dead `bump` rule.
    ("the `*-label` guard goes back to scanning raw TEXT, which a comment satisfies",
     GUARD,
     '        inputs = label_inputs(doc, path.name)\n'
     '    except CannotTell as exc:\n'
     '        return [f"{path.name} is unreadable: {exc}"]\n'
     '    if not inputs:',
     '        inputs = label_inputs(doc, path.name)\n'
     '    except CannotTell as exc:\n'
     '        return [f"{path.name} is unreadable: {exc}"]\n'
     '    if LABEL_INPUT_SUFFIX not in path.read_text(encoding="utf-8"):'),
    ("the `--add-label` guard goes back to scanning raw TEXT, which a comment satisfies",
     GUARD,
     '    scripts = run_scripts(doc)\n'
     '    if "--add-label" in scripts and not ADD_LABEL.findall(scripts):',
     '    scripts = path.read_text(encoding="utf-8")\n'
     '    if "--add-label" in scripts and not ADD_LABEL.findall(scripts):'),
    ("the DERIVATION goes back to the raw file, so a commented-out `--add-label` "
     "puts a label that does not exist under the fleet-wide assertion",
     GUARD,
     '    for label in sorted(set(ADD_LABEL.findall(run_scripts(doc)))):',
     '    for label in sorted(set(ADD_LABEL.findall(\n'
     '            path.read_text(encoding="utf-8")))):'),
    ("run_scripts stops stripping shell comment lines",
     GUARD,
     '                bodies.extend(ln for ln in body.splitlines()\n'
     '                              if not ln.strip().startswith("#"))',
     '                bodies.extend(body.splitlines())'),

    # --- the template derivation -------------------------------------------
    ("a missing template directory reports 'no template labels' instead of refusing",
     GUARD,
     '        raise CannotTell(f"{where} is not a directory',
     '        return {}  # noqa\n        raise CannotTell(f"{where} is not a directory'),
    ("ZERO template labels becomes 'nothing to check' instead of a broken matcher",
     GUARD,
     '    if not tmpl:\n        raise CannotTell("derived ZERO labels from the issue',
     '    if False:\n        raise CannotTell("derived ZERO labels from the issue'),
    ("a template that will not parse contributes nothing, silently",
     GUARD,
     '            raise CannotTell(f"{path.name} could not be read or parsed: {exc}") from exc\n'
     '        for label in (doc or {}).get("labels") or []:',
     '            continue\n'
     '        for label in (doc or {}).get("labels") or []:'),

    # --- the fleet read: the fail-open direction that matters ---------------
    ("an unreadable label list returns an EMPTY SET, so a 403 reads as 'this repo "
     "has no labels' — the backend#1415 failure, one layer along",
     GUARD,
     '        raise CannotTell(f"{org}/{repo}: label list unreadable "',
     '        return set()  # noqa\n        raise CannotTell(f"{org}/{repo}: label '
     'list unreadable "'),
    ("audit() folds an unreadable repo into 'complies' — the fail-open a green run "
     "cannot be distinguished from",
     GUARD,
     '        except CannotTell as exc:\n'
     '            unreadable[repo] = str(exc)\n'
     '            continue',
     '        except CannotTell:\n'
     '            continue'),
    ("the comparison is inverted: a repo is a finding for labels it HAS",
     GUARD,
     '        gap = sorted(set(wanted) - have)',
     '        gap = sorted(set(wanted) & have)'),
    ("a repo missing labels is no longer recorded, so every gap reports clean",
     GUARD,
     '        if gap:\n            missing[repo] = gap',
     '        if False:\n            missing[repo] = gap'),

    # --- the derivation is really tied to the WRITTEN rule ------------------
    # Both directions, because a derivation is only live if BOTH sides moving is a
    # finding: the producer drifting from the rule, and the rule being reworded out
    # from under the producer.
    ("the CANON renames the bug label and the workflow is not updated with it",
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

    # Refuse rather than measure against a baseline nothing vouches for
    # (backend#2441). Only the writing path: `--dry` writes nothing, and it is
    # what `make check` runs on every push, where refusing on an uncommitted edit
    # would block the pre-push tier for whoever is editing the target.
    if not dry:
        rc = mutation_baseline.guard(ROOT, [GUARD, CANON])
        if rc:
            return rc

    pristine = {p: p.read_text(encoding="utf-8") for p in (GUARD, CANON)}
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
        # bare traceback means the mutation broke the harness rather than being
        # detected by a case -- which is not coverage, and must not be logged as
        # if it were.
        reported = "triage-labels-selftest:" in run.stdout
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
            print("::error::%s was left mutated — restore it from git before "
                  "trusting any later run" % path)
            return 2

    if stale:
        print("\n::error::%d anchor(s) did not apply. A mutation that does not land "
              "is inert, and an inert mutation is indistinguishable from good "
              "coverage in this log (CLAUDE.md rule 5):" % len(stale))
        for label, why in stale:
            print("  - %s\n      %s" % (label, why))
    if uncaught:
        print("\n::error::%d mutation(s) went UNCAUGHT. The suite passed with the "
              "rule broken, so those assertions are vacuous — strengthen them "
              "rather than deleting the case:" % len(uncaught))
        for label, why in uncaught:
            print("  - %s\n      %s" % (label, why))
    if stale or uncaught:
        return 1

    verb = "resolved" if dry else "caught"
    print("\n%d/%d mutations %s." % (len(MUTATIONS), len(MUTATIONS), verb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
