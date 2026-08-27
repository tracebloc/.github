#!/usr/bin/env python3
"""Offline self-test for scripts/triage-labels-check.py (tracebloc/backend#2598).

No network, no token: `gh` is replaced with a stub on PATH, and every derivation
runs against generated fixtures. What is asserted is the part that decides
whether the check is worth its green — the fail-closed paths — plus the two
derivations, because a guard that derives the wrong domain reports a clean sweep
of a subset and is indistinguishable from one that works.

THE SUITE DRIVES THE REAL FUNCTIONS. Nothing here re-implements the rule; every
case imports the module and calls it (CLAUDE.md rule 9). `triage-labels-mutations.py`
breaks the module and re-runs this file, which is only meaningful because of that.

Every refusal is asserted BY ITS MESSAGE, not by `assertRaises(Exception)`
(CLAUDE.md rule 10): this module has eight distinct refusal paths and a bare
"something raised" would let a case pass while exercising a different one.

Exit 0 when every path fails the way it is supposed to.
"""
from __future__ import annotations

import importlib.util
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
GUARD = ROOT / "scripts" / "triage-labels-check.py"
CANON = ROOT / "org-standards.md"

_spec = importlib.util.spec_from_file_location("triage_labels_check", GUARD)
if _spec is None or _spec.loader is None:
    sys.exit(f"cannot import {GUARD}")
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)

RESULTS: "list[tuple[bool, str, str]]" = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}\n        {detail}")


def refuses(name: str, fn, needle: str) -> None:
    """`fn` must raise CannotTell and the message must NAME the refusal."""
    try:
        fn()
    except chk.CannotTell as exc:
        if needle.lower() in str(exc).lower():
            record(True, name, f"refused with {str(exc)[:110]!r}")
        else:
            record(False, name, f"refused, but for the wrong reason: {str(exc)[:150]!r} "
                                f"(expected to mention {needle!r})")
    except Exception as exc:  # noqa: BLE001 - a non-CannotTell escape is a finding
        record(False, name, f"raised {type(exc).__name__} instead of CannotTell: {exc}")
    else:
        record(False, name, "did NOT refuse — a read that produced no answer was "
                            "reported as an answer")


def write(tmp: Path, name: str, body: str) -> Path:
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --------------------------------------------------------------- the constant

def run_main(tmp: Path, name: str, inventory_body: str) -> "subprocess.CompletedProcess":
    """Drive the REAL `main()` with a substituted inventory path.

    A subprocess, so `main()` runs exactly as CI runs it — same module, same
    entry point, same exit code — with only `INVENTORY` swapped. Nothing about
    the decision is re-implemented here (CLAUDE.md rule 9).
    """
    fake = write(tmp, name, inventory_body)
    return subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(f"""
            import importlib.util, pathlib, sys
            s = importlib.util.spec_from_file_location('c', r'{GUARD}')
            m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
            m.INVENTORY = pathlib.Path(r'{fake}')
            sys.exit(m.main())
        """)],
        capture_output=True, text=True, cwd=str(ROOT),
    )


def case_constant_is_real(tmp: Path) -> None:
    """The one name this file holds must point at a real, DECLARED reusable.

    A constant is the seam every "derived" check still has, so it is the seam
    worth pinning: a rename that left it dangling would make the whole audit
    refuse (good) or, if the refusal were ever softened, silently derive nothing.
    """
    path = ROOT / ".github" / "workflows" / chk.REUSABLE
    inv = chk.load_inventory()
    on_disk = path.is_file()
    declared = chk.REUSABLE in (inv.get("reusables") or [])
    record(on_disk and declared, "REUSABLE names a real, inventory-declared workflow",
           f"{chk.REUSABLE}: on disk={on_disk}, in `reusables:`={declared}")

    # And `main()` must refuse when it is NOT declared, rather than auditing the
    # fleet against a workflow the fleet is not contracted to call. Driven through
    # the real entry point: the previous shape of this case raised its own
    # exception from a local helper and proved nothing about the module.
    proc = run_main(tmp, "undeclared-inv.yml", f"""\
        schema_version: 1
        org: tracebloc
        reusables:
          - some-other-reusable.yml
        repos:
          a:
            callers:
              {chk.REUSABLE}: required
    """)
    ok = proc.returncode == 2 and "is not in the inventory's `reusables:`" in proc.stdout
    record(ok, "main() refuses a reusable the inventory does not declare",
           f"exit={proc.returncode}; named the reason="
           f"{chr(96) + 'reusables:' + chr(96) in proc.stdout}")


