#!/usr/bin/env python3
"""Suite for scripts/lint-targets-run-in-ci.py (.github#388).

The rule: every live audit `make lint` depends on must be reachable from a target
CI actually runs, OR be run directly by a dedicated job.

FIXTURES, NOT THIS REPO. The live tree is checked by `make lint-targets-run-in-ci`.
This checks that the RULE CATCHES -- and it has to be fixtures, because in this
repo every audit is covered one way or another, so the interesting branches have
no visible effect here. A mutation that blanket-passes the direct-run check went
GREEN against the real tree while being a real hole; only a fixture with an actual
orphan can see it.

INPUTS ARE WRITTEN DOWN INDEPENDENTLY OF THE MATCHER (CLAUDE.md rule 9's
corollary). Every Makefile and workflow below is a literal.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE.parent / "lint-targets-run-in-ci.py"

spec = importlib.util.spec_from_file_location("ltric", GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

RESULTS = []


def record(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))


def tree(makefile: str, workflows: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "Makefile").write_text(makefile, encoding="utf-8")
    wf = d / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, body in workflows.items():
        (wf / name).write_text(body, encoding="utf-8")
    return d


def wf_running(*targets: str) -> str:
    steps = "\n".join(f"      - run: make {t}" for t in targets)
    return "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n" + steps


def drive(d: Path) -> tuple[int, str]:
    """Run the guard on a fixture tree, capturing what it printed.

    The OUTPUT is captured, not just the exit code, because rule 10 applies to a
    guard as much as to a test: an unhandled traceback also exits non-zero, so an
    exit-code-only assertion cannot tell a REFUSAL from a CRASH. Three fail-closed
    mutations survived while this only checked the status.
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = guard.main(["lint-targets-run-in-ci.py", str(d)])
    except BaseException as exc:  # a crash is not a refusal
        return 99, f"CRASHED: {exc.__class__.__name__}: {exc}"
    return rc, buf.getvalue()


def case(name: str, makefile: str, workflows: dict[str, str], want: int,
         must_say: str = "") -> None:
    d = tree(makefile, workflows)
    rc, out = drive(d)
    ok = rc == want
    if ok and must_say:
        ok = must_say in out
    detail = f"exit {rc}, wanted {want}"
    if must_say:
        detail += f"; expected message {must_say!r} -> {'found' if must_say in out else 'MISSING'}"
    record(ok, name, detail)


# --- the escape this guard exists for --------------------------------------
case(
    "an audit only in `make lint` is a FINDING",
    "lint: ruffish myaudit\n\t@true\n"
    "myaudit:\n\t$(PYTHON) scripts/myaudit.py\n"
    "ruffish:\n\truffish --all\n"
    "selftests:\n\techo fixtures\n",
    {"ci.yml": wf_running("selftests")},
    1,
)

case(
    "the same audit, wired into a target CI runs, is clean",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\t$(PYTHON) scripts/myaudit.py\n"
    "selftests:\n\techo fixtures\n",
    {"ci.yml": wf_running("selftests", "myaudit")},
    0,
)

case(
    "reachable TRANSITIVELY through a target CI runs is clean",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\t$(PYTHON) scripts/myaudit.py\n"
    "bundle: myaudit\n\t@true\n",
    {"ci.yml": wf_running("bundle")},
    0,
)

case(
    "reachable through a $(VAR) prerequisite list is clean",
    "AUDITS := myaudit\n"
    "lint: $(AUDITS)\n\t@true\n"
    "myaudit:\n\t$(PYTHON) scripts/myaudit.py\n"
    "bundle: $(AUDITS)\n\t@true\n",
    {"ci.yml": wf_running("bundle")},
    0,
)

# --- the direct-run escape hatch, which is what makes ruff/shellcheck legal --
case(
    "an audit run DIRECTLY by its own job (not via make) is clean",
    "lint: shellcheckish\n\t@true\n" "shellcheckish:\n\tshellcheckish -S warning\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: shellcheckish -S warning\n"
    },
    0,
)

case(
    "an audit whose SCRIPT a job runs directly is clean",
    "lint: myaudit\n\t@true\n" "myaudit:\n\t$(PYTHON) scripts/myaudit.py\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: python3 scripts/myaudit.py\n"
    },
    0,
)

# THE ONE THAT MADE THE FIRST VERSION VACUOUS. `$(PYTHON) scripts/x.py` must not
# be satisfied by a workflow that merely mentions python. Without this case the
# interpreter leak is invisible: in the real repo every Python audit is wired, so
# blanket-passing the direct-run check has no visible effect.
case(
    "a job that merely INSTALLS python does not cover a python audit",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\t$(PYTHON) scripts/myaudit.py\n"
    "selftests:\n\techo fixtures\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: python -m pip install --quiet pyyaml\n"
        "      - run: make selftests\n"
    },
    1,
)

case(
    "a COMMENTED `make` invocation does not count as wiring",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\t$(PYTHON) scripts/myaudit.py\n"
    "selftests:\n\techo fixtures\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: |\n          # make myaudit\n          make selftests\n"
    },
    1,
)

# THE SCRIPT WINS OVER THE TOOL when both are present, and that precedence is
# load-bearing rather than tidy. Here the audit is `ruffish check <its own
# config>`, and CI runs `ruffish` on something else entirely. Treating the tool
# name as evidence would read that unrelated job as covering this audit -- which
# is how a specific invocation gets mistaken for "the tool runs somewhere".
case(
    "a job running the same TOOL on other files does not cover a scripted audit",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\truffish check scripts/myaudit-rules.py\n"
    "selftests:\n\techo fixtures\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: ruffish check src/\n"
        "      - run: make selftests\n"
    },
    1,
)

