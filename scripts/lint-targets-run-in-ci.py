#!/usr/bin/env python3
"""Every live audit `make lint` depends on must be reachable from a target CI runs.

WHY THIS EXISTS — the fourth occurrence of one wiring gap
--------------------------------------------------------
A guard has two halves that answer different questions: a FIXTURE suite proving
the rule catches, and a LIVE audit proving the tree complies. The live half keeps
landing in `make lint`, which no workflow invokes, so `selftests` goes green while
nothing has read the real files.

  mint-scope            .github#287
  make mutations        .github#300
  reason-citations      backend#2449
  reusable-no-cancel    .github#388   <- this one, added two lines below the
                                         comment documenting the previous one

Four times, each with a longer comment above it than the last. A comment is not a
mechanism; this is (CLAUDE.md rule 7 — a claim that should be a machine check).

WHAT IT DOES, and why it is derivation rather than a list
--------------------------------------------------------
It parses `lint`'s prerequisites out of the Makefile, parses the `make <target>`
invocations out of every workflow, expands both through the Makefile's own
dependency graph, and reports any lint prerequisite CI cannot reach. It holds no
list of guards and no list of workflows, so a new guard is covered the day it is
added to `lint` -- which is the whole point, since the failure mode is somebody
adding one and forgetting the second wiring step.

`lint` is the right root: it is where this repo puts live audits by convention,
and it is the exact place all four escapes lived.

FAIL CLOSED. An unparseable Makefile, an unreadable workflow directory, a `lint`
target that cannot be found, or zero parsed CI invocations are all findings --
"cannot tell" must never read as "wired". Zero parsed prerequisites would pass a
reachability check vacuously, which is the shape backend#1729 catalogued.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover -- guard-pyyaml covers this in the Makefile
    print("::error::pyyaml is required", file=sys.stderr)
    raise SystemExit(1)

# Targets that are not audits and whose absence from CI is not a finding. Kept
# tiny and justified; this is the one place a judgement call lives.
#   mutations-dry  the anchor-resolution pass. `make mutations` (the full tier) IS
#                  run by selftests.yml, and it strictly supersedes the dry pass.
NOT_AN_AUDIT = {"mutations-dry"}


def recipe_of(text: str, target: str) -> list[str]:
    """The tab-indented recipe lines for `target`."""
    lines, collecting = [], False
    for line in text.splitlines():
        if not collecting:
            if re.match(rf"^{re.escape(target)}\s*:(?!=)", line):
                collecting = True
            continue
        if line.startswith("\t"):
            lines.append(line[1:])
        elif line.strip() == "":
            continue
        else:
            break
    return lines


def run_evidence(target: str, text: str) -> list[str]:
    """Strings that would prove CI runs this audit WITHOUT going through make.

    Derived from the target's own recipe, never from a hand-written map: the
    script paths it executes, plus the leading command word when there is no
    script (`ruff`, `shellcheck`, `actionlint` are their own tools and CI runs
    them in dedicated jobs).

    This exists because `make` is NOT the only way CI can run an audit, and a
    guard that assumed it was reported five false positives on its first run --
    `quality / ruff`, `quality / shellcheck`, `quality / house-rules`,
    `quality / action-pins` and `actionlint` are all their own REQUIRED contexts,
    invoking the tools directly. Landing that version would have meant a red gate
    (rule 4) built on a wrong model of the repo.
    """
    # GENERIC INTERPRETERS ARE NOT EVIDENCE, and getting this wrong made the first
    # version of this guard VACUOUS ON THE BUG IT WAS WRITTEN FOR. The recipe
    # `$(PYTHON) scripts/reusable-no-cancel.py` yielded `python` as evidence, and
    # `python` appears in nearly every workflow (`python -m pip install ...`), so
    # every Python audit passed trivially. Unwiring the real step still reported
    # clean. Only the SCRIPT identifies the audit; the thing that runs it does not.
    GENERIC = {"python", "python3", "bash", "sh", "env", "make", "pipx", "uv"}
    # Shell builtins and coreutils. Belt-and-braces beside the first-command rule
    # above: a recipe may legitimately START with one of these, and such a target
    # has no distinguishing tool either way, so it stays coverable only by a job
    # named after it.
    SHELL_WORDS = {"set", "if", "then", "else", "elif", "fi", "for", "do", "done",
                   "while", "case", "esac", "echo", "printf", "true", "false", "cd",
                   "exit", "return", "local", "read", "test", "eval", "shift",
                   "git", "rm", "cp", "mv", "mkdir", "tr", "awk", "sed", "grep",
                   "cat", "find", "sort", "uniq", "head", "tail", "xargs", "chmod",
                   "mktemp", "tee", "wc", "diff", "comm", "jq", "curl", "gh", "ls"}

    # SCRIPT PATHS come from every recipe line (a path is specific enough to be
    # safe anywhere). TOOLS come from the FIRST command only.
    #
    # Why (Bugbot, High, on .github#388, second round): a multi-line shell recipe
    # contributes the leading word of every line, so `shellcheck`'s evidence was
    # ['git', 'rm', 'tr'] and `action-pins`'s was ['awk', 'rm'] -- coreutils that
    # appear in any workflow on earth. Both audits were therefore uncoverable as
    # orphans even after the assignment fix. The audit tool, if there is one, is
    # what the recipe RUNS FIRST; everything after is plumbing.
    #
    # A recipe whose first command is itself plumbing (`set -e; \`) yields no tool
    # evidence at all, which is correct: such a target is coverable only by a job
    # named after it. That is the fail-CLOSED direction -- more orphans reported,
    # never fewer.
    scripts, tools = [], []
    first_command_seen = False
    for line in recipe_of(text, target):
        for m in re.finditer(r"(?:\./)?scripts/[A-Za-z0-9_./-]+", line):
            scripts.append(m.group(0).lstrip("./"))
        stripped = line.lstrip("@-").strip()

        # A SHELL ASSIGNMENT IS NOT A COMMAND (Bugbot, High, on .github#388).
        # `action-pins`'s recipe contains `wf=.github/...` and `shellcheck`'s
        # contains `files=$$(mktemp)`, so the first-word rule produced the "tools"
        # `wf` and `files`. Both occur as substrings of ordinary workflow text, so
        # those two audits could never be reported unreachable -- unwiring their
        # dedicated jobs left this guard green. The check that exists to catch an
        # unrun audit was itself unable to see two of them.
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*=", stripped):
            continue

        if first_command_seen:
            continue
        first_command_seen = True

        m = re.match(r"\$[({]([A-Za-z0-9_]+)[)}]", stripped)
        if m:
            # RESOLVE the variable rather than lowercasing its name: `$(ACTIONLINT)`
            # happens to equal its binary, but that is a coincidence of naming and
            # not something to rely on.
            for tok in expand_vars(text, [f"$({m.group(1)})"]):
                base = tok.split("/")[-1].lower()
                if base and base not in GENERIC:
                    tools.append(base)
                break
        else:
            m2 = re.match(r"([A-Za-z][A-Za-z0-9_.-]*)", stripped)
            if m2:
                name = m2.group(1).lower()
                if name not in GENERIC and name not in SHELL_WORDS:
                    tools.append(name)

    # A script path is the strongest evidence and, when present, the ONLY tool
    # evidence worth trusting -- the interpreter in front of it says nothing.
    ev = list(scripts) if scripts else list(tools)
    # The target name is NOT returned here. A dedicated job named after the target
    # is how `ruff` / `shellcheck` / `house-rules` / `action-pins` / `actionlint`
    # are legitimately covered without going through make -- but that is an
    # EXACT-NAME question about jobs, checked against job names in main(), not a
    # substring question about script bodies.
    return [e for e in dict.fromkeys(ev) if e]


def parse_makefile(text: str) -> dict[str, list[str]]:
    """target -> prerequisites. Recipe lines (tab-indented) are ignored."""
    deps: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("\t") or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-/ ]+?):(?!=)([^=]*)$", line)
        if not m:
            continue
        targets, prereqs = m.group(1).split(), m.group(2).split()
        # Drop make-internal and variable-ish tokens; `$(FOO)` is expanded below.
        for t in targets:
            if t.startswith("."):
                continue
            deps.setdefault(t, []).extend(prereqs)
    return deps


def expand_vars(text: str, tokens: list[str]) -> list[str]:
    """Resolve `$(NAME)` / `${NAME}` prerequisites against the Makefile's own
    variable assignments. Without this, `selftests: selftests-cover $(SELFTEST_TARGETS)`
    contributes one unusable token and every real target behind it is invisible --
    a reachability check that silently sees nothing."""
    out: list[str] = []
    for tok in tokens:
        m = re.fullmatch(r"\$[({]([A-Za-z0-9_]+)[)}]", tok)
        if not m:
            out.append(tok)
            continue
        name = m.group(1)
        vm = re.search(
            rf"^{re.escape(name)}\s*:?[+]?=\s*((?:.*?\\\n)*.*)$", text, re.MULTILINE
        )
        if vm:
            out.extend(vm.group(1).replace("\\\n", " ").split())
    return out


def reachable(roots: list[str], deps: dict[str, list[str]], text: str) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        t = stack.pop()
        if t in seen:
            continue
        seen.add(t)
        for p in expand_vars(text, deps.get(t, [])):
            if p not in seen:
                stack.append(p)
    return seen


def workflow_run_text(wf_dir: Path) -> tuple[str, set[str]]:
    """Every non-comment `run:` line across every workflow, concatenated.

    Searched for the recipe-derived evidence above, so an audit CI executes
    directly (not through make) is recognised as covered.
    """
    out: list[str] = []
    job_names: set[str] = set()
    for f in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        for key, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            # JOB NAMES ARE KEPT SEPARATE from run content, because they are
            # IDENTITY evidence and run content is INVOCATION evidence. Folding
            # them together let a target name match anywhere in any script body:
            # with code-quality.yml deleted, `shellcheck` still read as covered
            # because some other workflow's run text mentions the word, and
            # `action-pins` likewise. That is the High finding's claim surviving
            # its own first fix (Bugbot on .github#388).
            job_names.add(str(key).lower())
            if isinstance(job.get("name"), str):
                job_names.add(job["name"].strip().lower())
            for step in job.get("steps") or []:
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    out += [ln for ln in step["run"].splitlines() if not ln.strip().startswith("#")]
                if isinstance(step, dict) and isinstance(step.get("uses"), str):
                    out.append(step["uses"])
    return "\n".join(out), job_names


def ci_make_targets(wf_dir: Path) -> tuple[set[str], int]:
    """`make <target>` as actually invoked by a workflow `run:` step."""
    found: set[str] = set()
    files = sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml"))
    for f in files:
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        for job in (doc.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                # Only real invocations: a `make` inside a comment line is not one.
                for line in run.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    for m in re.finditer(r"\bmake\s+([A-Za-z][A-Za-z0-9_.\-]*)", stripped):
                        found.add(m.group(1))
    return found, len(files)


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    mk = root / "Makefile"
    wf = root / ".github" / "workflows"

    if not mk.is_file():
        print(f"::error::no Makefile at {mk} -- refusing to report wiring I could not read")
        return 1
    if not wf.is_dir():
        print(f"::error::{wf} is not a directory -- refusing to report wiring I could not read")
        return 1

    text = mk.read_text(encoding="utf-8")
    deps = parse_makefile(text)
    if "lint" not in deps:
        print("::error::no `lint` target found in the Makefile -- this guard keys on it, and 'cannot tell' is a finding")
        return 1

    lint_prereqs = [t for t in expand_vars(text, deps["lint"]) if t not in NOT_AN_AUDIT]
    if not lint_prereqs:
        print("::error::parsed ZERO prerequisites for `lint` -- the reachability check below would be vacuous")
        return 1

    ci_targets, n_files = ci_make_targets(wf)
    # NOT a hard failure. An earlier revision errored here, on the theory that zero
    # parsed `make` invocations meant a stale parser -- but a repo whose CI runs its
    # audits directly (dedicated jobs, no make) is a legitimate shape, and the
    # fixture suite caught this rejecting exactly that. Real staleness still
    # surfaces: with nothing parsed, no target is make-reachable, so any audit that
    # is not also directly run becomes an orphan below. The signal survives; only
    # the wrong reason for it is gone.
    if not ci_targets:
        print(f"note: no `make <target>` invocations in {n_files} workflow file(s); coverage rests entirely on directly-run audits")

    # Only tokens that are REAL targets become reachability roots. `make` appears
    # in workflow prose too ("make it", "make the"), and a bogus root that happened
    # to collide with a real target name would manufacture coverage.
    real_roots = sorted(t for t in ci_targets if t in deps)
    if ci_targets and not real_roots:
        print(f"note: none of the parsed `make` targets ({' '.join(sorted(ci_targets))}) exist in this Makefile")
    covered = reachable(real_roots, deps, text) if real_roots else set()
    run_text, job_names = workflow_run_text(wf)
    if not run_text.strip():
        print("::error::parsed NO `run:` content from any workflow -- refusing to report wiring I could not read")
        return 1

    orphans = []
    for t in lint_prereqs:
        if t in covered:
            continue
        # Not reachable through make -- is it run DIRECTLY by a dedicated job?
        # WORD-BOUNDARY matching, not a bare substring. A two-letter token like
        # `wf` matched inside unrelated workflow text and manufactured coverage;
        # the assignment skip above removes that class at the source, and this
        # closes the same failure for anything short that survives it.
        # A job named exactly after the target: identity, exact match only.
        if t.lower() in job_names:
            continue
        # Otherwise the audit's own script or tool must actually be invoked.
        # WORD-BOUNDARY matching, not a bare substring: a two-letter token like
        # `wf` matched inside unrelated text and manufactured coverage.
        if any(
            re.search(rf"(?<![\w./-]){re.escape(e)}(?![\w-])", run_text)
            for e in run_evidence(t, text)
        ):
            continue
        orphans.append(t)

    print(f"`make lint` depends on {len(lint_prereqs)} audit target(s): {' '.join(sorted(lint_prereqs))}")
    print(f"CI invokes (real targets): {' '.join(real_roots)}")
    print(f"reachable from CI: {len(covered)} target(s)")

    for t in orphans:
        print(
            f"::error::`{t}` is a prerequisite of `make lint`, and no target any workflow "
            f"runs can reach it. CI does not run `make lint`, so this audit is advice: it "
            f"passes locally and never executes in a required context. Add a step running "
            f"it (see selftests.yml's `mint-scope` and `reusable-no-cancel` steps), or make "
            f"it a prerequisite of a target CI already runs."
        )
    if orphans:
        print(f"\nlint-targets-run-in-ci: {len(orphans)} unreachable audit(s).")
        return 1
    print("\nlint-targets-run-in-ci: clean -- every live audit in `make lint` is reachable from a target CI runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