# ----------------------------------------------------------------- the fleet

def case_enrolment(tmp: Path) -> None:
    inv = {
        "org": "tracebloc",
        "reusables": [chk.REUSABLE],
        "repos": {
            "yes-a": {"callers": {chk.REUSABLE: "required"}},
            "yes-b": {"callers": {chk.REUSABLE: "required"}},
            "exempt-one": {"callers": {chk.REUSABLE: {"exempt": "a written reason"}}},
            "no-entry": {"callers": {"other.yml": "required"}},
        },
    }
    got = chk.enrolled_repos(inv)
    record(got == ["yes-a", "yes-b"], "only `required` repos are enrolled",
           f"enrolled={got} (an exempt or absent entry must not be audited)")

    skipped = chk.not_enrolled(inv)
    named = all(any(r in row for row in skipped) for r in ("exempt-one", "no-entry"))
    record(named and len(skipped) == 2,
           "a repo that left the scope is REPORTED, never silent",
           f"not_enrolled={skipped}")

    refuses("an inventory with no `repos:` mapping is a refusal",
            lambda: chk.load_inventory(write(tmp, "bad-inv.yml", "org: tracebloc\n")),
            "no `repos:`")
    refuses("an unparseable inventory is a refusal",
            lambda: chk.load_inventory(write(tmp, "broken.yml", "repos: [\n")),
            "could not be read or parsed")
    refuses("a missing inventory is a refusal",
            lambda: chk.load_inventory(tmp / "does-not-exist.yml"),
            "could not be read or parsed")


# ------------------------------------------------------------ the derivations

GOOD_WF = """\
    name: fixture
    on:
      workflow_call:
        inputs:
          trigger-label:
            type: string
            default: "from:customer"
          bug-label:
            type: string
            default: "work-type:bug"
          project-number:
            type: number
            default: 2
    jobs:
      bump:
        runs-on: ubuntu-latest
        steps:
          - run: |
              gh issue edit "$N" --add-label priority
              gh issue edit "$N" --add-label "$INTERPOLATED"
"""


def case_caller_derivation(tmp: Path) -> None:
    wf = write(tmp, "good.yml", GOOD_WF)
    got = chk.caller_labels(wf)
    want = {"from:customer", "work-type:bug", "priority"}
    record(set(got) == want, "both caller idioms are derived",
           f"derived {sorted(got)} — the two `*-label` defaults plus the "
           "`--add-label` write, and the interpolated one skipped")
    record("project-number" not in "".join(got),
           "a non-label input contributes nothing",
           "`project-number: 2` is not a label and must not enter the domain")

    # A THIRD label rule is covered the day it lands — the point of deriving over
    # the suffix instead of over two known input names.
    wf3 = write(tmp, "three.yml", GOOD_WF.replace(
        '          project-number:\n            type: number\n            default: 2\n',
        '          escalation-label:\n            type: string\n            default: "sev1"\n'))
    got3 = chk.caller_labels(wf3)
    record("sev1" in got3, "a NEW `*-label` input joins the domain automatically",
           f"derived {sorted(got3)}")

    refuses("a `*-label` input with no default is a refusal, not a silent skip",
            lambda: chk.caller_labels(write(tmp, "nodefault.yml", GOOD_WF.replace(
                '          bug-label:\n            type: string\n            '
                'default: "work-type:bug"\n',
                '          bug-label:\n            type: string\n'))),
            "no string default")
    refuses("an unparseable workflow is a refusal",
            lambda: chk.caller_labels(write(tmp, "wfbroken.yml", "on: [\n")),
            "could not be read or parsed")
    refuses("a missing workflow is a refusal",
            lambda: chk.caller_labels(tmp / "gone.yml"),
            "could not be read or parsed")