# --- Bugbot High on .github#388: recipe tokens are not evidence ------------
# `action-pins` contained `wf=...` and `shellcheck` contained `files=$(mktemp)`,
# so the leading-word rule produced the "tools" `wf` and `files` -- substrings of
# ordinary workflow text, which made those two audits UNCOVERABLE as orphans.
case(
    "a shell ASSIGNMENT in a recipe is not tool evidence",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\t@set -e; \\\n\t wf=.github/workflows/x.yml; \\\n\t files=$$(mktemp)\n"
    "selftests:\n\techo fixtures\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: |\n          wf=whatever\n          files=here\n"
        "      - run: make selftests\n"
    },
    1,
)

# Second round of the same finding: with the assignment fix in, a multi-line shell
# recipe still contributed the leading word of EVERY line, so evidence became
# ['git', 'rm', 'tr'] -- coreutils present in any workflow. Only the FIRST command
# identifies the audit.
case(
    "coreutils on LATER recipe lines are not tool evidence",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\t@set -e; \\\n\t git diff; \\\n\t tr -d x; \\\n\t rm -f /tmp/z\n"
    "selftests:\n\techo fixtures\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: |\n          git rev-parse HEAD\n          rm -f /tmp/a\n"
        "      - run: make selftests\n"
    },
    1,
)

case(
    "a job NAMED after the target covers it (identity, exact)",
    "lint: myaudit\n\t@true\n" "myaudit:\n\t@set -e; \\\n\t grep -q x y\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  myaudit:\n    name: myaudit\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: echo doing it\n"
    },
    0,
)

case(
    "the target name merely MENTIONED in a script body does not cover it",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\t@set -e; \\\n\t grep -q x y\n"
    "selftests:\n\techo fixtures\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  other:\n    name: other\n"
        "    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: echo 'see myaudit for details'\n"
        "      - run: make selftests\n"
    },
    1,
)

# --- each defence isolated -------------------------------------------------
# The three rules above (skip assignments / first command only / exclude
# coreutils) overlap, so each one alone survived its mutation: the other two
# covered for it. These fixtures isolate one rule each, which is what makes the
# mutation tier able to see them. Overlapping defences with no isolating test are
# indistinguishable from one defence.

case(
    "an assignment as the FIRST recipe line is skipped, not taken as the tool",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\tfiles=$$(mktemp); \\\n\t grep -q x y\n"
    "selftests:\n\techo fixtures\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo 'files everywhere'\n"
        "      - run: make selftests\n"
    },
    1,
)

case(
    "a NON-coreutil tool on a later recipe line is not evidence",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\tmytool --check; \\\n\t helm lint chart/\n"
    "selftests:\n\techo fixtures\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: helm lint chart/\n"
        "      - run: make selftests\n"
    },
    1,
)

case(
    "a coreutil as the FIRST command is not evidence either",
    "lint: myaudit\n\t@true\n"
    "myaudit:\n\tgit diff --exit-code\n"
    "selftests:\n\techo fixtures\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: git rev-parse HEAD\n"
        "      - run: make selftests\n"
    },
    1,
)

# --- fail closed -----------------------------------------------------------
case("a Makefile with NO lint target is a finding", "selftests:\n\techo hi\n",
     {"ci.yml": wf_running("selftests")}, 1, "no `lint` target found")
case("a lint target with NO prerequisites is a finding", "lint:\n\t@true\n",
     {"ci.yml": wf_running("lint")}, 1, "ZERO prerequisites")
case("workflows that invoke NO make target and no audit is a finding",
     "lint: myaudit\n\t@true\nmyaudit:\n\t$(PYTHON) scripts/myaudit.py\n",
     {"ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo nothing\n"},
     1)
case("`make` targets that do not exist in the Makefile is a finding",
     "lint: myaudit\n\t@true\nmyaudit:\n\t$(PYTHON) scripts/myaudit.py\n",
     {"ci.yml": wf_running("nosuchtarget")}, 1)

# These assert the MESSAGE, so a crash cannot stand in for a refusal.
d = Path(tempfile.mkdtemp())
rc, out = drive(d)
record(rc == 1 and "no Makefile at" in out,
       "a tree with no Makefile REFUSES by name (not a traceback)", f"exit {rc}: {out.strip()[:90]}")
(d / "Makefile").write_text("lint: a\n\t@true\na:\n\techo x\n")
rc, out = drive(d)
record(rc == 1 and "is not a directory" in out,
       "a tree with no .github/workflows REFUSES by name", f"exit {rc}: {out.strip()[:90]}")

# An unreadable workflow dir that exists but yields nothing parseable.
d2 = Path(tempfile.mkdtemp())
(d2 / "Makefile").write_text("lint: a\n\t@true\na:\n\techo x\n")
(d2 / ".github" / "workflows").mkdir(parents=True)
(d2 / ".github" / "workflows" / "empty.yml").write_text("name: x\non: push\njobs: {}\n")
rc, out = drive(d2)
record(rc == 1 and "NO `run:` content" in out,
       "workflows with no run: content REFUSES by name", f"exit {rc}: {out.strip()[:90]}")

# GENERIC is load-bearing on its own: an audit with NO script path, whose recipe
# is an inline interpreter call. Without the denylist, `python3` matches any
# workflow that so much as installs python, and the audit reads as covered.
case(
    "an inline `python3 -c` audit is NOT covered by a python install step",
    "lint: inlineaudit\n\t@true\n"
    "inlineaudit:\n\tpython3 -c 'import sys; sys.exit(0)'\n"
    "selftests:\n\techo fixtures\n",
    {
        "ci.yml": "name: ci\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: python3 -m pip install pyyaml\n"
        "      - run: make selftests\n"
    },
    1,
)

failed = [n for ok, n in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    for n in failed:
        print(f"  FAILED: {n}")
    sys.exit(1)
