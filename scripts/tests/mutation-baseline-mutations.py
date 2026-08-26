#!/usr/bin/env python3
"""Mutation harness for the mutation harnesses' baseline guard (backend#2441).

WHY. `mutation-baseline-selftest.py` asserts the guard's behaviour; this asserts
the SELFTEST. Break the guard, watch the suite redden, restore. The guard exists
because a harness can report `0 uncaught` while measuring nothing, so a suite
that would not notice the guard being disabled is the same failure one level up.

WHAT IS MUTATED: every arm that could turn a refusal back into a pass -- the
dirty file, the comparison being against the index rather than HEAD, each
cannot-tell arm, and the wording the operator is supposed to act on.

Every anchor must match EXACTLY ONCE, and this runner guards its own baseline
with the very function it mutates.

  mutation-baseline-mutations.py          run them all
  mutation-baseline-mutations.py --dry    resolve anchors only
"""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOD = ROOT / "scripts" / "tests" / "mutation_baseline.py"
SUITE = ROOT / "scripts" / "tests" / "mutation-baseline-selftest.py"

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402

# (label, old, new)
MUTATIONS = [
    # --- the ticket's own bug: a corrupted file is adopted as the baseline ---
    ("a file that differs from HEAD is accepted",
     "        if rc == 0:\n            continue\n        if rc == 1:",
     "        if rc in (0, 1):\n            continue\n        if rc == 1:"),
    ("the comparison is against the INDEX, so a STAGED mutation reads as clean",
     '        rc, _ = _git(root, ["diff", "--quiet", "HEAD", "--", rel])',
     '        rc, _ = _git(root, ["diff", "--quiet", "--", rel])'),

    # --- cannot-tell turned back into a pass, one arm at a time -------------
    ("a git that cannot be run reports a clean comparison",
     '    except OSError as exc:\n        raise DirtyBaseline(\n'
     '            "`git %s` could not be run',
     '    except OSError as exc:\n        return 0, ""\n        raise DirtyBaseline(\n'
     '            "`git %s` could not be run'),
    ("a repository with no HEAD is measured anyway",
     '    if rc != 0:\n        raise DirtyBaseline(\n            "`git rev-parse HEAD` failed',
     '    if rc != 0:\n        pass\n    if False:\n        raise DirtyBaseline(\n'
     '            "`git rev-parse HEAD` failed'),
    ("an untracked target is accepted, because git diffs it against nothing",
     '        if rc != 0:\n            raise DirtyBaseline(\n'
     '                "%s is not tracked by git',
     '        if False:\n            raise DirtyBaseline(\n'
     '                "%s is not tracked by git'),
    ("an unreadable target is never opened, so the read never fails",
     "        try:\n            path.read_bytes()\n        except OSError as exc:",
     "        try:\n            pass\n        except OSError as exc:"),
    ("a target outside the repository is compared anyway",
     '        except ValueError:\n            raise DirtyBaseline(\n'
     '                "%s is outside %s',
     '        except ValueError:\n            rel = path.as_posix()\n            _unused = (\n'
     '                "%s is outside %s'),
    ("an unexpected git exit status is treated as a match",
     '        raise DirtyBaseline(\n            "`git diff --quiet HEAD -- %s` exited %d',
     '        continue\n        raise DirtyBaseline(\n'
     '            "`git diff --quiet HEAD -- %s` exited %d'),

    # --- the refusal stops being actionable ---------------------------------
    # A guard that refuses without saying which file, or without saying what to
    # do, is a guard people work around. Both are asserted, so both are pinned.
    ("the refusal no longer tells the operator how to restore the file",
     "                \"  Restore it with `git checkout -- %s`, or commit it if the \"",
     "                \"  Restore it somehow, or commit it if the \""),
    ("guard() reports success on a baseline it just refused",
     '        sys.stderr.write("::error::%s\\n" % exc)\n        return 2',
     '        sys.stderr.write("::error::%s\\n" % exc)\n        return 0'),
    ("an unexpected failure inside the check escapes instead of refusing",
     "    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see below",
     "    except ValueError as exc:  # noqa: BLE001 -- deliberately broad, see below"),
    ("guard()'s refusal is no longer an Actions annotation",
     '        sys.stderr.write("::error::%s\\n" % exc)',
     '        sys.stderr.write("%s\\n" % exc)'),
]


def _drop_bytecode_cache():
    """Remove any cached bytecode for the module. A stale pyc makes a caught
    mutation report as uncaught."""
    try:
        cached = importlib.util.cache_from_source(str(MOD))
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

    # This runner mutates the guard, so it guards itself with it. A previous run
    # killed mid-mutation would otherwise leave a broken guard on disk and this
    # would measure the suite against it -- the exact defect, one level up.
    if not dry:
        rc = mutation_baseline.guard(ROOT, [MOD])
        if rc:
            return rc

    pristine = MOD.read_text(encoding="utf-8")
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
            print("  anchor ok  %s" % label)
            continue
        MOD.write_text(mutated, encoding="utf-8")
        _drop_bytecode_cache()
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            run = subprocess.run(
                [sys.executable, "-B", str(SUITE)],
                capture_output=True, text=True, cwd=str(ROOT), env=env,
            )
        finally:
            # ALWAYS restore, including on a crash -- and see the guard above for
            # the runs where "always" is not true.
            MOD.write_text(pristine, encoding="utf-8")
            _drop_bytecode_cache()
        caught = [line.split("  ", 1)[-1].strip()
                  for line in run.stdout.splitlines() if line.startswith("FAIL  ")]
        m = re.search(r"(\d+) passed, (\d+) failed", run.stdout)
        failed = int(m.group(2)) if m else 0
        # A crash counts as caught ONLY if the suite actually ran and reported. A
        # bare traceback means the mutation broke the harness rather than being
        # detected by a case, which is not coverage.
        reported = "mutation-baseline-selftest:" in run.stdout and m is not None
        if reported and failed > 0:
            print("  caught     %s\n             by: %s" % (label, ", ".join(caught)[:140]))
        elif not reported:
            uncaught.append((label, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % label)
        else:
            uncaught.append((label, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % label)

    if MOD.read_text(encoding="utf-8") != pristine:
        sys.stderr.write("::error::%s was left mutated. Restore it from git.\n" % MOD.name)
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
