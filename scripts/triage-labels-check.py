#!/usr/bin/env python3
"""Every triage label the org's automation fires on must EXIST in every enrolled repo.

WHY THIS EXISTS (tracebloc/backend#2598)
----------------------------------------
`customer-priority-bump.yml` carries two label rules: `from:customer` -> add
`priority`, and `work-type:bug` -> move the card from `Backlog` to `Ready`. Both
key on a label that must be ADDED to the issue before the workflow can see it,
and the org issue templates are what add it.

**GitHub silently DROPS a template label the target repo does not have.** No
error, no annotation, no run. So a repo can carry the caller, pass
`caller-drift.py` as fully conformant, and be wired in exactly the way that does
nothing. Measured 2026-08-27, before this check existed:

    design-system-v2   0 of 7 triage labels
    release-train      0 of 7
    rfcs               0 of 7
    e2e-test-agent     2 of 7   <-- invisible to the ticket that filed this

`e2e-test-agent` is the reason this check is not scoped to `work-type:*`.
backend#2598 was derived over the `work-type:*` prefix and reported three
repos; that repo has `work-type:bug` and `priority` and NONE of the other five,
so a `work-type:*`-only sweep called it covered while four of the labels the
templates apply were being dropped on the floor. Deriving the domain from a
PREFIX rather than from the producers is the vocabulary gap CLAUDE.md rule 6
describes -- committed, in this instance, by the ticket asking for the check.

DERIVED, NEVER RESTATED (CLAUDE.md rule 1)
------------------------------------------
There is no hand-written label list in this file. Two producers declare the
vocabulary and both are parsed:

  CALLER family    `.github/workflows/customer-priority-bump.yml` --
                   every `workflow_call` input whose name ends in `-label`
                   contributes its `default`, and every `--add-label X` the
                   workflow's `run:` blocks execute contributes `X`.
  TEMPLATE family  `.github/ISSUE_TEMPLATE/*.yml` -- every entry of each
                   template's top-level `labels:`.

Rename an input, add a template, change a default: the domain moves with it.
The repo list is `repo-inventory.yml`, filtered to repos that DECLARE the
caller -- the same authority `caller-drift.py` uses, and the same one that would
have made backend#2598's first draft name three repos instead of two.

FAIL CLOSED (CLAUDE.md rule 3)
------------------------------
"Cannot tell" is a finding, never a pass:

  * an unreadable/unparseable inventory, workflow or template directory  -> exit 2
  * ZERO enrolled repos, ZERO caller labels or ZERO template labels      -> exit 2
    (this file's premise is that all three exist; finding none means the
    matcher broke, not that the fleet got clean)
  * a repo whose label list cannot be read                               -> exit 2
    A 403, a rate limit or a 404 is NOT "the label is absent" and is NOT
    "the repo complies". It is UNKNOWN, and unknown fails.
  * the `--add-label` idiom appearing in the workflow while the matcher
    finds nothing                                                        -> exit 2
    (a stale idiom would report a clean sweep of a subset -- the shape
    kanban-columns-check.py's `cross_check` exists for)

A repo that answers 200 with an EMPTY label list is a successful read, not an
unreadable one -- and every required label is then missing, so it is a finding
by the ordinary path. That distinction is the whole reason `read_labels`
separates "no labels" from "no answer".

WHAT THIS DOES NOT CHECK, stated so the green is not read for more than it is:
colour and description. They agree across the fleet today (measured: 16 of 17
identical on every label, the outlier being `e2e-test-agent`'s ad-hoc
`work-type:bug`, since aligned), but a wrong colour drops no label and blocks no
automation. Existence is the property that decides whether the rule can fire.

Exit codes: 0 every enrolled repo has every derived label; 2 anything else.
There is deliberately no exit 1: a missing label and an unreadable repo are the
same verdict here -- neither is evidence that the rule can fire.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - guarded by `make guard-pyyaml`
    sys.stderr.write("::error::PyYAML is required: python3 -m pip install pyyaml\n")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "repo-inventory.yml"
WORKFLOWS = ROOT / ".github" / "workflows"
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"

# The one name this file holds, and the selftest asserts it against BOTH the
# directory and the inventory's `reusables:` list -- so a rename cannot leave the
# check silently pointed at a file the fleet does not call. Everything else is
# read out of this workflow rather than written down beside it.
REUSABLE = "customer-priority-bump.yml"

# `gh issue edit ... --add-label priority`. Matched with the flag, so the label
# the workflow WRITES is derived from the write itself — and matched over
# `run_scripts()`, the parsed `run:` bodies with comment lines stripped, never
# over the file. `$`-containing values are skipped: an interpolated label cannot
# be known here, and guessing one would put a fabricated name under the
# assertion.
ADD_LABEL = re.compile(r"--add-label[= ]+([A-Za-z0-9:_.\-]+)")

# The suffix that marks a `workflow_call` input as naming a label. Deriving over
# the suffix rather than over two input names means a THIRD label rule added to
# the reusable is covered the day it lands.
LABEL_INPUT_SUFFIX = "-label"


class CannotTell(Exception):
    """A read that produced no answer. Never folded into 'complies'."""


def load_inventory(path: "Path | None" = None) -> dict:
    path = path or INVENTORY
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CannotTell(f"{path.name} could not be read or parsed: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        raise CannotTell(f"{path.name} has no `repos:` mapping — refusing to audit "
                         "a fleet this script could not enumerate")
    return data


def enrolled_repos(inv: dict, reusable: str = REUSABLE) -> "list[str]":
    """Repos that DECLARE the caller, from the inventory. Empty is a refusal.

    A repo whose entry marks the caller `exempt`/`divergent` (a mapping rather
    than the string `required`) is NOT enrolled: it is not expected to run the
    workflow, so the labels it fires on are not its contract. Those are reported
    separately by `not_enrolled` so an exemption cannot quietly shrink the audit
    without anyone seeing which repo left.
    """
    out = []
    for name, spec in (inv.get("repos") or {}).items():
        callers = (spec or {}).get("callers") or {}
        if callers.get(reusable) == "required":
            out.append(name)
    return sorted(out)


def not_enrolled(inv: dict, reusable: str = REUSABLE) -> "list[str]":
    """Declared repos that are NOT enrolled, with why. Reported, never silent."""
    out = []
    for name, spec in sorted((inv.get("repos") or {}).items()):
        callers = (spec or {}).get("callers") or {}
        if reusable not in callers:
            out.append(f"{name}: no `{reusable}` entry at all")
        elif callers[reusable] != "required":
            state = callers[reusable]
            if isinstance(state, dict):
                why = next(iter(state.values()), "")
                state = f"{next(iter(state), '?')}: {str(why)[:110]}"
            out.append(f"{name}: {state}")
    return out


def _parse(path: "Path") -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise CannotTell(f"{path.name} could not be read or parsed: {exc}") from exc


def label_inputs(doc: dict, name: str = REUSABLE) -> "dict[str, dict]":
    """The `workflow_call` inputs that NAME a label, from the parsed document.

    Parsed, never grepped, and this is the correction Bugbot made on .github#364
    rather than a stylistic preference. The first version of `stale_idiom` asked
    whether the string `-label` appeared anywhere in the file — which the very
    COMMENTS this change added satisfy. So the backstop would have stayed quiet
    after the inputs were renamed away, the domain would have silently lost
    `from:customer` (no template applies it), and the check would have gone green
    while the `bump` rule was dead. A guard a comment can satisfy is inert
    verification, and kanban-columns-check.py's `names_in` carries the same
    lesson from e2e#176.
    """
    on = (doc or {}).get("on", (doc or {}).get(True)) or {}
    # `on: push` parses to a STRING and `on: [push, pull_request]` to a list.
    # Neither declares a `workflow_call` input, so neither has label inputs —
    # which `stale_idiom` then reports as a finding rather than crashing on.
    if not isinstance(on, dict):
        return {}
    call = on.get("workflow_call") or {}
    inputs = (call.get("inputs") if isinstance(call, dict) else None) or {}
    if not isinstance(inputs, dict):
        raise CannotTell(f"{name}: `workflow_call.inputs` is not a mapping")
    return {k: (v or {}) for k, v in sorted(inputs.items())
            if k.endswith(LABEL_INPUT_SUFFIX)}


def run_scripts(doc: dict) -> str:
    """Every `run:` body in the workflow, with shell comment lines stripped.

    The `--add-label` derivation reads THIS rather than the file, for the same
    reason `label_inputs` parses: a `--add-label` mentioned in a YAML comment (or
    in a `#` line inside a run block) is not a call the workflow makes, and a
    matcher a comment can feed reports a domain the workflow does not have.
    """
    bodies: "list[str]" = []
    for job in ((doc or {}).get("jobs") or {}).values():
        for step in ((job or {}).get("steps") or []):
            body = (step or {}).get("run")
            if isinstance(body, str):
                bodies.extend(ln for ln in body.splitlines()
                              if not ln.strip().startswith("#"))
    return "\n".join(bodies)


def caller_labels(path: "Path | None" = None) -> "dict[str, str]":
    """The labels the reusable fires on or writes, read from the reusable.

    Returns {label: how it was derived}. Two idioms, both parsed out of the
    workflow: the `*-label` input defaults it KEYS on, and the `--add-label`
    values its `run:` blocks WRITE. A label rule that used neither would be
    invisible here, which is why `stale_idiom` below asserts both idioms actually
    matched — structurally, not by looking for a string in the file.
    """
    path = path or (WORKFLOWS / REUSABLE)
    doc = _parse(path)
    inputs = label_inputs(doc, path.name)

    found: "dict[str, str]" = {}
    for name, spec in inputs.items():
        default = (spec or {}).get("default")
        if not isinstance(default, str) or not default.strip():
            # An input that names a label with no default cannot be derived from
            # here — the caller supplies it. Refusing is the honest answer: a
            # silent skip would shrink the domain to whatever happened to have a
            # default, which is rule 6's vocabulary gap.
            raise CannotTell(f"{path.name}: input `{name}` names a label but has no "
                             "string default, so the label it fires on cannot be "
                             "derived here")
        found[default.strip()] = f"input `{name}` default"

    for label in sorted(set(ADD_LABEL.findall(run_scripts(doc)))):
        if "$" in label:
            continue
        found.setdefault(label, "written by `--add-label`")
    return found


def stale_idiom(path: "Path | None" = None) -> "list[str]":
    """Both matchers must still match, asserted against the PARSED workflow.

    kanban-columns-check.py learned this the expensive way: its writer list was
    wrong on day one and the check stayed green, because "found nothing" and
    "there is nothing" are indistinguishable to a regex.

    ASSERTED STRUCTURALLY, NOT TEXTUALLY (Bugbot, .github#364). Both questions
    are answered from `label_inputs()` and `run_scripts()` — the same functions
    the derivation itself uses — so no comment anywhere in the file can satisfy
    either. That matters most for the `*-label` half: the caller family loses
    `from:customer` when those inputs go, and NO template applies it, so a quiet
    backstop there is a green run over a dead `bump` rule.
    """
    path = path or (WORKFLOWS / REUSABLE)
    try:
        doc = _parse(path)
    except CannotTell as exc:
        return [f"{path.name} is unreadable: {exc}"]
    out = []
    try:
        inputs = label_inputs(doc, path.name)
    except CannotTell as exc:
        return [f"{path.name} is unreadable: {exc}"]
    if not inputs:
        out.append(f"{path.name} declares no `*{LABEL_INPUT_SUFFIX}` "
                   "`workflow_call` input any more — either the label rules moved, "
                   "or the derivation is stale. The caller family cannot lose those "
                   "inputs quietly: `from:customer` comes from nowhere else")
    scripts = run_scripts(doc)
    if "--add-label" in scripts and not ADD_LABEL.findall(scripts):
        out.append(f"{path.name} still runs `--add-label` but ADD_LABEL matched "
                   "nothing — the written label is no longer under this assertion")
    return out


def template_labels(where: "Path | None" = None) -> "dict[str, str]":
    """Every label the org issue templates apply, read from the templates.

    These are the labels GitHub DROPS when the target repo lacks them, so they
    are the direct cause of the defect this file exists for — not a stricter
    extra. `config.yml` carries no `labels:` and contributes nothing.
    """
    where = where or TEMPLATES
    if not where.is_dir():
        raise CannotTell(f"{where} is not a directory — refusing to report that "
                         "template labels exist when the templates could not be read")
    found: "dict[str, str]" = {}
    for path in sorted(where.glob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise CannotTell(f"{path.name} could not be read or parsed: {exc}") from exc
        for label in (doc or {}).get("labels") or []:
            if isinstance(label, str) and label.strip():
                found.setdefault(label.strip(), f"applied by `{path.name}`")
    return found


def required_labels(
    workflow: "Path | None" = None, templates: "Path | None" = None
) -> "dict[str, str]":
    """The whole derived domain. Empty in either family is a refusal."""
    caller = caller_labels(workflow)
    if not caller:
        raise CannotTell("derived ZERO labels from the reusable — this script's "
                         "premise is that it fires on some, so finding none means "
                         "the derivation broke, not that there are none")
    tmpl = template_labels(templates)
    if not tmpl:
        raise CannotTell("derived ZERO labels from the issue templates — same "
                         "reasoning: a matcher that finds nothing is the finding")
    merged = dict(tmpl)
    for label, why in caller.items():
        if label in merged:
            merged[label] = f"{merged[label]}; {why}"
        else:
            merged[label] = why
    return merged


def read_labels(org: str, repo: str) -> "set[str]":
    """The repo's label names. Raises CannotTell rather than returning empty.

    An EMPTY set from a successful call is a legitimate answer (the repo has no
    labels) and is returned as such — every required label is then missing, which
    the ordinary path reports. What must never happen is a failed call arriving
    here as an empty set: that reads as "no labels" and therefore as findings
    about the right repo for the wrong reason, or worse, as compliance once the
    domain is empty too.
    """
    proc = subprocess.run(
        ["gh", "api", f"repos/{org}/{repo}/labels?per_page=100",
         "--paginate", "--jq", ".[].name"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise CannotTell(f"{org}/{repo}: label list unreadable "
                         f"({proc.stderr.strip()[:200] or 'no stderr'})")
    names = {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}
    return names


def audit(org: str, repos: "list[str]", wanted: "dict[str, str]",
          reader=read_labels) -> "tuple[dict[str, list[str]], dict[str, str]]":
    """(missing per repo, unreadable per repo). Extracted so the suite and the
    mutation harness both drive the REAL comparison rather than a copy of it
    (CLAUDE.md rule 9)."""
    missing: "dict[str, list[str]]" = {}
    unreadable: "dict[str, str]" = {}
    for repo in repos:
        try:
            have = reader(org, repo)
        except CannotTell as exc:
            unreadable[repo] = str(exc)
            continue
        gap = sorted(set(wanted) - have)
        if gap:
            missing[repo] = gap
    return missing, unreadable


def main() -> int:
    try:
        inv = load_inventory()
        org = inv.get("org")
        if not isinstance(org, str) or not org.strip():
            raise CannotTell("repo-inventory.yml declares no `org:`")
        if REUSABLE not in (inv.get("reusables") or []):
            raise CannotTell(f"`{REUSABLE}` is not in the inventory's `reusables:` "
                             "list, so this check is pointed at a workflow the fleet "
                             "is not declared to call — fix the constant or the "
                             "inventory before believing either")
        repos = enrolled_repos(inv)
        if not repos:
            raise CannotTell("ZERO repos declare the caller — an empty scope passes "
                             "every assertion, so this is a failed enumeration")
        wanted = required_labels()
    except CannotTell as exc:
        print(f"ERROR: {exc}")
        print("\nRefusing to report label conformance from a read this script did "
              "not understand. Nothing here is known to comply.")
        return 2

    stale = stale_idiom()
    if stale:
        print("ERROR: a derivation matcher has gone stale, so this check would "
              "report a clean sweep of a subset:")
        for row in stale:
            print(f"  - {row}")
        return 2

    print(f"Derived {len(wanted)} triage label(s) that must exist in each of the "
          f"{len(repos)} enrolled repo(s):")
    for label, why in sorted(wanted.items()):
        print(f"  {label:24} <- {why}")

    skipped = not_enrolled(inv)
    if skipped:
        print("\nDeclared but NOT enrolled (not expected to run the caller, so the "
              "labels are not their contract):")
        for row in skipped:
            print(f"  - {row}")

    missing, unreadable = audit(org, repos, wanted)

    print("")
    for repo in repos:
        if repo in unreadable:
            mark, detail = "??", "UNREADABLE"
        elif repo in missing:
            mark, detail = "x ", "missing " + ", ".join(missing[repo])
        else:
            mark, detail = "ok", f"all {len(wanted)} present"
        print(f"  {mark} {repo:24} {detail}")

    if unreadable:
        print("\nERROR: these repos' label lists could not be read. A repo that "
              "cannot be read is NOT known to comply — 'cannot tell' is a finding:")
        for repo, why in sorted(unreadable.items()):
            print(f"  - {why}")

    if missing:
        print("\nERROR: these enrolled repos are missing a triage label the org's "
              "automation fires on:")
        for repo, gap in sorted(missing.items()):
            print(f"  - {repo}: {', '.join(gap)}")
        print("\nGitHub silently DROPS a template label the target repo lacks, so "
              "the issue is filed unlabelled, the caller never fires, and the card "
              "sits in `Backlog`. Create the label (`gh label create`) with the "
              "same name, colour and description the rest of the fleet uses — do "
              "not rewrite the template or the workflow to route around it.")

    if missing or unreadable:
        return 2

    print(f"\nAll {len(wanted)} derived label(s) exist in all {len(repos)} enrolled "
          "repo(s): every label rule can fire everywhere it is declared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
