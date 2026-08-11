#!/usr/bin/env python3
"""Compare every active tracebloc repo against repo-inventory.yml.

tracebloc/backend#1415. Nothing in the org detected a missing caller before this:
merge-settings-drift.yml reads three booleans, and kanban-reconcile.yml:436 probes
a single filename to decide board scope. Eight repos drifted unnoticed and
e2e-test-agent#1 closed without routing because no closure caller exists there.

DESIGN RULES, all of them learned from bugs in this repo:

1.  NEVER REPORT ALL-CLEAR FROM A FAILED READ. Every read either produces a value
    or produces an UNREADABLE record, and one unreadable record fails the run.
    There is no code path where an exception, a 403, a rate limit, an unparseable
    file or a truncated API response is folded into "no caller found". That
    conflation is kanban-reconcile.yml:436 - a 403 there is indistinguishable
    from "repo not tracked", so the repo silently leaves scope and the sweep
    reports success having swept nothing.

2.  MATCH ON `uses:` CONTENT, NEVER ON FILENAME. Two filename conventions are
    mixed within nearly every repo and kanban-closure-router's callers match
    neither. Each workflow is parsed as YAML and its resolved `uses:` values are
    read, so a commented-out example line (code-quality.yml:60) is not a caller
    and a local copy that merely shares a name is not either.

3.  DEVELOP-FIRST. Twelve of twenty repos default to main/master while work lands
    on develop. Enumerating on the default branch under-reports anything in
    flight; that error produced three wrong counts in this epic.

4.  A MISSING INVENTORY KEY IS A FAILURE, NOT A DEFAULT. Schema validation runs
    before any network call, and every repo must carry an entry for every
    reusable and every copy. An exemption without a written reason is rejected.

ONE KNOWN AWKWARDNESS, documented rather than papered over: the inventory is read
from the checkout, but every repo's state - including tracebloc/.github's own - is
read from its audit branch over the API. So a PR that adds a caller to .github AND
flips that entry to `required` in the same commit fails, because the caller is not
on develop yet. That is the fail-closed direction, and reading .github's workflows
from the checkout instead would break the develop-first policy on a run triggered
from main. Land the caller first, flip the entry in a follow-up.

Exit codes, all of which the calling workflow treats as failure except 0:
  0  every repo read, inventory matches reality
  1  drift: reality diverges from the inventory
  2  could not evaluate: bad inventory, failed enumeration, or an unreadable repo
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - the workflow installs it explicitly
    sys.stderr.write(
        "::error::PyYAML is not importable. The guard parses workflows as YAML "
        "rather than grepping them; without it there is no degraded mode that is "
        "still trustworthy, so this is a hard failure.\n"
    )
    raise SystemExit(2)

TOP_LEVEL_KEYS = {
    "schema_version", "org", "pinned_ref", "audit_branch", "source_repo",
    "reusables", "copies", "shared_reasons", "repos", "protection_policy",
    "ruleset_policy",
}
REQUIRED_TOP_LEVEL = TOP_LEVEL_KEYS - {"shared_reasons"}
REPO_KEYS = {
    "visibility", "release_train", "callers", "copies", "protection", "rulesets",
}

# ---------------------------------------------------------------------- rulesets
#
# GitHub's SECOND protection system, and until backend#1681 nothing in the org
# audited it. `read_protection()` reads rulesets, but only to satisfy the seven
# classic-expressible properties -- so it can tell that a branch is protected,
# never that a ruleset EXISTS, what it permits, or who may bypass it.
#
# Two things rest entirely on that unaudited layer:
#   * `allowed_merge_methods: ["merge"]` on every promotion branch. This is not
#     expressible in classic protection at all, and it is what stops a promotion
#     PR being squash-merged -- which would collapse the merge-commit ancestry
#     the release train's squash guard reads.
#   * the `v*` tag trust root on the repos that publish from a tag.
#
# Measured 2026-08-11, both were drifting unnoticed: start-training carried NO
# ruleset at all (its classic protection was fully compliant, so the guard was
# structurally blind to it), and `release-python` -- four engineers -- held
# `always` tag bypass on backend, data-ingestors and tracebloc-py-package, two
# of which publish on the tag. Neither is detectable by any existing check.
#
# MATCH ON TARGET + RULES, NEVER ON NAME. `client`'s tag ruleset is called
# "R8 trust root - protect v* release tags" while its five peers use
# "Protect v* release tags (supply-chain trust root)". A name-keyed check
# silently reports client as missing its trust root.
RULESET_KINDS = ("promotion_merge_commit_only", "tag_trust_root")
SUPPORTED_SCHEMA = 2
SUPPORTED_AUDIT_BRANCH = "develop-first"

# ------------------------------------------------------------------- protection
#
# The three branch ROLES every repo is measured against. `prod` resolves per repo
# to `main` or `master` from the BRANCH LIST -- never by probing
# `branches/main` then `branches/master`, because GitHub follows rename
# redirects: `GET branches/master` on a repo renamed master -> main returns 200
# for a branch that does not exist. Only the list tells the truth.
PROTECTION_ROLES = ("develop", "staging", "prod")

# What a policy may assert. `null` means "not asserted" -- used for
# enforce_admins on develop/staging, which backend#1276 deliberately leaves open
# as the cheap escape hatch. Not asserting is different from asserting false:
# a repo that hardens develop must not be reported as drift.
POLICY_KEYS = {
    "classic_protection",              # bool: the classic protection object exists
    "min_reviews",                     # int: effective approving reviews >= this
    "enforce_admins",                  # bool | null
    "block_force_pushes",              # bool
    "block_deletions",                 # bool
    "require_conversation_resolution",  # bool
    "strict",                          # bool | null: required_status_checks.strict
    # list[str]: contexts that must be REQUIRED on this role, merged across both
    # protection systems. A caller being present only means the check RUNS; this
    # is what makes it BLOCK. Until backend#1681 the whole
    # required_status_checks object was opened and everything but `strict` thrown
    # away, so `.github/staging` required exactly one context while the inventory
    # reported it conformant -- and two promotion PRs merged with the contract
    # audit red (.github#203, #205).
    "required_checks",
}
# Which policy keys a per-repo `divergent` entry may override. Deliberately NOT
# every key: a repo may document a weaker review count or admin posture, but it
# may not opt out of `classic_protection` -- that is `exempt`, which is a louder
# word and shows up differently in the report.
OVERRIDABLE = {
    "min_reviews", "enforce_admins", "require_conversation_resolution", "strict",
    # A repo may require FEWER contexts than the fleet baseline, but only with a
    # written reason naming the narrower set -- so the gap is a sentence in this
    # file rather than something a reader has to diff two API calls to discover.
    "required_checks",
}

WORKFLOW_PATH = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
ORG_USES = re.compile(
    r"^tracebloc/\.github/\.github/workflows/(?P<name>[^/@]+\.ya?ml)@(?P<ref>.+)$"
)
HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")

# The repo that holds the authoritative release-train membership list.
TRAIN_REPO = "release-train"
TRAIN_FILE = "repos.yml"


def die(message: str) -> "None":
    """Abort with exit code 2. Used only where a value could not be established."""
    sys.stderr.write(f"::error::{message}\n")
    sys.stderr.write(
        "::error::Refusing to report on caller drift from an incomplete read.\n"
    )
    raise SystemExit(2)


class GhError(Exception):
    def __init__(self, status: "int | None", detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def gh(args: "list[str]") -> str:
    """Run `gh` and return stdout. Raises GhError with the HTTP status if known.

    stdin is /dev/null: `gh` inherits stdin, and a caller iterating a list on
    stdin would otherwise have its list swallowed (the bug merge-settings-drift.yml
    guards against with `< /dev/null`).
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise GhError(None, f"could not execute gh: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().replace("\n", " ")
        match = HTTP_STATUS.search(err)
        status = int(match.group(1)) if match else None
        raise GhError(status, err or f"gh exited {proc.returncode}")
    return proc.stdout


def gh_json(args: "list[str]"):
    raw = gh(args)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GhError(None, f"gh returned unparseable JSON: {exc}") from exc


def gh_json_array(path: str) -> "list":
    """Read a paginated ARRAY endpoint completely.

    `gh api --paginate` on an array endpoint concatenates one JSON array per
    page, which `json.loads` cannot parse -- so this streams elements with
    `--jq '.[]'` (element-wise, NOT an aggregating filter, which --paginate would
    re-run per page) and reassembles them.

    Pagination is not optional here. `rules/branches/{b}` defaults to 30 items
    per page, and a rule dropped off page 2 is a silently PARTIAL view of a
    branch's protection -- which this guard would then report as a verdict.
    Exactly the failure mode the header of read_protection() describes.
    (Bugbot, .github#196.)
    """
    raw = gh(["api", "--paginate", "--jq", ".[]", path])
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise GhError(None, f"gh returned an unparseable element: {exc}") from exc
    return out


# --------------------------------------------------------------------- schema


def _reason_entry(value, where: str, allowed: "set[str]") -> "tuple[str, str]":
    """Validate a single inventory cell. Returns (state, reason).

    `required` is the only bare scalar accepted. Everything else must be a
    one-key mapping whose value is a non-empty string, so an exemption cannot
    exist without a written reason.
    """
    if value == "required":
        return ("required", "")
    if isinstance(value, str):
        die(
            f"{where}: {value!r} is not a valid state. Use `required`, or "
            f"one of {sorted(allowed)} with a written reason."
        )
    if not isinstance(value, dict) or len(value) != 1:
        die(
            f"{where}: expected `required` or exactly one of {sorted(allowed)} "
            f"mapped to a reason string, got {value!r}."
        )
    state, reason = next(iter(value.items()))
    if state not in allowed:
        die(f"{where}: unknown state {state!r}; expected one of {sorted(allowed)}.")
    if not isinstance(reason, str) or not reason.strip():
        die(
            f"{where}: `{state}` carries no written reason. An exemption without "
            "a reason is indistinguishable from an oversight, which is the whole "
            "failure this inventory exists to prevent."
        )
    return (state, reason.strip())


def _policy_value(key: str, value, where: str, allow_null: bool) -> None:
    """Validate ONE policy value.

    Shared by the fleet policy and by per-repo `divergent` overrides on purpose.
    Validating the override KEY NAMES but not their VALUES let a cell that looked
    like a narrow, documented divergence quietly neutralise the assertion instead
    of restating it -- `min_reviews: -1` can never fail, and `enforce_admins:
    null` stops asserting it at all. (Bugbot, .github#196.)

    `allow_null` is False for overrides: naming a key means stating a DIFFERENT
    value for it, not switching the assertion off. Not asserting something is a
    fleet-policy decision, not a per-repo one.
    """
    if key == "min_reviews":
        # bool is a subclass of int in Python, so `min_reviews: true` would slip
        # through a bare isinstance(value, int) check.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            die(f"{where}.min_reviews: must be a non-negative integer.")
        return
    if key in ("classic_protection", "block_force_pushes", "block_deletions"):
        if not isinstance(value, bool):
            die(f"{where}.{key}: must be true or false (it cannot be un-asserted).")
        return
    if key in ("enforce_admins", "require_conversation_resolution", "strict"):
        if value is None:
            if allow_null:
                return
            die(
                f"{where}.{key}: `null` is not allowed in a `divergent` override. "
                "Naming a key means stating a different value for it; switching "
                "the assertion off is a protection_policy decision."
            )
        if not isinstance(value, bool):
            die(f"{where}.{key}: must be true, false, or null (not asserted).")
        return
    if key == "required_checks":
        # A LIST, not a bool, so it needs its own shape rules. `[]` is legal and
        # deliberately so: `docs` requires no checks on staging/prod by a written
        # 2026-06-04 exemption, and saying that out loud in a `divergent` cell is
        # the point of this file. What is NOT legal is a non-list, a non-string
        # entry, or a blank/duplicate context -- each of those would silently
        # assert less than it appears to.
        if not isinstance(value, list):
            die(
                f"{where}.required_checks: must be a list of status-check "
                "contexts (use [] to assert none, with a written reason)."
            )
        seen = set()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                die(
                    f"{where}.required_checks: every entry must be a non-empty "
                    f"context string; got {item!r}."
                )
            if item in seen:
                die(f"{where}.required_checks: duplicate context {item!r}.")
            seen.add(item)
        return
    die(f"{where}.{key}: unknown policy key.")


def _policy_block(value, where: str) -> dict:
    """Validate one branch-role policy. Every POLICY_KEY must be stated."""
    if not isinstance(value, dict):
        die(f"{where}: must be a mapping of policy keys.")
    unknown = set(value) - POLICY_KEYS
    if unknown:
        die(f"{where}: unknown policy key(s) {sorted(unknown)}.")
    missing = POLICY_KEYS - set(value)
    if missing:
        die(
            f"{where}: missing policy key(s) {sorted(missing)}. Absence is never "
            "implicit here either - state the value, or `null` to not assert it."
        )
    for key in POLICY_KEYS:
        _policy_value(key, value[key], where, allow_null=True)
    return dict(value)


def _ruleset_policy_block(value, where: str) -> dict:
    """Validate one ruleset-kind policy.

    Every key is stated explicitly, for the same reason the protection policy
    does it: a silently-absent `bypass_actors` would assert nothing while looking
    like an allowlist.
    """
    required = {"target", "require_rule_types", "bypass_actors"}
    optional = {"allowed_merge_methods", "include_refs", "must_cover_roles"}
    if not isinstance(value, dict):
        die(f"{where}: must be a mapping.")
    unknown = set(value) - required - optional
    if unknown:
        die(f"{where}: unknown key(s) {sorted(unknown)}.")
    absent = required - set(value)
    if absent:
        die(
            f"{where}: missing key(s) {sorted(absent)}. Absence is never implicit - "
            "an unstated bypass allowlist would assert nothing."
        )
    if value["target"] not in ("branch", "tag"):
        die(f"{where}.target: must be `branch` or `tag`.")
    for key in ("require_rule_types", "bypass_actors"):
        _str_list(value[key], f"{where}.{key}", allow_empty=(key == "bypass_actors"))
    for key in optional & set(value):
        if key == "must_cover_roles":
            bad = [r for r in value[key] if r not in PROTECTION_ROLES]
            if bad:
                die(f"{where}.must_cover_roles: unknown role(s) {bad}.")
        _str_list(value[key], f"{where}.{key}", allow_empty=False)
    return dict(value)


def _str_list(value, where: str, allow_empty: bool) -> None:
    """A list of non-empty, unique strings -- or a stated-empty allowlist."""
    if not isinstance(value, list):
        die(f"{where}: must be a list of strings.")
    if not value and not allow_empty:
        die(f"{where}: must not be empty.")
    seen = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            die(f"{where}: every entry must be a non-empty string; got {item!r}.")
        if item in seen:
            die(f"{where}: duplicate entry {item!r}.")
        seen.add(item)


def _protection_entry(value, where: str) -> "tuple[str, str, dict]":
    """Validate one repo x branch-role cell. Returns (state, reason, overrides).

    `required`                      the role's branch exists and meets the policy
    `exempt: "<reason>"`            the branch legitimately does not exist / is
                                    not held to the policy at all
    `divergent: {reason:, <key>:}`  the branch exists and is held to the policy
                                    EXCEPT the named keys, each written down

    `divergent` takes a mapping rather than a bare string on purpose. A blanket
    "this one is different" would switch off every assertion at once, which is
    how an exemption written for one reason silently covers a second, unrelated
    regression later. Naming the deviating key keeps every other assertion live.
    """
    if value == "required":
        return ("required", "", {})
    if isinstance(value, str):
        die(
            f"{where}: {value!r} is not a valid state. Use `required`, "
            "`exempt` with a reason, or `divergent` with a reason plus the "
            "specific policy keys that differ."
        )
    if not isinstance(value, dict) or len(value) != 1:
        die(f"{where}: expected exactly one of required/exempt/divergent, got {value!r}.")
    state, payload = next(iter(value.items()))

    if state == "exempt":
        if not isinstance(payload, str) or not payload.strip():
            die(f"{where}: `exempt` carries no written reason.")
        return ("exempt", payload.strip(), {})

    if state != "divergent":
        die(f"{where}: unknown state {state!r}; expected required/exempt/divergent.")
    if not isinstance(payload, dict):
        die(
            f"{where}: `divergent` must be a mapping with a `reason` and the "
            "policy keys that differ."
        )
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        die(f"{where}: `divergent` carries no written reason.")
    overrides = {k: v for k, v in payload.items() if k != "reason"}
    if not overrides:
        die(
            f"{where}: `divergent` names no differing policy key. If nothing "
            "differs, state `required`; if the branch is out of scope entirely, "
            "use `exempt`."
        )
    illegal = set(overrides) - OVERRIDABLE
    if illegal:
        die(
            f"{where}: {sorted(illegal)} cannot be overridden per-repo. "
            f"Overridable keys are {sorted(OVERRIDABLE)}; dropping "
            "`classic_protection` or the force-push/deletion blocks is `exempt`, "
            "not `divergent`."
        )
    # The VALUES matter as much as the key names. Without this the cell could
    # neutralise the assertion it claims merely to adjust (Bugbot, .github#196).
    for key, val in overrides.items():
        _policy_value(key, val, where, allow_null=False)
    return ("divergent", reason.strip(), overrides)


def load_inventory(path: str) -> dict:
    if not os.path.isfile(path):
        die(f"inventory not found at {path}.")
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        die(f"could not read or parse {path}: {exc}")
    if not isinstance(data, dict):
        die(f"{path}: top level must be a mapping.")

    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        die(
            f"{path}: unknown top-level key(s) {sorted(unknown)}. Unknown keys are "
            "rejected rather than ignored: a typo'd key would otherwise silently "
            "disable whatever it was meant to configure."
        )
    missing = REQUIRED_TOP_LEVEL - set(data)
    if missing:
        die(f"{path}: missing required top-level key(s) {sorted(missing)}.")

    if data["schema_version"] != SUPPORTED_SCHEMA:
        die(
            f"{path}: schema_version is {data['schema_version']!r}; this guard only "
            f"understands {SUPPORTED_SCHEMA}. Refusing to interpret a schema it was "
            "not written for."
        )
    for key in ("org", "pinned_ref", "source_repo"):
        if not isinstance(data[key], str) or not data[key].strip():
            die(f"{path}: `{key}` must be a non-empty string.")
    if data["audit_branch"] != SUPPORTED_AUDIT_BRANCH:
        die(
            f"{path}: audit_branch is {data['audit_branch']!r}; only "
            f"{SUPPORTED_AUDIT_BRANCH!r} is implemented. Not defaulting to the "
            "default branch - that is exactly the under-reporting this corrects."
        )

    for key in ("reusables", "copies"):
        value = data[key]
        if not isinstance(value, list):
            die(f"{path}: `{key}` must be a list.")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            die(f"{path}: `{key}` must contain only non-empty strings.")
        if len(set(value)) != len(value):
            die(f"{path}: `{key}` contains duplicates.")
    reusables = list(data["reusables"])
    copies = list(data["copies"])
    if not reusables:
        die(
            f"{path}: `reusables` is empty. An empty inventory would pass "
            "vacuously, which is worse than no guard at all."
        )
    overlap = set(reusables) & set(copies)
    if overlap:
        die(f"{path}: {sorted(overlap)} listed as both a reusable and a copy.")

    policy = data["protection_policy"]
    if not isinstance(policy, dict):
        die(f"{path}: `protection_policy` must be a mapping keyed by branch role.")
    unknown = set(policy) - set(PROTECTION_ROLES)
    if unknown:
        die(f"{path}: protection_policy has unknown role(s) {sorted(unknown)}.")
    missing = set(PROTECTION_ROLES) - set(policy)
    if missing:
        die(f"{path}: protection_policy is missing role(s) {sorted(missing)}.")
    for role in PROTECTION_ROLES:
        policy[role] = _policy_block(policy[role], f"{path}: protection_policy.{role}")

    rpolicy = data["ruleset_policy"]
    if not isinstance(rpolicy, dict):
        die(f"{path}: ruleset_policy must be a mapping of ruleset kinds.")
    unknown = set(rpolicy) - set(RULESET_KINDS)
    if unknown:
        die(f"{path}: ruleset_policy has unknown kind(s) {sorted(unknown)}.")
    missing = set(RULESET_KINDS) - set(rpolicy)
    if missing:
        die(f"{path}: ruleset_policy is missing kind(s) {sorted(missing)}.")
    for kind in RULESET_KINDS:
        rpolicy[kind] = _ruleset_policy_block(rpolicy[kind], f"{path}: ruleset_policy.{kind}")

    repos = data["repos"]
    if not isinstance(repos, dict) or not repos:
        die(f"{path}: `repos` must be a non-empty mapping.")
    if data["source_repo"] not in repos:
        die(f"{path}: source_repo {data['source_repo']!r} has no entry under `repos`.")

    for name, entry in repos.items():
        where = f"{path}: repos.{name}"
        if not isinstance(entry, dict):
            die(f"{where}: must be a mapping.")
        unknown = set(entry) - REPO_KEYS
        if unknown:
            die(f"{where}: unknown key(s) {sorted(unknown)}.")
        missing = REPO_KEYS - set(entry)
        if missing:
            die(f"{where}: missing key(s) {sorted(missing)}.")
        if entry["visibility"] not in ("public", "private"):
            die(f"{where}.visibility: must be `public` or `private`.")
        if not isinstance(entry["release_train"], bool):
            die(f"{where}.release_train: must be true or false.")

        for section, expected, allowed in (
            ("callers", reusables, {"exempt"}),
            ("copies", copies, {"exempt", "divergent"}),
            # Same schema as the caller family, deliberately: `required`, or an
            # exemption carrying a written reason. There is no `divergent` here --
            # a ruleset that exists but permits something else is drift, not a
            # documented variant, because the thing it permits (a squash promotion,
            # an extra bypass actor) is exactly what the property exists to catch.
            ("rulesets", RULESET_KINDS, {"exempt"}),
        ):
            cells = entry[section]
            if not isinstance(cells, dict):
                die(f"{where}.{section}: must be a mapping.")
            absent = set(expected) - set(cells)
            if absent:
                die(
                    f"{where}.{section}: no entry for {sorted(absent)}. A missing "
                    "key is a guard failure, not a default - state `required`, or "
                    "exempt it with a written reason."
                )
            extra = set(cells) - set(expected)
            if extra:
                die(
                    f"{where}.{section}: {sorted(extra)} is not in the top-level "
                    f"`{section}` list."
                )
            for key, value in cells.items():
                cells[key] = _reason_entry(value, f"{where}.{section}.{key}", allowed)

        prot = entry["protection"]
        if not isinstance(prot, dict):
            die(f"{where}.protection: must be a mapping keyed by branch role.")
        absent = set(PROTECTION_ROLES) - set(prot)
        if absent:
            die(
                f"{where}.protection: no entry for {sorted(absent)}. Same rule as "
                "callers - a missing role is a guard failure, not a default."
            )
        extra = set(prot) - set(PROTECTION_ROLES)
        if extra:
            die(f"{where}.protection: unknown role(s) {sorted(extra)}.")
        for role in PROTECTION_ROLES:
            prot[role] = _protection_entry(
                prot[role], f"{where}.protection.{role}"
            )

    return data


# ---------------------------------------------------------------- source copies


def blob_sha(payload: bytes) -> str:
    """git blob object id, so a copy can be compared without fetching content."""
    header = b"blob %d\0" % len(payload)
    return hashlib.sha1(header + payload).hexdigest()  # noqa: S324 - git's own id


def load_source_copies(source_dir: str, copies: "list[str]") -> "dict[str, str]":
    shas = {}
    for name in copies:
        path = os.path.join(source_dir, ".github", "workflows", name)
        try:
            with open(path, "rb") as handle:
                shas[name] = blob_sha(handle.read())
        except OSError as exc:
            die(
                f"canonical copy {path} is not readable ({exc}). Every repo's copy "
                "is compared against it, so without it the copy check would pass "
                "everything."
            )
    return shas


def check_source_reusables(source_dir: str, listed: "list[str]") -> None:
    """Every `workflow_call` workflow in the source repo must be in the inventory.

    THE GUARD ENUMERATED THE INVENTORY, NEVER THE SOURCE. `reusables` is a
    hand-written list and the audit iterates it, so a reusable added to
    tracebloc/.github but not added to that list is compared against no repo and
    reported by nothing -- the one direction of drift this file could not see.

    `version-bump-pr.yml` is how that surfaced (backend#1681): shipped, never
    listed, zero callers org-wide, and requiring a `pr-token` secret no repo
    supplies. It is Layer 2 of backend#1563, meant to stop the version staleness
    that stalled tracebloc-py-package's prod leg -- and nothing said it was never
    wired up.

    Deliberately a die(), not a finding: the inventory is the contract, and a
    contract that does not mention half the artifacts it governs cannot be
    audited against. Adding the row is the fix; `exempt` with a written reason is
    how a parked reusable stays parked (see wip-limit-check).
    """
    workflows = os.path.join(source_dir, ".github", "workflows")
    if not os.path.isdir(workflows):
        die(
            f"source workflow directory {workflows} is missing. Refusing to "
            "report that every reusable is tracked without having looked."
        )
    found = []
    for name in sorted(os.listdir(workflows)):
        if not name.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(workflows, name)
        try:
            with open(path, encoding="utf-8") as handle:
                doc = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as exc:
            die(f"{path} is not readable/parseable ({exc}); cannot tell if it is a reusable.")
        # `on:` parses as the boolean True in YAML 1.1, which is why this reads
        # both keys rather than the obvious one.
        triggers = doc.get("on") if isinstance(doc, dict) else None
        if triggers is None and isinstance(doc, dict):
            triggers = doc.get(True)
        if isinstance(triggers, dict) and "workflow_call" in triggers:
            found.append(name)
    untracked = sorted(set(found) - set(listed))
    if untracked:
        die(
            f"reusable workflow(s) {untracked} exist in {workflows} but are absent "
            "from the inventory's `reusables` list, so they are checked against no "
            "repo and reported by nothing. Add a row for every repo - `exempt` with "
            "a written reason is how a parked reusable stays parked."
        )
    phantom = sorted(set(listed) - set(found))
    if phantom:
        die(
            f"inventory lists reusable(s) {phantom} that are not `workflow_call` "
            f"workflows in {workflows}. A renamed or deleted reusable leaves every "
            "repo's row asserting something that cannot exist."
        )


# ------------------------------------------------------------------- discovery


def list_active_repos(org: str) -> "dict[str, dict]":
    try:
        rows = gh_json([
            "repo", "list", org, "--limit", "200", "--json",
            "name,isArchived,isFork,visibility,defaultBranchRef",
        ])
    except GhError as exc:
        die(f"could not list repos in {org}: {exc.detail}")
    if not isinstance(rows, list):
        die(f"unexpected payload from `gh repo list {org}`.")

    active = {}
    for row in rows:
        if row.get("isArchived") or row.get("isFork"):
            continue
        name = row.get("name")
        if not name:
            die("`gh repo list` returned a repo with no name.")
        branch_ref = row.get("defaultBranchRef") or {}
        active[name] = {
            "visibility": str(row.get("visibility", "")).lower(),
            "default_branch": branch_ref.get("name"),
        }
    if not active:
        die(
            f"enumerated zero active repos in {org}. The token cannot list the org, "
            "and an empty scope would make every check trivially pass."
        )
    return active


def load_release_train(org: str) -> "set[str]":
    """Authoritative train membership: release-train/repos.yml (RFC-BACKEND-0008 D14)."""
    for ref in ("develop", None):
        path = f"repos/{org}/{TRAIN_REPO}/contents/{TRAIN_FILE}"
        if ref:
            path += f"?ref={ref}"
        try:
            raw = gh(["api", path, "-H", "Accept: application/vnd.github.raw"])
        except GhError as exc:
            if ref and exc.status == 404:
                continue  # no develop branch, or the file only exists on default
            die(
                f"could not read {org}/{TRAIN_REPO}/{TRAIN_FILE}: {exc.detail}. "
                "release_train in the inventory is verified against it, so an "
                "unreadable list must not be treated as 'nobody is on the train'."
            )
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            die(f"{TRAIN_REPO}/{TRAIN_FILE} is not parseable YAML: {exc}")
        entries = (parsed or {}).get("repos")
        if not isinstance(entries, list) or not entries:
            die(
                f"{TRAIN_REPO}/{TRAIN_FILE} has no non-empty `repos:` list. An empty "
                "train list would silently clear release_train for every repo."
            )
        names = set()
        for item in entries:
            if not isinstance(item, dict) or not item.get("name"):
                die(f"{TRAIN_REPO}/{TRAIN_FILE}: entry {item!r} has no `name`.")
            names.add(item["name"])
        return names
    die(f"could not locate {TRAIN_FILE} in {org}/{TRAIN_REPO} on any branch.")
    return set()  # unreachable; keeps the return type honest


def collect_uses(node, found: "list[str]") -> None:
    """Walk a parsed workflow and collect every `uses:` string value."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                found.append(value.strip())
            else:
                collect_uses(value, found)
    elif isinstance(node, list):
        for item in node:
            collect_uses(item, found)


class RepoRead:
    """What the guard managed to learn about one repo, or why it could not."""

    def __init__(self, name: str):
        self.name = name
        self.branch: "str | None" = None
        self.branches: "set[str]" = set()
        self.callers: "dict[str, list[tuple[str, str]]]" = {}
        self.copies: "dict[str, str]" = {}
        self.has_workflow_dir = False
        self.errors: "list[str]" = []

    @property
    def ok(self) -> bool:
        return not self.errors


def read_repo(org: str, name: str, meta: dict, copies: "list[str]") -> RepoRead:
    out = RepoRead(name)

    try:
        branches = {
            line.strip()
            for line in gh([
                "api", f"repos/{org}/{name}/branches",
                "--paginate", "--jq", ".[].name",
            ]).splitlines()
            if line.strip()
        }
    except GhError as exc:
        out.errors.append(f"branch list unreadable ({exc.detail})")
        return out
    if not branches:
        # An empty repo is a legitimate state, but it is not one this guard can
        # audit, and it must not read as "no callers, all exemptions hold".
        out.errors.append("repo has no branches")
        return out
    out.branches = branches

    # develop-first, per the inventory's audit_branch policy.
    if "develop" in branches:
        out.branch = "develop"
    elif meta.get("default_branch") in branches:
        out.branch = meta["default_branch"]
    else:
        out.errors.append(
            f"default branch {meta.get('default_branch')!r} is not in the branch "
            "list; cannot choose an audit branch"
        )
        return out

    try:
        tree = gh_json([
            "api", f"repos/{org}/{name}/git/trees/{out.branch}?recursive=1",
        ])
    except GhError as exc:
        out.errors.append(f"tree of {out.branch} unreadable ({exc.detail})")
        return out
    if tree.get("truncated"):
        # A truncated tree means "some paths are missing from this response".
        # Concluding "no workflows" from it is precisely the fail-open this guard
        # exists to eliminate.
        out.errors.append(f"git tree of {out.branch} was truncated by the API")
        return out
    entries = tree.get("tree")
    if not isinstance(entries, list):
        out.errors.append(f"git tree of {out.branch} had no `tree` array")
        return out

    workflows = [
        item for item in entries
        if item.get("type") == "blob" and WORKFLOW_PATH.match(item.get("path") or "")
    ]
    out.has_workflow_dir = bool(workflows)

    for item in workflows:
        path = item["path"]
        filename = path.rsplit("/", 1)[-1]
        sha = item.get("sha")
        if not sha:
            out.errors.append(f"{path}: tree entry has no sha")
            continue
        if filename in copies:
            out.copies[filename] = sha
        try:
            payload = gh_json(["api", f"repos/{org}/{name}/git/blobs/{sha}"])
            encoding = payload.get("encoding")
            content = payload.get("content")
            if encoding != "base64" or not isinstance(content, str):
                raise GhError(None, f"unexpected blob encoding {encoding!r}")
            text = base64.b64decode(content).decode("utf-8", errors="replace")
        except (GhError, ValueError) as exc:
            out.errors.append(f"{path}: content unreadable ({exc})")
            continue
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            # An unparseable workflow could contain any caller at all. Recorded as
            # unreadable, never as "contains no caller".
            out.errors.append(f"{path}: unparseable YAML ({exc.__class__.__name__})")
            continue
        refs: "list[str]" = []
        collect_uses(parsed, refs)
        for ref in refs:
            match = ORG_USES.match(ref)
            if match:
                out.callers.setdefault(match.group("name"), []).append(
                    (filename, match.group("ref"))
                )
    return out


# ------------------------------------------------------------------ protection
#
# GITHUB RUNS TWO INDEPENDENT BRANCH-PROTECTION SYSTEMS AND THE CLASSIC API ONLY
# SEES ONE OF THEM. This is not a footnote; it is the reason this family exists
# in the shape it does.
#
# A branch protected solely by a RULESET returns
#   404 {"message": "Branch not protected"}
# from `branches/{b}/protection`, while `GET /branches` reports
# `"protected": true` for that same branch. Reading only the classic endpoint
# therefore reports a protected branch as unprotected. Measured 2026-08-10 on
# docs/staging, where it produced a wrong finding on backend#1276 before being
# caught: the fleet-wide `promotion-branches-merge-commit-only` ruleset was
# covering it the whole time.
#
# Rules also do NOT compose the way classic settings do. A `pull_request` rule
# requires changes to arrive via a PR -- which blocks direct and force pushes --
# but it does NOT block deletion; `deletion` and `non_fast_forward` are separate
# rule types. So "has a ruleset" never means "equivalently protected", and the
# merge below is per-property rather than per-system.


class BranchProtection:
    """Effective protection on one branch, merged across BOTH systems."""

    def __init__(self, branch: str):
        self.branch = branch
        self.classic_present = False
        self.min_reviews = 0
        self.enforce_admins = False
        self.block_force_pushes = False
        self.block_deletions = False
        self.conversation_resolution = False
        self.strict: "bool | None" = None
        # Contexts that BLOCK a merge on this branch, unioned across both
        # systems. A set, because the same context can be required by classic
        # protection and by a ruleset and it blocks exactly once either way.
        self.required_checks: "set[str]" = set()
        self.rulesets: "list[str]" = []
        self.error: "str | None" = None


def read_protection(org: str, name: str, branch: str) -> BranchProtection:
    out = BranchProtection(branch)

    # --- classic ------------------------------------------------------------
    # A 404 here is a FACT ("no classic protection"), not a read failure. Any
    # other error is a read failure and must not be reported as "unprotected" --
    # that is the fail-open this whole guard exists to eliminate.
    try:
        classic = gh_json(["api", f"repos/{org}/{name}/branches/{branch}/protection"])
    except GhError as exc:
        if exc.status == 404:
            classic = None
        else:
            out.error = f"classic protection unreadable ({exc.detail})"
            return out
    if classic is not None:
        out.classic_present = True
        reviews = classic.get("required_pull_request_reviews") or {}
        out.min_reviews = reviews.get("required_approving_review_count") or 0
        out.enforce_admins = bool((classic.get("enforce_admins") or {}).get("enabled"))
        # The classic API states these as ALLOW flags; the policy states them as
        # BLOCK flags. Invert here so the comparison downstream reads plainly.
        out.block_force_pushes = not (
            classic.get("allow_force_pushes") or {}
        ).get("enabled", False)
        out.block_deletions = not (
            classic.get("allow_deletions") or {}
        ).get("enabled", False)
        out.conversation_resolution = bool(
            (classic.get("required_conversation_resolution") or {}).get("enabled")
        )
        checks = classic.get("required_status_checks")
        # `strict` is only meaningful when a required-status-checks object exists.
        # Absent object -> None ("nothing to assert"), NOT False.
        out.strict = checks.get("strict") if isinstance(checks, dict) else None
        if isinstance(checks, dict):
            # Two spellings of the same list. `checks` is the current shape
            # ({context, app_id}); `contexts` is the legacy flat list still
            # returned for older configurations. Read both -- taking only the
            # modern one would report a branch's required set as EMPTY, which is
            # the fail-open shape this guard exists to prevent.
            for item in checks.get("checks") or []:
                ctx = item.get("context") if isinstance(item, dict) else None
                if isinstance(ctx, str) and ctx:
                    out.required_checks.add(ctx)
            for ctx in checks.get("contexts") or []:
                if isinstance(ctx, str) and ctx:
                    out.required_checks.add(ctx)

    # --- rulesets -----------------------------------------------------------
    # `rules/branches/{b}` resolves every ruleset that targets this branch,
    # including org-level ones, so it does not need the repo ruleset list too.
    try:
        rules = gh_json_array(f"repos/{org}/{name}/rules/branches/{branch}")
    except GhError as exc:
        out.error = f"ruleset rules unreadable ({exc.detail})"
        return out

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rtype = rule.get("type")
        params = rule.get("parameters") or {}
        src = rule.get("ruleset_source") or str(rule.get("ruleset_id") or "?")
        if src not in out.rulesets:
            out.rulesets.append(src)
        if rtype == "pull_request":
            # Changes must arrive via a PR, so direct AND force pushes are
            # blocked. Deletion is NOT covered -- that is the `deletion` rule.
            out.block_force_pushes = True
            out.min_reviews = max(
                out.min_reviews,
                params.get("required_approving_review_count") or 0,
            )
            if params.get("required_review_thread_resolution"):
                out.conversation_resolution = True
        elif rtype == "non_fast_forward":
            out.block_force_pushes = True
        elif rtype == "deletion":
            out.block_deletions = True
        elif rtype == "required_status_checks":
            if params.get("strict_required_status_checks_policy"):
                out.strict = True
            # A ruleset can require checks too, and those block just as hard as
            # the classic ones. No repo uses this today (measured 2026-08-11:
            # all 28 active rules are `pull_request`), but reading only classic
            # would silently under-report the moment one does.
            for item in params.get("required_status_checks") or []:
                ctx = item.get("context") if isinstance(item, dict) else None
                if isinstance(ctx, str) and ctx:
                    out.required_checks.add(ctx)

    return out


def resolve_role_branch(role: str, branches: "set[str]") -> "str | None":
    """Map a policy role onto a real branch name, from the BRANCH LIST only.

    `prod` is whichever of main/master exists. Never probe `branches/master`
    to decide: GitHub follows rename redirects, so that call returns 200 on a
    repo renamed master -> main and every repo looks like it has a `master`
    (measured 2026-08-10, all 16 train repos reported one; none of the 13
    main-prod repos has it).
    """
    if role in ("develop", "staging"):
        return role if role in branches else None
    if "main" in branches:
        return "main"
    if "master" in branches:
        return "master"
    return None


def evaluate_protection(
    name: str, entry: dict, policy: dict, branches: "set[str]",
    org: str, findings: "list[str]", unreadable: "list[str]",
) -> None:
    """Assert one repo's three branch roles against the policy."""
    for role in PROTECTION_ROLES:
        state, reason, overrides = entry["protection"][role]
        branch = resolve_role_branch(role, branches)

        if state == "exempt":
            # An exemption claims the role is out of scope. If the branch turns
            # out to exist and be protected anyway, the exemption is stale and
            # says so -- the same staleness check the caller family applies.
            #
            # Order matters here, and the obvious ordering is wrong. Gating on
            # `probe.error is None` FIRST would let a failed read decide a
            # negative: an unreadable probe would silently mean "not stale" and
            # the run would stay green. It is also strictly worse than that --
            # read_protection() sets `error` when the RULESET call fails even
            # though the classic read already succeeded, so `classic_present`
            # can be known true while the whole finding is suppressed.
            #
            # So: decide on what IS known first, and only fall back to
            # unreadable when nothing was established. (Bugbot, .github#196 --
            # the same defect class this file's header documents, found in the
            # change that documents it.)
            # BOTH SYSTEMS, not just the classic one. This condition read
            # `probe.classic_present` alone until backend#1681, which made a
            # branch protected SOLELY by a ruleset read as unprotected here --
            # so its exemption stayed silently valid. That is precisely the
            # two-systems defect the 20-line header of read_protection()
            # exists to prevent, sitting in the one place that decides whether
            # an exemption is still true. `probe.rulesets` is populated by the
            # very same call; it was simply never consulted.
            if branch is not None:
                probe = read_protection(org, name, branch)
                if probe.classic_present or probe.rulesets:
                    cover = (
                        "classic protection"
                        if probe.classic_present
                        else f"a ruleset ({', '.join(probe.rulesets)})"
                    )
                    findings.append(
                        f"{name}: protection.{role} is `exempt` but {branch} exists "
                        f"and is protected by {cover}. The exemption is stale - "
                        f"promote it to `required`. (reason on file: {reason[:80]})"
                    )
                elif probe.error:
                    unreadable.append(
                        f"{name}: protection of {branch} ({role}, exempt staleness "
                        f"probe) - {probe.error}"
                    )
            continue

        if branch is None:
            findings.append(
                f"{name}: protection.{role} is `{state}` but no branch fills that "
                f"role ({'main/master' if role == 'prod' else role} absent). Either "
                "create it or exempt the role with a written reason."
            )
            continue

        got = read_protection(org, name, branch)
        if got.error:
            unreadable.append(f"{name}: protection of {branch} - {got.error}")
            continue

        want = dict(policy[role])
        want.update(overrides)

        def fail(what: str) -> None:
            tail = f" [divergent: {reason[:60]}]" if state == "divergent" else ""
            findings.append(f"{name}: {branch} ({role}) {what}{tail}")

        if want["classic_protection"] and not got.classic_present:
            # Say what DID cover it, so the reader is not sent to re-derive the
            # ruleset story by hand.
            cover = (
                f" A ruleset does cover it ({', '.join(got.rulesets)}), but the "
                "policy asks for the classic layer too."
                if got.rulesets else " No ruleset covers it either."
            )
            fail(f"has NO classic branch protection.{cover}")
        if got.min_reviews < want["min_reviews"]:
            fail(
                f"requires {got.min_reviews} approving review(s), policy wants "
                f">= {want['min_reviews']}."
            )
        if want["enforce_admins"] is not None and got.enforce_admins != want["enforce_admins"]:
            fail(
                f"enforce_admins={got.enforce_admins}, policy wants "
                f"{want['enforce_admins']} (backend#1276)."
            )
        if want["block_force_pushes"] and not got.block_force_pushes:
            fail("allows force pushes.")
        if want["block_deletions"] and not got.block_deletions:
            fail("allows deletion.")
        if want["require_conversation_resolution"] is not None and (
            got.conversation_resolution != want["require_conversation_resolution"]
        ):
            fail(
                f"conversation resolution={got.conversation_resolution}, policy "
                f"wants {want['require_conversation_resolution']}."
            )
        # `strict` is asserted only where the policy states a bool AND the branch
        # actually has a required-status-checks object to carry it.
        if want["strict"] is not None and got.strict is not None:
            if got.strict != want["strict"]:
                fail(
                    f"required_status_checks.strict={got.strict}, policy wants "
                    f"{want['strict']} (backend#1276 decision 2)."
                )
        # SUBSET, not equality: repos legitimately require their own tests on top
        # of the fleet baseline (backend's Django suite, cli's golangci-lint), and
        # demanding an exact match would turn every one of those into drift. The
        # baseline is a floor.
        missing = [c for c in want["required_checks"] if c not in got.required_checks]
        if missing:
            fail(
                f"does not REQUIRE {', '.join(missing)} - the check may run and go "
                f"red, but nothing stops the merge. Required here: "
                f"{', '.join(sorted(got.required_checks)) or '(none)'}."
            )


def _actor(entry: dict) -> str:
    """Render one bypass actor as a stable, comparable string.

    `OrganizationAdmin` carries no actor_id; teams and apps do. Rendering both
    into one vocabulary keeps the inventory readable and the comparison exact --
    a set difference, not a fuzzy match.
    """
    kind = entry.get("actor_type") or "Unknown"
    ident = entry.get("actor_id")
    return kind if ident is None else f"{kind}:{ident}"


class RepoRuleset:
    """One ruleset, reduced to the properties the inventory asserts."""

    def __init__(self, raw: dict):
        self.id = raw.get("id")
        self.name = raw.get("name") or ""
        self.target = raw.get("target") or ""
        self.enforcement = raw.get("enforcement") or ""
        cond = (raw.get("conditions") or {}).get("ref_name") or {}
        self.includes = [r for r in (cond.get("include") or []) if isinstance(r, str)]
        self.rule_types = {
            r.get("type") for r in (raw.get("rules") or []) if isinstance(r, dict)
        }
        self.merge_methods: "list[str] | None" = None
        for rule in raw.get("rules") or []:
            if isinstance(rule, dict) and rule.get("type") == "pull_request":
                params = rule.get("parameters") or {}
                methods = params.get("allowed_merge_methods")
                if isinstance(methods, list):
                    self.merge_methods = sorted(m for m in methods if isinstance(m, str))
        self.bypass = sorted(
            _actor(a) for a in (raw.get("bypass_actors") or []) if isinstance(a, dict)
        )


def read_rulesets(org: str, name: str) -> "tuple[list[RepoRuleset], str | None]":
    """Every ruleset on a repo, fully expanded.

    TWO CALLS PER RULESET, NOT ONE, AND THE SECOND IS THE POINT. The listing at
    `/rulesets` carries no rules and no bypass actors, and
    `/rules/branches/{b}` -- which read_protection() already uses -- omits
    `bypass_actors` ENTIRELY (verified against the live payload 2026-08-11: its
    rule objects carry only type/parameters/ruleset_source*/ruleset_id). Only
    `/rulesets/{id}` returns them. Asserting a bypass allowlist from either of
    the cheaper endpoints would assert nothing and report a pass.

    Fail-closed: any unreadable ruleset returns an error rather than a short
    list, because "fewer rulesets than exist" reads exactly like "this repo is
    missing its ruleset".
    """
    try:
        # includes_parents=false: the default listing folds in org/enterprise
        # rulesets whose ids then 404 on the per-ruleset get-by-id below, failing
        # the whole audit. We assert only THIS repo's rulesets (Bugbot, .github#212).
        listing = gh_json_array(
            f"repos/{org}/{name}/rulesets?includes_parents=false"
        )
    except GhError as exc:
        return ([], f"rulesets unreadable ({exc.detail})")
    out = []
    for row in listing:
        if not isinstance(row, dict) or row.get("id") is None:
            return ([], "rulesets listing carried an entry with no id")
        try:
            full = gh_json(["api", f"repos/{org}/{name}/rulesets/{row['id']}"])
        except GhError as exc:
            return ([], f"ruleset {row['id']} unreadable ({exc.detail})")
        if not isinstance(full, dict):
            return ([], f"ruleset {row['id']} did not return an object")
        out.append(RepoRuleset(full))
    return (out, None)


def classify(rs: RepoRuleset) -> "str | None":
    """Which policy kind a ruleset is, from its SHAPE.

    Deliberately not its name: `client`'s tag ruleset is named differently from
    its five peers, and a name-keyed check reports it as missing.
    """
    if rs.target == "tag":
        return "tag_trust_root"
    if rs.target == "branch" and "pull_request" in rs.rule_types:
        return "promotion_merge_commit_only"
    return None


def evaluate_rulesets(
    name: str, entry: dict, policy: dict, branches: "set[str]",
    org: str, findings: "list[str]", unreadable: "list[str]",
) -> None:
    """Assert one repo's rulesets against the policy."""
    cells = entry["rulesets"]
    # NO early return for a fully-exempt repo, even though it would save an API
    # call. An exemption still has to be checked for STALENESS -- "this repo has
    # no rulesets" is a claim about reality, and skipping the read would make it
    # unfalsifiable. A repo that exempts everything is exactly the one where an
    # unnoticed ruleset would sit forever. (Caught by this file's own selftest.)
    found, error = read_rulesets(org, name)
    if error:
        unreadable.append(f"{name}: rulesets - {error}")
        return

    by_kind: "dict[str, list[RepoRuleset]]" = {k: [] for k in RULESET_KINDS}
    for rs in found:
        kind = classify(rs)
        if kind in by_kind:
            by_kind[kind].append(rs)

    for kind in RULESET_KINDS:
        state, reason = cells[kind]
        matches = by_kind[kind]
        want = policy[kind]

        if state == "exempt":
            # Same staleness rule the caller and protection families apply: an
            # exemption that is no longer true must say so out loud.
            if matches:
                findings.append(
                    f"{name}: rulesets.{kind} is `exempt` but a matching ruleset "
                    f"exists ({matches[0].name!r}). The exemption is stale - "
                    f"promote it to `required`. (reason on file: {reason[:70]})"
                )
            continue

        if not matches:
            findings.append(
                f"{name}: has NO {kind} ruleset. Nothing enforces "
                f"{'merge-commit-only on its promotion branches' if kind == 'promotion_merge_commit_only' else 'the v* tag trust root'}."
            )
            continue

        for rs in matches:
            label = f"{name}: ruleset {rs.name!r} ({kind})"
            if rs.enforcement != "active":
                findings.append(
                    f"{label} is enforcement={rs.enforcement!r}, not `active` - it "
                    "looks protective and enforces nothing."
                )
            missing_rules = sorted(set(want["require_rule_types"]) - rs.rule_types)
            if missing_rules:
                findings.append(f"{label} is missing rule type(s) {missing_rules}.")
            if want.get("allowed_merge_methods") is not None:
                if rs.merge_methods != sorted(want["allowed_merge_methods"]):
                    findings.append(
                        f"{label} allows merge methods {rs.merge_methods}, policy "
                        f"wants {sorted(want['allowed_merge_methods'])} - a squash "
                        "promotion rewrites the merge-commit ancestry the train reads."
                    )
            # Bypass is an EXACT allowlist, not a subset: an unexpected actor is
            # precisely the finding this exists for (backend#1681 removed a team
            # with `always` tag bypass on two repos that publish on the tag).
            allowed = sorted(want["bypass_actors"])
            if rs.bypass != allowed:
                extra = sorted(set(rs.bypass) - set(allowed))
                gone = sorted(set(allowed) - set(rs.bypass))
                detail = []
                if extra:
                    detail.append(f"unexpected {extra}")
                if gone:
                    detail.append(f"missing {gone}")
                findings.append(f"{label} bypass actors: {'; '.join(detail)}.")
            for ref in want.get("include_refs") or []:
                if ref not in rs.includes:
                    findings.append(f"{label} does not cover {ref} (covers {rs.includes}).")
            for role in want.get("must_cover_roles") or []:
                branch = resolve_role_branch(role, branches)
                if branch is None:
                    continue
                if f"refs/heads/{branch}" not in rs.includes:
                    findings.append(
                        f"{label} does not cover {role} (refs/heads/{branch}); "
                        f"covers {rs.includes}."
                    )


def render_matrix(matrix: dict, inventory: dict) -> "list[str]":
    """The one screen (backend#1608 increment 3).

    The audit has always known each repo's state per family and only ever
    printed the failures, so a green run said "no drift" and nothing about what
    was actually covered -- you could not tell a fleet that conforms from a fleet
    that was barely checked. This renders every repo x family cell, so coverage
    and drift are visible in the same glance.

    Cells:
      OK   the family was evaluated and matched
      N    that many findings in that family
      ?    that family could not be READ (never conflated with OK -- an
           unreadable family is the state this guard exists to refuse)
    """
    families = ("callers", "copies", "protection", "rulesets")
    out = [
        "<details><summary><b>Conformance matrix</b> - every repo x every family, "
        "including what passed</summary>",
        "",
        "| repo | train | " + " | ".join(families) + " |",
        "|---|---|" + "---|" * len(families),
    ]
    for name in sorted(matrix):
        cells = matrix[name]
        entry = inventory["repos"].get(name, {})
        train = "yes" if entry.get("release_train") else "-"
        if cells.get("unread"):
            out.append(f"| `{name}` | {train} | " + " | ".join(["?"] * len(families)) + " |")
            continue
        rendered = []
        for fam in families:
            # Only protection and rulesets can be partially unread: callers and
            # copies come from the repo read, which fails the whole row first.
            unread = cells.get(f"{fam}_unread", 0) if fam in ("protection", "rulesets") else 0
            n = cells.get(fam, 0)
            if unread:
                rendered.append("?")
            elif n:
                rendered.append(f"**{n}**")
            else:
                rendered.append("OK")
        out.append(f"| `{name}` | {train} | " + " | ".join(rendered) + " |")
    out += ["", "</details>", ""]
    return out


# ------------------------------------------------------------------ evaluation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", default="repo-inventory.yml")
    parser.add_argument(
        "--source-dir", default=".",
        help="checkout of tracebloc/.github, holding the canonical copies",
    )
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    parser.add_argument("--output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()

    inventory = load_inventory(args.inventory)
    org = inventory["org"]
    pinned_ref = inventory["pinned_ref"]
    source_repo = inventory["source_repo"]
    reusables = list(inventory["reusables"])
    copies = list(inventory["copies"])
    check_source_reusables(args.source_dir, reusables)
    source_shas = load_source_copies(args.source_dir, copies)

    active = list_active_repos(org)
    train = load_release_train(org)

    findings: "list[str]" = []
    unreadable: "list[str]" = []
    # Kept separate until `evaluated` is computed - see the note at the call site.
    # Holds BOTH protection and ruleset read failures (backend#1681). Kept as one
    # bucket deliberately: both are "this repo's caller/copy audit SUCCEEDED, a
    # different control plane did not", which is the distinction this list exists
    # to preserve. Protection and ruleset read-failures go in SEPARATE buckets so
    # each names exactly which layer failed: a rulesets-only outage must not read
    # as branch protection failing, and vice versa (Bugbot, .github#212).
    protection_unreadable: "list[str]" = []
    ruleset_unreadable: "list[str]" = []

    untracked = sorted(set(active) - set(inventory["repos"]))
    for name in untracked:
        findings.append(
            f"{name}: active in {org} but absent from repo-inventory.yml. A new repo "
            "arrives with no callers and nothing required of it; add an entry."
        )
    stale = sorted(set(inventory["repos"]) - set(active))
    for name in stale:
        findings.append(
            f"{name}: in repo-inventory.yml but not an active repo in {org} "
            "(archived, forked, renamed or deleted). Remove or correct the entry."
        )

    audited = sorted(set(active) & set(inventory["repos"]))
    if not audited:
        die(
            "no repo appears in both the org listing and the inventory. Nothing was "
            "evaluated, so this cannot be reported as no drift."
        )

    # ONE SCREEN (backend#1608 increment 3). The audit already knows every
    # repo's state per family; until now it only ever emitted the failures, so a
    # reader could see what was wrong but never what was covered. `matrix` records
    # a per-repo, per-family count so the report can show the whole fleet at once.
    #
    # Counted by DELTA around each family's block rather than by parsing the
    # finding strings: the strings are prose written for humans, and keying a
    # table off them would break the first time one is reworded.
    matrix: "dict[str, dict]" = {}

    for name in audited:
        entry = inventory["repos"][name]
        meta = active[name]
        read = read_repo(org, name, meta, copies)
        if not read.ok:
            for problem in read.errors:
                unreadable.append(f"{name}: {problem}")
            print(f"  ?? {name} - NOT EVALUATED: {'; '.join(read.errors)}")
            matrix[name] = {"unread": True}
            continue

        print(f"  -- {name} @ {read.branch}")

        if meta["visibility"] != entry["visibility"]:
            findings.append(
                f"{name}: visibility is {meta['visibility']!r} in {org}, "
                f"{entry['visibility']!r} in the inventory. Visibility decides "
                "which caller set applies here."
            )
        on_train = name in train
        if on_train != entry["release_train"]:
            findings.append(
                f"{name}: release-train/repos.yml says on-train={on_train}, the "
                f"inventory says {entry['release_train']}. A repo joining the train "
                "without an fr-gate caller is an ungated staging -> prod hop."
            )

        _m: "dict[str, int|bool]" = {}
        _mark = len(findings)

        for reusable in reusables:
            state, _reason = entry["callers"][reusable]
            hits = read.callers.get(reusable, [])
            if state == "required":
                if not hits:
                    findings.append(
                        f"{name}: MISSING required caller for {reusable} on "
                        f"{read.branch}."
                    )
                    continue
                for filename, ref in hits:
                    if ref != pinned_ref:
                        findings.append(
                            f"{name}: {filename} calls {reusable}@{ref}, expected "
                            f"@{pinned_ref}."
                        )
            elif hits:
                where = ", ".join(sorted(f for f, _ in hits))
                findings.append(
                    f"{name}: {reusable} is marked `exempt` but a caller exists "
                    f"({where}). The exemption is stale - promote it to `required` "
                    "or delete the caller."
                )

        for reusable, hits in sorted(read.callers.items()):
            if reusable not in reusables and reusable not in copies:
                where = ", ".join(sorted(f for f, _ in hits))
                findings.append(
                    f"{name}: {where} calls {reusable}, which is not listed under "
                    "`reusables` in repo-inventory.yml. Either the reusable is new "
                    "and the inventory has not caught up, or the call is a typo "
                    "that has never run."
                )

        _m["callers"] = len(findings) - _mark
        _mark = len(findings)

        for copy_name in copies:
            state, _reason = entry["copies"][copy_name]
            actual = read.copies.get(copy_name)
            if state == "required":
                if actual is None:
                    findings.append(
                        f"{name}: MISSING required copy {copy_name} on {read.branch}."
                    )
                elif name == source_repo:
                    # This repo holds the canonical file. Comparing the audit branch
                    # against the checkout would flag any PR that edits a copy, so
                    # only presence is asserted here; load_source_copies() already
                    # proved the checked-out original is readable.
                    print(f"     {copy_name}: source of truth, presence only")
                elif actual != source_shas[copy_name]:
                    findings.append(
                        f"{name}: {copy_name} has DRIFTED from the copy in "
                        f"{org}/.github (blob {actual[:12]} vs "
                        f"{source_shas[copy_name][:12]}). It is a copy, not a "
                        "caller, so nothing else would ever notice."
                    )
            elif state == "divergent":
                if actual is None:
                    findings.append(
                        f"{name}: {copy_name} is marked `divergent`, which asserts "
                        "the file is present, but it is absent."
                    )
                elif name != source_repo and actual == source_shas[copy_name]:
                    findings.append(
                        f"{name}: {copy_name} is marked `divergent` but now matches "
                        f"{org}/.github exactly. The divergence was resolved - "
                        "change the entry to `required`."
                    )
            elif actual is not None:
                findings.append(
                    f"{name}: {copy_name} is marked `exempt` but the file exists. "
                    "The exemption is stale."
                )

        # Protection is read from the branch LIST already gathered by read_repo,
        # so it costs no extra enumeration and inherits its pagination.
        #
        # Into its OWN list, not the shared one. `evaluated` below drives a die()
        # that discards the entire report, and `unreadable` means "this repo's
        # caller/copy state could not be read at all". A fleet-wide protection
        # outage (rate limit, auth, GitHub incident) is a different thing: the
        # caller audit for those repos SUCCEEDED, and folding protection failures
        # into the same counter would make every repo look unreadable, abort, and
        # throw away real caller findings before anything was written.
        # Fail-closed means the run still goes RED - it must not mean the results
        # are destroyed. (Bugbot, .github#196.)
        _m["copies"] = len(findings) - _mark
        _mark = len(findings)
        _pmark = len(protection_unreadable)

        evaluate_protection(
            name, entry, inventory["protection_policy"], read.branches,
            org, findings, protection_unreadable,
        )
        # Rulesets get their OWN unreadable bucket (not protection's): a rulesets
        # API failure is neither "callers unreadable" nor "protection unreadable",
        # and must not abort the run or discard real caller findings.
        _m["protection"] = len(findings) - _mark
        _m["protection_unread"] = len(protection_unreadable) - _pmark
        _mark = len(findings)
        _rmark = len(ruleset_unreadable)

        evaluate_rulesets(
            name, entry, inventory["ruleset_policy"], read.branches,
            org, findings, ruleset_unreadable,
        )
        _m["rulesets"] = len(findings) - _mark
        _m["rulesets_unread"] = len(ruleset_unreadable) - _rmark
        matrix[name] = _m

    # Computed from repo-read failures ONLY, before the two lists are merged.
    evaluated = len(audited) - len({line.split(":", 1)[0] for line in unreadable})
    if evaluated <= 0:
        die(
            f"all {len(audited)} inventoried repos were unreadable: "
            + "; ".join(unreadable)
        )
    # Merged only now, so protection failures are REPORTED and still fail the run
    # without ever being able to trigger the abort above.
    unreadable.extend(protection_unreadable)
    unreadable.extend(ruleset_unreadable)

    report = [
        "### Repo conformance drift",
        "",
        f"Inventory: **{len(inventory['repos'])}** repos x **{len(reusables)}** "
        f"reusables + **{len(copies)}** copies + **{len(PROTECTION_ROLES)}** "
        f"branch-protection roles + **{len(RULESET_KINDS)}** ruleset kinds. "
        f"Audited **{evaluated}** of **{len(audited)}** "
        "on the develop-first branch.",
        "",
        "Protection is read from **both** GitHub protection systems (classic + "
        "rulesets); a ruleset-only branch 404s on the classic endpoint and would "
        "otherwise read as unprotected.",
        "",
    ]
    report.extend(render_matrix(matrix, inventory))
    if findings:
        report.append(f"**{len(findings)} drift finding(s):**")
        report.append("")
        report.extend(f"- {line}" for line in findings)
        report.append("")
    if unreadable:
        # Name WHICH family failed. `unreadable` now carries both, and a message
        # that says "caller state is unknown" after a protection-only outage
        # misstates what happened and quietly undoes the separation above
        # (Bugbot, .github#196).
        caller_failed = (
            len(unreadable) - len(protection_unreadable) - len(ruleset_unreadable)
        )
        bits = []
        if caller_failed:
            bits.append(f"**{caller_failed} repo read(s) FAILED** (caller/copy state UNKNOWN)")
        if protection_unreadable:
            bits.append(
                f"**{len(protection_unreadable)} branch-protection read(s) FAILED** "
                "(protection state UNKNOWN for the repos named below; caller/copy "
                "state for those repos WAS read)"
            )
        if ruleset_unreadable:
            bits.append(
                f"**{len(ruleset_unreadable)} ruleset read(s) FAILED** "
                "(ruleset state UNKNOWN for the repos named below; caller/copy and "
                "branch-protection state for those repos WAS read)"
            )
        report.append(
            " and ".join(bits)
            + ". These are not known to comply; the run fails on them rather than "
            "reporting all-clear:"
        )
        report.append("")
        report.extend(f"- {line}" for line in unreadable)
        report.append("")
    if not findings and not unreadable:
        report.append("No drift. Every repo read, every entry matched.")
        report.append("")

    text = "\n".join(report)
    print(text)
    if args.summary:
        try:
            with open(args.summary, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError as exc:
            # Cosmetic only: the exit code below, not the summary, decides the run.
            sys.stderr.write(f"::warning::could not write step summary: {exc}\n")

    if args.output:
        # The heredoc delimiter must not occur in the body, or the runner rejects
        # the whole output file. Findings contain repo and file names, so this is
        # defence against a hostile branch name rather than a likely accident.
        delimiter = "CALLER_DRIFT_EOF"
        body = text.replace(delimiter, "CALLER-DRIFT-EOF")
        if len(body) > 48000:
            body = body[:48000] + "\n\n[...truncated; see the run log for the rest]"
        try:
            with open(args.output, "a", encoding="utf-8") as handle:
                handle.write(f"findings={len(findings)}\n")
                handle.write(f"unreadable={len(unreadable)}\n")
                handle.write(f"evaluated={evaluated}\n")
                handle.write(f"report<<{delimiter}\n")
                handle.write(body + "\n")
                handle.write(f"{delimiter}\n")
        except OSError as exc:
            # The reporting step keys off these outputs. If they cannot be written
            # the run must still fail rather than look like a clean pass.
            die(f"could not write step outputs: {exc}")

    if unreadable:
        caller_failed = (
            len(unreadable) - len(protection_unreadable) - len(ruleset_unreadable)
        )
        parts = []
        if caller_failed:
            parts.append(f"{caller_failed} repo read(s) failed - caller/copy state UNKNOWN")
        if protection_unreadable:
            parts.append(
                f"{len(protection_unreadable)} branch-protection read(s) failed - "
                "protection state UNKNOWN"
            )
        if ruleset_unreadable:
            parts.append(
                f"{len(ruleset_unreadable)} ruleset read(s) failed - "
                "ruleset state UNKNOWN"
            )
        sys.stderr.write("::error::" + "; ".join(parts) + ".\n")
        return 2
    if findings:
        sys.stderr.write(
            f"::error::{len(findings)} repo-conformance drift finding(s).\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as unexpected:  # noqa: BLE001 - deliberate catch-all
        # An unhandled exception must not be reported as exit 1 ("drift found"),
        # which is what a bare traceback would give the caller. A crash means the
        # comparison did not complete, which is exit 2: could not evaluate.
        import traceback

        traceback.print_exc()
        sys.stderr.write(
            f"::error::the guard crashed ({unexpected.__class__.__name__}). Caller "
            "state across the org is UNKNOWN - this is not a drift report and not "
            "an all-clear.\n"
        )
        raise SystemExit(2) from unexpected