def case_stale_idiom(tmp: Path) -> None:
    clean = chk.stale_idiom(write(tmp, "s-ok.yml", GOOD_WF))
    record(clean == [], "a workflow whose idioms match reports no staleness",
           f"stale_idiom={clean}")

    # The label is still written, but with a flag the matcher cannot see. Without
    # this assertion the domain silently loses `priority` and the run stays green.
    blinded = GOOD_WF.replace("--add-label priority", "--add-label\\\n priority")
    got = chk.stale_idiom(write(tmp, "s-blind.yml", blinded))
    record(any("ADD_LABEL matched nothing" in r for r in got),
           "an `--add-label` the matcher can no longer see is a finding",
           f"stale_idiom={got}")

    no_inputs = chk.stale_idiom(write(tmp, "s-noinputs.yml",
                                      "name: x\non: push\njobs: {}\n"))
    record(any("declares no" in r for r in no_inputs),
           "a workflow with no `*-label` input at all is a finding",
           f"stale_idiom={no_inputs}")

    unreadable = chk.stale_idiom(tmp / "s-absent.yml")
    record(any("unreadable" in r for r in unreadable),
           "an unreadable workflow is a finding here too",
           f"stale_idiom={unreadable}")


def case_template_derivation(tmp: Path) -> None:
    d = tmp / "templates"
    write(d, "bug.yml", 'name: Bug\nlabels: ["work-type:bug"]\nbody: []\n')
    write(d, "feature.yml",
          'name: Feature\nlabels: ["work-type:feature", "needs-refinement"]\nbody: []\n')
    write(d, "config.yml", "blank_issues_enabled: false\n")
    got = chk.template_labels(d)
    record(set(got) == {"work-type:bug", "work-type:feature", "needs-refinement"},
           "every template label is derived, and config.yml contributes nothing",
           f"derived {sorted(got)}")

    refuses("a missing template directory is a refusal",
            lambda: chk.template_labels(tmp / "no-such-dir"),
            "not a directory")
    d2 = tmp / "badtemplates"
    write(d2, "bug.yml", "labels: [\n")
    refuses("an unparseable template is a refusal",
            lambda: chk.template_labels(d2),
            "could not be read or parsed")

    empty = tmp / "emptytemplates"
    empty.mkdir()
    refuses("ZERO template labels is a refusal, not 'nothing to check'",
            lambda: chk.required_labels(write(tmp, "rl.yml", GOOD_WF), empty),
            "derived zero labels from the issue templates")


# ------------------------------------------------------- the live derivation

def case_live_domain_matches_the_written_rule(tmp: Path) -> None:
    """The derived domain must contain the label the CANON names.

    Written down independently of the matcher, from `org-standards.md` — not by
    iterating the module's own output, which would be self-consistent and
    therefore blind (CLAUDE.md rule 9 corollary). If the canon renames the label
    and the workflow is not updated with it, this reddens.
    """
    text = CANON.read_text(encoding="utf-8")
    m = re.search(r"label them `([^`]+)`", text)
    if not m:
        record(False, "the canon still states the bug-label rule",
               "no 'label them `X`' sentence in org-standards.md — the derivation "
               "has nothing independent to be checked against")
        return
    canon_label = m.group(1)
    live = chk.required_labels()
    record(canon_label in live,
           "the label the CANON names is in the derived domain",
           f"canon says {canon_label!r}; derived domain = {sorted(live)}")


# ------------------------------------------------------------ the API read

def gh_stub(tmp: Path, body: str, rc: int = 0) -> "dict[str, str]":
    """A `gh` on PATH. Exercises the real subprocess path in `read_labels`."""
    binroot = tempfile.mkdtemp(dir=tmp)
    path = Path(binroot) / "gh"
    path.write_text("#!/bin/sh\n" + body + f"\nexit {rc}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return {**os.environ, "PATH": f"{binroot}{os.pathsep}{os.environ['PATH']}"}


def with_path(env, fn):
    old = os.environ.get("PATH")
    os.environ["PATH"] = env["PATH"]
    try:
        return fn()
    finally:
        if old is None:
            del os.environ["PATH"]
        else:
            os.environ["PATH"] = old


def case_read_labels(tmp: Path) -> None:
    ok = gh_stub(tmp, 'printf "work-type:bug\\npriority\\n"')
    got = with_path(ok, lambda: chk.read_labels("tracebloc", "r"))
    record(got == {"work-type:bug", "priority"}, "a successful read returns the names",
           f"read {sorted(got)}")

    empty = gh_stub(tmp, "true")
    got = with_path(empty, lambda: chk.read_labels("tracebloc", "r"))
    record(got == set(), "a repo with NO labels is a successful read, not an error",
           "an empty 200 is an answer; every required label is then missing, which "
           "the ordinary path reports")

    forbidden = gh_stub(tmp, 'echo "HTTP 403: rate limit" >&2', rc=1)
    refuses("a 403/rate-limited read is a refusal, never 'no labels'",
            lambda: with_path(forbidden, lambda: chk.read_labels("tracebloc", "r")),
            "unreadable")


def case_audit_is_fail_closed(tmp: Path) -> None:
    wanted = {"work-type:bug": "x", "priority": "y"}

    def reader(org, repo):
        if repo == "unreadable-repo":
            raise chk.CannotTell(f"{org}/{repo}: label list unreadable (403)")
        if repo == "partial-repo":
            return {"work-type:bug"}
        return {"work-type:bug", "priority", "unrelated"}

    missing, unreadable = chk.audit(
        "tracebloc", ["good-repo", "partial-repo", "unreadable-repo"], wanted, reader)
    record(missing == {"partial-repo": ["priority"]},
           "a repo missing one label is reported with the label named",
           f"missing={missing}")
    record(list(unreadable) == ["unreadable-repo"] and "unreadable-repo" not in missing,
           "an unreadable repo is UNKNOWN, never folded into 'complies' or 'missing'",
           f"unreadable={list(unreadable)} — the distinction backend#1415 was filed "
           "for: a 403 that reads as absence is a finding about the wrong thing")

    m2, u2 = chk.audit("tracebloc", [], wanted, reader)
    record(m2 == {} and u2 == {},
           "an EMPTY repo list produces no findings — which is why main() refuses it",
           "an empty scope passes every assertion; `enrolled_repos` returning "
           "nothing must therefore be a refusal upstream, asserted below")


def case_main_refuses_an_empty_scope(tmp: Path) -> None:
    """The end-to-end refusal, driven through `main()` with a stubbed inventory.

    `audit` on an empty list is silently clean (asserted above), so the only thing
    standing between that and a vacuous green is `main()`'s refusal. Assert it
    through the real entry point rather than trusting the reading.
    """
    proc = run_main(tmp, "empty-inv.yml", f"""\
        schema_version: 1
        org: tracebloc
        reusables:
          - {chk.REUSABLE}
        repos:
          lonely:
            callers:
              other.yml: required
    """)
    ok = proc.returncode == 2 and "ZERO repos declare the caller" in proc.stdout
    record(ok, "main() refuses an empty fleet instead of passing vacuously",
           f"exit={proc.returncode}; stdout mentions the refusal="
           f"{'ZERO repos declare the caller' in proc.stdout}")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for case in (
            case_constant_is_real,
            case_enrolment,
            case_caller_derivation,
            case_stale_idiom,
            case_template_derivation,
            case_live_domain_matches_the_written_rule,
            case_read_labels,
            case_audit_is_fail_closed,
            case_main_refuses_an_empty_scope,
        ):
            case(tmp)

    bad = [n for ok, n, _ in RESULTS if not ok]
    print(f"\ntriage-labels-selftest: {len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    if bad:
        print("FAILED: " + "; ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
