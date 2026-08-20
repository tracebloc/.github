#!/usr/bin/env python3
"""Offline self-test for scripts/caller-drift.py (tracebloc/backend#1415).

Every PR in this epic has turned up the same defect class: a failure path that
reports success, or a guard whose precondition fails open. For a drift guard that
is the whole product, so the fail-closed paths are asserted here rather than
trusted. No network, no token: `gh` is replaced with a stub, and the schema cases
run against generated inventories.

Exit 0 when every path fails the way it is supposed to.
"""

from __future__ import annotations

import ast
import base64
import copy
import importlib.util
import inspect
import json
import re
import pathlib
import os
import sys
import tempfile
import textwrap

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, os.pardir, "caller-drift.py")

_spec = importlib.util.spec_from_file_location("caller_drift", GUARD)
if _spec is None or _spec.loader is None:
    sys.exit(f"cannot import {GUARD}")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

RESULTS: "list[tuple[bool, str, str]]" = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        {detail}")


# ------------------------------------------------------------- schema failures

def _policy(**over):
    base = {
        "classic_protection": True,
        "min_reviews": 1,
        "enforce_admins": None,
        "block_force_pushes": True,
        "block_deletions": True,
        "require_conversation_resolution": True,
        "strict": False,
        "required_checks": ["ci / build"],
        "bypass_reviews": [],
    }
    base.update(over)
    return base


MINIMAL = {
    "schema_version": 2,
    "org": "acme",
    "pinned_ref": "main",
    "audit_branch": "develop-first-on-train",
    "source_repo": "hub",
    "reusables": ["a.yml"],
    "copies": ["c.yml"],
    "quality_files": ["GUIDE.md"],
    "caller_inputs": {"a.yml": {"soft-fail": False}},
    "protection_policy": {
        "develop": _policy(),
        "staging": _policy(),
        "prod": _policy(enforce_admins=True),
    },
    "ruleset_policy": {
        "promotion_merge_commit_only": {
            "target": "branch",
            "require_rule_types": ["pull_request"],
            "allowed_merge_methods": ["merge"],
            "must_cover_roles": ["staging", "prod"],
            "bypass_actors": [],
        },
        "tag_trust_root": {
            "target": "tag",
            "require_rule_types": ["creation", "update", "deletion"],
            "include_refs": ["refs/tags/v*"],
            "bypass_actors": ["OrganizationAdmin"],
        },
    },
    "repos": {
        "hub": {
            "visibility": "public",
            "release_train": False,
            "protection": {
                "develop": "required",
                "staging": "required",
                "prod": "required",
            },
            "callers": {"a.yml": "required"},
            "copies": {"c.yml": "required"},
            "quality_files": {"GUIDE.md": "required"},
            "rulesets": {
                "promotion_merge_commit_only": "required",
                "tag_trust_root": {"exempt": "publishes no v* tags"},
            },
        },
    },
}


def expect_schema_failure(name: str, mutate) -> None:
    """A malformed inventory must raise SystemExit(2), never parse permissively."""
    data = copy.deepcopy(MINIMAL)
    mutate(data)
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
        yaml.safe_dump(data, handle)
        path = handle.name
    try:
        guard.load_inventory(path)
    except SystemExit as exc:
        record(exc.code == 2, name, f"SystemExit({exc.code})")
    else:
        record(False, name, "load_inventory ACCEPTED a malformed inventory")
    finally:
        os.unlink(path)


def _drop(key):
    return lambda d: d.pop(key)


expect_schema_failure("unknown top-level key rejected",
                      lambda d: d.update({"typoed_key": 1}))
expect_schema_failure("missing pinned_ref rejected", _drop("pinned_ref"))
expect_schema_failure("missing repos rejected", _drop("repos"))
expect_schema_failure("wrong schema_version rejected",
                      lambda d: d.update({"schema_version": 99}))
expect_schema_failure("audit_branch other than develop-first-on-train rejected",
                      lambda d: d.update({"audit_branch": "default"}))
expect_schema_failure("empty reusables rejected",
                      lambda d: d.update({"reusables": []}))
expect_schema_failure("blank org rejected", lambda d: d.update({"org": "  "}))
expect_schema_failure("source_repo with no repos entry rejected",
                      lambda d: d.update({"source_repo": "absent"}))
expect_schema_failure("reusable listed as a copy too rejected",
                      lambda d: d.update({"copies": ["a.yml"]}))
expect_schema_failure("duplicate reusable rejected",
                      lambda d: d.update({"reusables": ["a.yml", "a.yml"]}))
expect_schema_failure("repo with an unknown key rejected",
                      lambda d: d["repos"]["hub"].update({"extra": 1}))
expect_schema_failure("repo missing release_train rejected",
                      lambda d: d["repos"]["hub"].pop("release_train"))
expect_schema_failure("non-boolean release_train rejected",
                      lambda d: d["repos"]["hub"].update({"release_train": "yes"}))
expect_schema_failure("visibility other than public/private rejected",
                      lambda d: d["repos"]["hub"].update({"visibility": "internal"}))

# The headline schema rule: absence is never implicit.
expect_schema_failure("MISSING caller key is a failure, not a default",
                      lambda d: d["repos"]["hub"]["callers"].clear())
expect_schema_failure("MISSING copy key is a failure, not a default",
                      lambda d: d["repos"]["hub"]["copies"].clear())
expect_schema_failure("caller key not in the reusables list rejected",
                      lambda d: d["repos"]["hub"]["callers"].update({"z.yml": "required"}))

# The other headline rule: an exemption must carry a written reason.
expect_schema_failure("exempt with an empty reason rejected",
                      lambda d: d["repos"]["hub"]["callers"].update({"a.yml": {"exempt": ""}}))
expect_schema_failure("exempt with a whitespace reason rejected",
                      lambda d: d["repos"]["hub"]["callers"].update({"a.yml": {"exempt": "  \n"}}))
expect_schema_failure("exempt with a null reason rejected",
                      lambda d: d["repos"]["hub"]["callers"].update({"a.yml": {"exempt": None}}))
expect_schema_failure("bare 'exempt' scalar rejected",
                      lambda d: d["repos"]["hub"]["callers"].update({"a.yml": "exempt"}))
expect_schema_failure("unknown state rejected",
                      lambda d: d["repos"]["hub"]["callers"].update({"a.yml": {"maybe": "x"}}))
expect_schema_failure("two states in one cell rejected",
                      lambda d: d["repos"]["hub"]["callers"].update(
                          {"a.yml": {"exempt": "x", "divergent": "y"}}))
expect_schema_failure("copies-only 'divergent' rejected on a caller",
                      lambda d: d["repos"]["hub"]["callers"].update(
                          {"a.yml": {"divergent": "not valid here"}}))

# ------------------------------------------------- protection schema (#1608 inc 2)
expect_schema_failure("missing protection_policy rejected", _drop("protection_policy"))
expect_schema_failure("protection_policy missing a role rejected",
                      lambda d: d["protection_policy"].pop("staging"))
expect_schema_failure("protection_policy with an unknown role rejected",
                      lambda d: d["protection_policy"].update({"qa": _policy()}))
expect_schema_failure("policy missing a key is a failure, not a default",
                      lambda d: d["protection_policy"]["develop"].pop("min_reviews"))
expect_schema_failure("policy with an unknown key rejected",
                      lambda d: d["protection_policy"]["develop"].update({"nope": True}))
expect_schema_failure("negative min_reviews rejected",
                      lambda d: d["protection_policy"]["develop"].update({"min_reviews": -1}))
expect_schema_failure("non-int min_reviews rejected",
                      lambda d: d["protection_policy"]["develop"].update({"min_reviews": "1"}))
# classic_protection/block_* may NOT be un-asserted: null there would silently
# switch off the assertions that matter most.
expect_schema_failure("null classic_protection rejected",
                      lambda d: d["protection_policy"]["develop"].update({"classic_protection": None}))
expect_schema_failure("null block_deletions rejected",
                      lambda d: d["protection_policy"]["develop"].update({"block_deletions": None}))

expect_schema_failure("repo missing the protection block rejected",
                      lambda d: d["repos"]["hub"].pop("protection"))
expect_schema_failure("MISSING protection role is a failure, not a default",
                      lambda d: d["repos"]["hub"]["protection"].pop("prod"))
expect_schema_failure("unknown protection role rejected",
                      lambda d: d["repos"]["hub"]["protection"].update({"qa": "required"}))
expect_schema_failure("protection exempt with no reason rejected",
                      lambda d: d["repos"]["hub"]["protection"].update({"prod": {"exempt": "  "}}))
expect_schema_failure("bare 'exempt' scalar rejected on protection",
                      lambda d: d["repos"]["hub"]["protection"].update({"prod": "exempt"}))
# `divergent` must NAME the deviating key. A blanket divergence would switch off
# every assertion at once - the failure this shape exists to prevent.
expect_schema_failure("protection divergent as a bare string rejected",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": "just a reason"}}))
expect_schema_failure("protection divergent naming no key rejected",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": {"reason": "because"}}}))
expect_schema_failure("protection divergent with no reason rejected",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": {"min_reviews": 0}}}))
expect_schema_failure("protection divergent cannot override classic_protection",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": {"reason": "x", "classic_protection": False}}}))
expect_schema_failure("protection divergent cannot override block_deletions",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": {"reason": "x", "block_deletions": False}}}))

# Override VALUES are validated too, not just the key names. Without this a cell
# that looks like a narrow documented divergence can neutralise the assertion it
# claims merely to adjust. (Bugbot, .github#196.)
expect_schema_failure("divergent min_reviews cannot be negative",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": {"reason": "x", "min_reviews": -1}}}))
expect_schema_failure("divergent min_reviews cannot be a bool (bool is an int in Python)",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": {"reason": "x", "min_reviews": True}}}))
expect_schema_failure("divergent min_reviews cannot be a string",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": {"reason": "x", "min_reviews": "0"}}}))
expect_schema_failure("divergent enforce_admins cannot be null (that un-asserts it)",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": {"reason": "x", "enforce_admins": None}}}))
expect_schema_failure("divergent strict cannot be null",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": {"reason": "x", "strict": None}}}))
expect_schema_failure("divergent strict cannot be a string",
                      lambda d: d["repos"]["hub"]["protection"].update(
                          {"prod": {"divergent": {"reason": "x", "strict": "false"}}}))
# The same bool-is-int trap at policy level.
expect_schema_failure("policy min_reviews cannot be a bool",
                      lambda d: d["protection_policy"]["develop"].update({"min_reviews": True}))

# A well-formed inventory must still load, or the tests above prove nothing.
with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as _h:
    yaml.safe_dump(copy.deepcopy(MINIMAL), _h)
    _ok_path = _h.name
try:
    loaded = guard.load_inventory(_ok_path)
    cell = loaded["repos"]["hub"]["callers"]["a.yml"]
    record(cell == ("required", ""), "positive control: valid inventory loads", str(cell))
except SystemExit as exc:
    record(False, "positive control: valid inventory loads", f"rejected with {exc.code}")
finally:
    os.unlink(_ok_path)

# A missing inventory file is a failure, not an empty inventory.
try:
    guard.load_inventory(os.path.join(tempfile.gettempdir(), "definitely-not-here-9f3a.yml"))
except SystemExit as exc:
    record(exc.code == 2, "absent inventory file rejected", f"SystemExit({exc.code})")
else:
    record(False, "absent inventory file rejected", "returned normally")

# A missing canonical copy must fail, or every copy comparison would pass.
try:
    guard.load_source_copies(tempfile.gettempdir(), ["surely-not-a-workflow-9f3a.yml"])
except SystemExit as exc:
    record(exc.code == 2, "absent canonical copy rejected", f"SystemExit({exc.code})")
else:
    record(False, "absent canonical copy rejected", "returned normally")


# ------------------------------------------------- unreadable-repo failure paths

COPIES = ["add-to-kanban.yml", "stale-backlog.yml"]
QFILES = ["CLAUDE.md", ".cursor/BUGBOT.md"]
# THE FIXTURE REPO IS ON THE TRAIN, and that is now load-bearing rather than
# incidental (backend#2214). `read_repo` audits `develop` for a train repo and the
# DEFAULT branch for one the train does not promote, so a fixture without
# `release_train` would silently exercise the non-train path while every case here
# is written about develop-first. `META_OFF_TRAIN` below is the other side.
META = {"visibility": "private", "default_branch": "main", "release_train": True}
META_OFF_TRAIN = {"visibility": "private", "default_branch": "main",
                  "release_train": False}
TREE_ONE = json.dumps({
    "truncated": False,
    "tree": [{"type": "blob",
              "path": ".github/workflows/fr-gate-caller.yml",
              "sha": "abc123"}],
})


def stub(handler) -> None:
    guard.gh = handler
    guard.gh_json = lambda args: json.loads(handler(args))


def blob(body: bytes) -> str:
    return json.dumps({"encoding": "base64",
                       "content": base64.b64encode(body).decode()})


def expect_unreadable(name: str, handler, needle: str) -> None:
    """A failed read must produce an UNREADABLE record, never 'no caller found'."""
    stub(handler)
    read = guard.read_repo("acme", "repo", META, COPIES, QFILES)
    ok = (not read.ok) and any(needle in err for err in read.errors)
    record(ok, name, str(read.errors))


def _branches(args) -> bool:
    return args[1].endswith("/branches")


def _tree(args) -> bool:
    return "git/trees" in args[1]


def _raise(status, message):
    def handler(_args):
        raise guard.GhError(status, message)
    return handler


expect_unreadable(
    "403 on the branch list is unreadable, not 'no callers'",
    _raise(403, "gh: Resource not accessible by integration (HTTP 403)"),
    "branch list unreadable")

expect_unreadable(
    "a repo with no branches is unreadable",
    lambda args: "" if _branches(args) else "{}",
    "no branches")

expect_unreadable(
    "default branch absent from the branch list is unreadable",
    lambda args: "trunk\n" if _branches(args) else "{}",
    "cannot choose an audit branch")


def _tree_ratelimited(args):
    if _branches(args):
        return "main\n"
    raise guard.GhError(403, "gh: API rate limit exceeded (HTTP 403)")


expect_unreadable("a rate-limited tree read is unreadable",
                  _tree_ratelimited, "tree of main unreadable")

expect_unreadable(
    "a TRUNCATED git tree is unreadable, never 'no workflows'",
    lambda args: "main\n" if _branches(args) else json.dumps({"truncated": True, "tree": []}),
    "truncated")

expect_unreadable(
    "a tree payload with no `tree` array is unreadable",
    lambda args: "main\n" if _branches(args) else json.dumps({"truncated": False}),
    "no `tree` array")

expect_unreadable(
    "a tree entry with no sha is unreadable",
    lambda args: "main\n" if _branches(args) else json.dumps(
        {"truncated": False,
         "tree": [{"type": "blob", "path": ".github/workflows/x.yml"}]}),
    "no sha")


def _blob_500(args):
    if _branches(args):
        return "main\n"
    if _tree(args):
        return TREE_ONE
    raise guard.GhError(500, "gh: Internal Server Error (HTTP 500)")


expect_unreadable("a 500 on the blob read is unreadable",
                  _blob_500, "content unreadable")

expect_unreadable(
    "an UNPARSEABLE workflow is unreadable, never 'contains no caller'",
    lambda args: "main\n" if _branches(args) else (
        TREE_ONE if _tree(args) else blob(b"jobs:\n  broken: [\n")),
    "unparseable YAML")

expect_unreadable(
    "an unexpected blob encoding is unreadable",
    lambda args: "main\n" if _branches(args) else (
        TREE_ONE if _tree(args) else json.dumps({"encoding": "none", "content": None})),
    "content unreadable")


# ------------------------------------------------------------ positive controls

def _good(args):
    if _branches(args):
        return "main\ndevelop\n"
    if _tree(args):
        if "trees/develop" not in args[1]:
            # NOT `raise`. This positive control asserts the train repo is audited on
            # develop, and a hard raise here ABORTS THE WHOLE SUITE -- so a mutation
            # that changes the resolver reported "CRASH" and hid every other case it
            # would also have broken. Returning an empty tree makes the control fail
            # cleanly and lets the rest of the run report (backend#2214).
            return json.dumps({"truncated": False, "tree": []})
        return TREE_ONE
    return blob(
        b"name: FR gate\n"
        b"on:\n  pull_request:\n"
        b"jobs:\n  gate:\n"
        b"    uses: tracebloc/.github/.github/workflows/fr-gate.yml@main\n"
        b"    secrets: inherit\n")


# ---------------------------------------- which branch gets audited (backend#2214)
#
# Plain develop-first read `develop` wherever that branch existed. For a repo the
# train does NOT promote there is no promotion pipeline, so its DEFAULT branch is
# where it ships -- and preferring a stray `develop` audits a branch nobody merges
# to. `rfcs` is the live case: default `main`, a lagging `develop`, and the audit
# reported drift against a change correctly landed on `main`.
#
# BOTH DIRECTIONS ARE PINNED. One case alone cannot fail: assert only the train side
# and the non-train path is untested; assert only the non-train side and a resolver
# that always uses the default branch passes. Each of these reddens a different
# mutation.
def _branch_probe(box):
    """A handler that records which ref the tree was requested for."""
    def h(args):
        if _branches(args):
            return "main\ndevelop\n"
        if _tree(args):
            box.append(args[1])
            return TREE_ONE
        return blob(b"name: x\non:\n  push:\njobs:\n  j:\n    uses: a/b/.github/workflows/c.yml@main\n")
    return h

box = []
stub(_branch_probe(box))
guard.read_repo("acme", "repo", META, COPIES, QFILES)
record(any("trees/develop" in u for u in box),
       "a TRAIN repo is audited on develop even when it defaults to main",
       f"requested {box!r}")

box = []
stub(_branch_probe(box))
guard.read_repo("acme", "repo", META_OFF_TRAIN, COPIES, QFILES)
record(any("trees/main" in u for u in box) and not any("trees/develop" in u for u in box),
       "a NON-TRAIN repo is audited on its default branch, not a stray develop",
       f"requested {box!r} — this is the rfcs case; develop here lags and nobody merges to it")

stub(_good)
read = guard.read_repo("acme", "repo", META, COPIES, QFILES)
record(
    read.ok
    and read.branch == "develop"
    and read.callers.get("fr-gate.yml") == [("fr-gate-caller.yml", "main", {})],
    "develop-first branch choice, and a caller under a NON-matching filename is found",
    f"branch={read.branch} callers={read.callers}")


def _decoys(args):
    if _branches(args):
        return "main\n"
    if _tree(args):
        return TREE_ONE
    return blob(
        b"name: decoys\n"
        b"# uses: tracebloc/.github/.github/workflows/code-quality.yml@main\n"
        b"on:\n  push:\n"
        b"jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
        b"      - run: |\n"
        b"          echo 'uses: tracebloc/.github/.github/workflows/fr-gate.yml@main'\n")


stub(_decoys)
read = guard.read_repo("acme", "repo", META, COPIES, QFILES)
record(read.ok and read.callers == {},
       "a commented-out `uses:` and one inside a run script are NOT callers",
       f"callers={read.callers}")


def _unpinned(args):
    if _branches(args):
        return "main\n"
    if _tree(args):
        return TREE_ONE
    return blob(
        b"jobs:\n  gate:\n"
        b"    uses: tracebloc/.github/.github/workflows/fr-gate.yml@v1.2.3\n")


stub(_unpinned)
read = guard.read_repo("acme", "repo", META, COPIES, QFILES)
record(read.ok and read.callers.get("fr-gate.yml") == [("fr-gate-caller.yml", "v1.2.3", {})],
       "a caller on an unexpected ref is captured with its ref, for the pin check",
       f"callers={read.callers}")


# THE INPUTS ARE CAPTURED, not walked past (backend#1977). `with:` is a sibling
# key of `uses:`; the old collector kept only the string, so by the time anything
# compared a caller to the inventory the inputs were gone. Two proofs: the
# permissive value is captured verbatim (so the audit can call it a finding), and
# an absent `with:` reads as {} rather than as "no caller".
def _soft_fail_true(args):
    if _branches(args):
        return "main\n"
    if _tree(args):
        return TREE_ONE
    return blob(
        b"jobs:\n  gate:\n"
        b"    uses: tracebloc/.github/.github/workflows/fr-gate.yml@main\n"
        b"    with:\n      soft-fail: true\n")


stub(_soft_fail_true)
read = guard.read_repo("acme", "repo", META, COPIES, QFILES)
record(
    read.ok
    and read.callers.get("fr-gate.yml") == [("fr-gate-caller.yml", "main", {"soft-fail": True})],
    "a caller's `with:` inputs are captured, so an unarmed caller is visible",
    f"callers={read.callers}")


def _copy_sha(args):
    if _branches(args):
        return "main\n"
    if _tree(args):
        return json.dumps({"truncated": False, "tree": [
            {"type": "blob", "path": ".github/workflows/add-to-kanban.yml", "sha": "deadbeef"}]})
    return blob(b"name: add to kanban\non:\n  issues:\n")


stub(_copy_sha)
read = guard.read_repo("acme", "repo", META, COPIES, QFILES)
record(read.ok and read.copies.get("add-to-kanban.yml") == "deadbeef",
       "a copy is recorded by blob sha so content can be compared",
       f"copies={read.copies}")

record(guard.blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a",
       "blob_sha matches git's own object id",
       guard.blob_sha(b"hello\n"))


# --------------------------------------------- quality files (#1608 increment 5)
#
# A presence family is the easiest thing in this file to write as inert
# verification: "the path is in the tree" passes for a zero-byte file, for a
# symlink to nowhere, and -- worst -- for a repo whose tree was never read.
# The cases below pin all three, plus both directions of the exemption.

def _qf_entry(path, size=120, mode="100644", type_="blob", with_size=True):
    entry = {"type": type_, "path": path, "mode": mode}
    if with_size:
        entry["size"] = size
    return entry


def _qf_tree(*items, truncated=False):
    return json.dumps({"truncated": truncated, "tree": list(items)})


def _stub_qf_tree(payload):
    """Branch list plus one tree read; no workflow blobs are involved."""
    stub(lambda args: "main\n" if _branches(args) else payload)


_stub_qf_tree(_qf_tree(_qf_entry("CLAUDE.md", size=5901),
                       _qf_entry(".cursor/BUGBOT.md", size=4193)))
read = guard.read_repo("acme", "repo", META, COPIES, QFILES)
record(read.ok
       and read.quality_files.get("CLAUDE.md", {}).get("size") == 5901
       and read.quality_files.get(".cursor/BUGBOT.md", {}).get("mode") == "100644",
       "quality files: a present file is recorded with its size and mode",
       f"quality_files={read.quality_files}")

# Absence from a SUCCESSFUL read is the only legitimate way to conclude "absent".
_stub_qf_tree(_qf_tree(_qf_entry("README.md")))
read = guard.read_repo("acme", "repo", META, COPIES, QFILES)
record(read.ok and read.quality_files == {},
       "quality files: absent from a fully-read tree is recorded as absent",
       f"quality_files={read.quality_files}")

# THE FAIL-CLOSED CASE. A truncated tree omits paths, so "not in the tree" is not
# knowledge. read_repo must fail the whole row rather than let the family conclude
# the file is missing -- exit 2, never a finding and never an all-clear.
_stub_qf_tree(_qf_tree(_qf_entry("README.md"), truncated=True))
read = guard.read_repo("acme", "repo", META, COPIES, QFILES)
record(not read.ok and any("truncated" in e for e in read.errors),
       "quality files: a TRUNCATED tree is unreadable, never 'the file is absent'",
       f"ok={read.ok} errors={read.errors}")

# A blob whose size the API did not report cannot be told from an empty file.
# Guessing either way is the guard deciding a fact it does not have.
_stub_qf_tree(_qf_tree(_qf_entry("CLAUDE.md", with_size=False)))
read = guard.read_repo("acme", "repo", META, COPIES, QFILES)
record(not read.ok and any("no size" in e for e in read.errors),
       "quality files: a blob with no reported size is unreadable, not 'empty'",
       f"ok={read.ok} errors={read.errors}")


def _qf_cells(cells):
    """An inventory entry holding only the quality_files section."""
    return {"quality_files": {p: v for p, v in cells.items()}}


REQ_BOTH = _qf_cells({"CLAUDE.md": ("required", ""),
                      ".cursor/BUGBOT.md": ("required", "")})

# POSITIVE CONTROL: both present, regular, non-empty -> silence.
f = []
guard.evaluate_quality_files(
    "repo", REQ_BOTH, QFILES,
    {"CLAUDE.md": {"type": "blob", "mode": "100644", "size": 5901},
     ".cursor/BUGBOT.md": {"type": "blob", "mode": "100644", "size": 4193}}, f)
record(not f, "quality files: both present and non-empty reports nothing", f"findings={f}")

# THE POINT: a required file that is not there.
f = []
guard.evaluate_quality_files(
    "repo", REQ_BOTH, QFILES,
    {"CLAUDE.md": {"type": "blob", "mode": "100644", "size": 5901}}, f)
record(len(f) == 1 and "MISSING required .cursor/BUGBOT.md" in f[0],
       "quality files: a MISSING required file is a finding, naming it",
       f"findings={f}")

# Present but empty: passes a bare existence check, carries nothing.
f = []
guard.evaluate_quality_files(
    "repo", REQ_BOTH, QFILES,
    {"CLAUDE.md": {"type": "blob", "mode": "100644", "size": 0},
     ".cursor/BUGBOT.md": {"type": "blob", "mode": "100644", "size": 4193}}, f)
record(len(f) == 1 and "EMPTY" in f[0] and "CLAUDE.md" in f[0],
       "quality files: a 0-byte file is a finding, not a pass",
       f"findings={f}")

# A symlink resolves for `cat` and is not the guidance being present in the repo.
f = []
guard.evaluate_quality_files(
    "repo", REQ_BOTH, QFILES,
    {"CLAUDE.md": {"type": "blob", "mode": "120000", "size": 12},
     ".cursor/BUGBOT.md": {"type": "blob", "mode": "100644", "size": 4193}}, f)
record(len(f) == 1 and "120000" in f[0],
       "quality files: a SYMLINK at the asserted path is a finding",
       f"findings={f}")

# A directory (or submodule) at the path is not a file either.
f = []
guard.evaluate_quality_files(
    "repo", REQ_BOTH, QFILES,
    {"CLAUDE.md": {"type": "tree", "mode": "040000", "size": None},
     ".cursor/BUGBOT.md": {"type": "blob", "mode": "100644", "size": 4193}}, f)
record(len(f) == 1 and "'tree'" in f[0] and "not a file" in f[0],
       "quality files: a directory at the asserted path is a finding",
       f"findings={f}")

# Both directions of the exemption, which is what stops the family becoming a
# list of permanent excuses: an exemption whose file appears must say so.
EXEMPT_ONE = _qf_cells({"CLAUDE.md": ("required", ""),
                        ".cursor/BUGBOT.md": ("exempt", "no guide yet, backend#1608")})
f = []
guard.evaluate_quality_files(
    "repo", EXEMPT_ONE, QFILES,
    {"CLAUDE.md": {"type": "blob", "mode": "100644", "size": 5901}}, f)
record(not f, "quality files: an exempt file that is absent reports nothing", f"findings={f}")

f = []
guard.evaluate_quality_files(
    "repo", EXEMPT_ONE, QFILES,
    {"CLAUDE.md": {"type": "blob", "mode": "100644", "size": 5901},
     ".cursor/BUGBOT.md": {"type": "blob", "mode": "100644", "size": 4193}}, f)
record(len(f) == 1 and "exemption is stale" in f[0] and "backend#1608" in f[0],
       "quality files: an exempt file that EXISTS is a stale exemption, and says why "
       "it was exempt",
       f"findings={f}")

# Schema: the same two headline rules as every other family.
expect_schema_failure("missing quality_files top-level rejected",
                      _drop("quality_files"))
expect_schema_failure("EMPTY quality_files list rejected (it would pass vacuously)",
                      lambda d: d.update({"quality_files": []}))
expect_schema_failure("repo missing the quality_files section rejected",
                      lambda d: d["repos"]["hub"].pop("quality_files"))
expect_schema_failure("MISSING quality-file key is a failure, not a default",
                      lambda d: d["repos"]["hub"]["quality_files"].clear())
expect_schema_failure("quality-file key not in the top-level list rejected",
                      lambda d: d["repos"]["hub"]["quality_files"].update(
                          {"OTHER.md": "required"}))
expect_schema_failure("quality-file exempt with no reason rejected",
                      lambda d: d["repos"]["hub"]["quality_files"].update(
                          {"GUIDE.md": {"exempt": "   "}}))
expect_schema_failure("`divergent` rejected on a quality file (presence is binary)",
                      lambda d: d["repos"]["hub"]["quality_files"].update(
                          {"GUIDE.md": {"divergent": "content differs"}}))
expect_schema_failure("duplicate quality_files entry rejected",
                      lambda d: d.update({"quality_files": ["GUIDE.md", "GUIDE.md"]}))

# Path shapes that can NEVER match a git tree path, so they would assert nothing
# while reading like an assertion.
for _bad, _label in (("/GUIDE.md", "absolute"),
                     ("../GUIDE.md", "parent-relative"),
                     ("docs/", "trailing-slash"),
                     (" GUIDE.md", "whitespace-padded")):
    expect_schema_failure(
        f"quality_files path that is {_label} rejected",
        (lambda b: lambda d: (d.update({"quality_files": [b]}),
                              d["repos"]["hub"].update(
                                  {"quality_files": {b: "required"}})))(_bad))


# ------------------------------------------------ protection reads (#1608 inc 2)
#
# The lesson these encode: on 2026-08-10 an audit that read ONLY the classic
# endpoint reported docs/staging as unprotected. It was covered by a ruleset the
# whole time. A guard that cannot tell those apart is worse than none.

def _prot(args) -> bool:
    # Scan ALL args, not args[1]: the rules read now carries --paginate/--jq
    # flags ahead of the path, so positional indexing would silently stop
    # matching and every stub would fall through to the default.
    return any(a.endswith("/protection") for a in args)


def _rules(args) -> bool:
    return any("/rules/branches/" in a for a in args)


def ndjson(*objs) -> str:
    """What `gh api --paginate --jq '.[]'` emits: one compact object per line."""
    return "".join(json.dumps(o) + "\n" for o in objs)


CLASSIC = json.dumps({
    "required_pull_request_reviews": {"required_approving_review_count": 1},
    "enforce_admins": {"enabled": True},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
    "required_conversation_resolution": {"enabled": True},
    "required_status_checks": {"strict": False, "checks": []},
})

# --- ruleset-only branch: classic 404s, rules carry the protection -------------
def _ruleset_only(args):
    if _prot(args):
        raise guard.GhError(404, "Branch not protected (HTTP 404)")
    if _rules(args):
        return ndjson(
            {"type": "pull_request", "ruleset_source": "promotion-branches",
             "parameters": {"required_approving_review_count": 2,
                            "required_review_thread_resolution": True}},
            {"type": "deletion", "ruleset_source": "promotion-branches",
             "parameters": {}},
        )
    return "{}"


stub(_ruleset_only)
got = guard.read_protection("acme", "repo", "staging")
record(got.error is None and not got.classic_present,
       "ruleset-only branch: classic 404 is a FACT, not a read failure",
       f"error={got.error} classic={got.classic_present}")
record(got.min_reviews == 2 and got.block_force_pushes and got.block_deletions
       and got.conversation_resolution,
       "ruleset-only branch: protection is read from the RULESET, not reported absent",
       f"reviews={got.min_reviews} force={got.block_force_pushes} "
       f"del={got.block_deletions} conv={got.conversation_resolution}")

# --- a pull_request rule alone does NOT block deletion -------------------------
def _pr_rule_only(args):
    if _prot(args):
        raise guard.GhError(404, "Branch not protected (HTTP 404)")
    if _rules(args):
        return ndjson({"type": "pull_request", "ruleset_source": "x",
                       "parameters": {}})
    return "{}"


stub(_pr_rule_only)
got = guard.read_protection("acme", "repo", "staging")
record(got.block_force_pushes and not got.block_deletions,
       "a pull_request rule blocks force pushes but NOT deletion",
       f"force={got.block_force_pushes} del={got.block_deletions}")

# --- fail closed: an unreadable classic read is never 'unprotected' -----------
def _prot_500(args):
    if _prot(args):
        raise guard.GhError(500, "server error (HTTP 500)")
    if _rules(args):
        return ""
    return "{}"


stub(_prot_500)
got = guard.read_protection("acme", "repo", "main")
record(got.error is not None,
       "non-404 protection error is UNREADABLE, never 'no protection'",
       f"error={got.error}")

# --- fail closed: an unreadable RULESET read is not 'classic is enough' -------
def _rules_500(args):
    if _prot(args):
        return CLASSIC
    if _rules(args):
        raise guard.GhError(500, "server error (HTTP 500)")
    return "{}"


stub(_rules_500)
got = guard.read_protection("acme", "repo", "main")
record(got.error is not None,
       "unreadable ruleset read fails closed even when classic succeeded",
       f"error={got.error}")

# --- strict: absent required_status_checks is None ('nothing to assert') ------
def _no_checks_object(args):
    if _prot(args):
        payload = json.loads(CLASSIC)
        payload.pop("required_status_checks")
        return json.dumps(payload)
    if _rules(args):
        return ""
    return "{}"


stub(_no_checks_object)
got = guard.read_protection("acme", "repo", "main")
record(got.strict is None,
       "absent required_status_checks means strict=None, not strict=False",
       f"strict={got.strict!r}")

# --- prod role resolves from the BRANCH LIST, never by probing ----------------
# GET branches/master follows rename redirects and returns 200 for a branch that
# does not exist; on 2026-08-10 that made all 16 train repos report a `master`.
record(guard.resolve_role_branch("prod", {"develop", "main"}) == "main",
       "prod resolves to main when main is in the branch list", "main")
record(guard.resolve_role_branch("prod", {"develop", "master"}) == "master",
       "prod resolves to master for a genuine master-prod repo", "master")
record(guard.resolve_role_branch("prod", {"develop", "main", "master"}) == "main",
       "main wins when a repo carries both (mid-rename)", "main")
record(guard.resolve_role_branch("prod", {"develop"}) is None,
       "no prod branch resolves to None, so the policy cell reports it", "None")
record(guard.resolve_role_branch("staging", {"develop"}) is None,
       "absent staging resolves to None rather than falling back", "None")


# --- the `exempt` staleness probe must not let a failed read decide -----------
# Bugbot, .github#196. Gating the staleness check on `error is None` first meant
# an unreadable probe silently read as "not stale" - a failed read deciding a
# negative, in the change that documents that exact defect class.

POLICY = {role: _policy() for role in guard.PROTECTION_ROLES}
POLICY["prod"]["enforce_admins"] = True


def _exempt_entry():
    return {"protection": {
        "develop": ("exempt", "documented reason", {}),
        "staging": ("exempt", "documented reason", {}),
        "prod": ("exempt", "documented reason", {}),
    }}


# both reads fail -> UNREADABLE, never "the exemption holds"
def _all_500(args):
    if _prot(args) or _rules(args):
        raise guard.GhError(500, "server error (HTTP 500)")
    return "{}"


stub(_all_500)
f, u = [], []
guard.evaluate_protection("repo", _exempt_entry(), POLICY, {"develop", "main"}, "acme", f, u)
record(len(u) >= 1 and not f,
       "exempt + unreadable probe records UNREADABLE, not a silent pass",
       f"findings={len(f)} unreadable={u[:1]}")

# classic read SUCCEEDS, ruleset read fails -> we already know enough to call the
# exemption stale, and suppressing that on the ruleset error is the worse half.
def _classic_ok_rules_500(args):
    if _prot(args):
        return CLASSIC
    if _rules(args):
        raise guard.GhError(500, "server error (HTTP 500)")
    return "{}"


stub(_classic_ok_rules_500)
f, u = [], []
guard.evaluate_protection("repo", _exempt_entry(), POLICY, {"develop", "main"}, "acme", f, u)
record(any("exemption is stale" in x for x in f),
       "exempt + classic-present-but-ruleset-unreadable STILL reports the stale exemption",
       f"findings={f[:1]}")

# a genuinely unprotected branch leaves the exemption intact and says nothing
def _unprotected(args):
    if _prot(args):
        raise guard.GhError(404, "Branch not protected (HTTP 404)")
    if _rules(args):
        return ""
    return "{}"


stub(_unprotected)
f, u = [], []
guard.evaluate_protection("repo", _exempt_entry(), POLICY, {"develop", "main"}, "acme", f, u)
record(not f and not u,
       "a genuinely unprotected branch leaves its exemption intact",
       f"findings={f} unreadable={u}")

# THE FAIL-OPEN backend#1681 CLOSED. Classic 404s, a RULESET carries the branch.
# The exempt probe used to read `probe.classic_present` alone, so this state --
# a real, protected branch -- read as unprotected and the exemption stayed
# silently valid. `_ruleset_only` already existed for the `required` path; it was
# never pointed at the `exempt` path, which is why the hole survived.
stub(_ruleset_only)
f, u = [], []
guard.evaluate_protection("repo", _exempt_entry(), POLICY, {"develop", "main"}, "acme", f, u)
record(any("exemption is stale" in x for x in f) and any("ruleset" in x for x in f),
       "exempt + RULESET-ONLY protection reports the stale exemption (backend#1681)",
       f"findings={f[:1]}")


# --- the ruleset read must be PAGINATED --------------------------------------
# `rules/branches/{b}` defaults to 30 per page. A rule dropped off page 2 is a
# partial view of a branch's protection that this guard would then report as a
# verdict - the exact failure mode read_protection()'s header describes.
# Asserted on the CALL, because pagination itself is gh's job and is stubbed out
# here. (Bugbot, .github#196.)
SEEN_ARGS = []


def _capture(args):
    SEEN_ARGS.append(list(args))
    if _prot(args):
        raise guard.GhError(404, "Branch not protected (HTTP 404)")
    if _rules(args):
        return ""
    return "{}"


stub(_capture)
guard.read_protection("acme", "repo", "main")
rules_calls = [a for a in SEEN_ARGS if _rules(a)]
record(len(rules_calls) == 1 and "--paginate" in rules_calls[0],
       "the ruleset read passes --paginate, so page 2 is never silently dropped",
       f"call={rules_calls[0] if rules_calls else None}")

# And the NDJSON reassembly must actually reassemble multiple elements.
def _two_pages(args):
    if _prot(args):
        raise guard.GhError(404, "Branch not protected (HTTP 404)")
    if _rules(args):
        # what gh emits for a 2-page result with --jq '.[]': one object per line
        return ndjson({"type": "pull_request", "ruleset_source": "p1",
                       "parameters": {"required_approving_review_count": 1}},
                      {"type": "deletion", "ruleset_source": "p2",
                       "parameters": {}})
    return "{}"


stub(_two_pages)
got = guard.read_protection("acme", "repo", "main")
record(got.block_deletions and got.min_reviews == 1 and got.error is None,
       "elements streamed across pages are all reassembled, not just the first",
       f"del={got.block_deletions} reviews={got.min_reviews} rulesets={got.rulesets}")


# --- protection unreadability must not be able to abort the whole audit -------
# `evaluated <= 0` calls die(), which discards the report. A fleet-wide
# protection outage would otherwise make every repo look unreadable and throw
# away real, already-collected caller findings before anything was written.
# Fail-closed must mean RED, not "results destroyed". (Bugbot, .github#196.)
#
# Asserted structurally: evaluate_protection writes ONLY into the list it is
# handed, so main() can keep it out of the `evaluated` computation.
stub(_all_500)
caller_unreadable, prot_unreadable = [], []
guard.evaluate_protection(
    "repo", {"protection": {r: ("required", "", {}) for r in guard.PROTECTION_ROLES}},
    POLICY, {"develop", "staging", "main"}, "acme", [], prot_unreadable,
)
record(len(prot_unreadable) == 3 and caller_unreadable == [],
       "protection failures land in their OWN list, never the one driving die()",
       f"protection={len(prot_unreadable)} caller={len(caller_unreadable)}")

# And they must still be loud - isolating them must not make them silent.
record(all("unreadable" in x for x in prot_unreadable),
       "isolated protection failures are still recorded as UNREADABLE",
       f"{prot_unreadable[:1]}")


# --- required_checks (backend#1681) -------------------------------------------
#
# The property exists because a caller being PRESENT only means the check runs.
# These assert the thing that was previously unassertable: that it BLOCKS.

def _classic_with(contexts, legacy=False):
    """Classic protection whose required-status-checks carry `contexts`."""
    rsc = {"strict": False}
    if legacy:
        rsc["contexts"] = list(contexts)          # older flat spelling
    else:
        rsc["checks"] = [{"context": c, "app_id": None} for c in contexts]
    return json.dumps({
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "enforce_admins": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "required_conversation_resolution": {"enabled": True},
        "required_status_checks": rsc,
    })


def _required_entry():
    return {"protection": {r: ("required", "", {}) for r in guard.PROTECTION_ROLES}}


ALL_BRANCHES = {"develop", "staging", "main"}


def _stub_classic(contexts, legacy=False, rule_contexts=None):
    body = _classic_with(contexts, legacy)

    def handler(args):
        if _prot(args):
            return body
        if _rules(args):
            if rule_contexts is None:
                return ""
            return ndjson({
                "type": "required_status_checks",
                "ruleset_source": "some-ruleset",
                "parameters": {"required_status_checks":
                               [{"context": c} for c in rule_contexts]},
            })
        return "{}"

    stub(handler)


# POSITIVE CONTROL: the baseline is met -> silence.
_stub_classic(["ci / build"])
f, u = [], []
guard.evaluate_protection("repo", _required_entry(), POLICY, ALL_BRANCHES, "acme", f, u)
record(not f and not u, "required_checks: baseline met reports nothing", f"findings={f}")

# THE POINT: the context is absent -> a finding on every role, naming it.
_stub_classic([])
f, u = [], []
guard.evaluate_protection("repo", _required_entry(), POLICY, ALL_BRANCHES, "acme", f, u)
record(len(f) == 3 and all("does not REQUIRE ci / build" in x for x in f),
       "required_checks: a check that runs but cannot block IS a finding",
       f"findings={len(f)} :: {f[:1]}")

# SUBSET, not equality: repo-specific suites on top of the floor are not drift.
_stub_classic(["ci / build", "Django tests", "golangci-lint"])
f, u = [], []
guard.evaluate_protection("repo", _required_entry(), POLICY, ALL_BRANCHES, "acme", f, u)
record(not f, "required_checks: extra contexts beyond the baseline are not drift",
       f"findings={f}")

# The legacy `contexts` spelling is read too -- reading only `checks` would
# report an EMPTY required set for those repos, i.e. fail open.
_stub_classic(["ci / build"], legacy=True)
f, u = [], []
guard.evaluate_protection("repo", _required_entry(), POLICY, ALL_BRANCHES, "acme", f, u)
record(not f, "required_checks: the legacy `contexts` spelling still counts", f"findings={f}")

# Union across BOTH systems: classic carries none, a ruleset carries it.
_stub_classic([], rule_contexts=["ci / build"])
f, u = [], []
guard.evaluate_protection("repo", _required_entry(), POLICY, ALL_BRANCHES, "acme", f, u)
record(not f, "required_checks: a ruleset-supplied context satisfies the policy",
       f"findings={f}")

# A documented divergence may narrow the set -- and is then held to the narrower
# set, not excused entirely.
_stub_classic(["gate / gate"])
narrowed = {"protection": {r: ("divergent", "public repo, tracked in backend#1681",
                              {"required_checks": ["gate / gate"]})
                           for r in guard.PROTECTION_ROLES}}
f, u = [], []
guard.evaluate_protection("repo", narrowed, POLICY, ALL_BRANCHES, "acme", f, u)
record(not f, "required_checks: a divergent cell is judged against ITS list", f"findings={f}")

_stub_classic([])
f, u = [], []
guard.evaluate_protection("repo", narrowed, POLICY, ALL_BRANCHES, "acme", f, u)
record(len(f) == 3 and all("gate / gate" in x for x in f),
       "required_checks: a divergent cell still fails when its own list is unmet",
       f"findings={len(f)} :: {f[:1]}")

# Schema: the shapes that would silently assert less than they appear to.
expect_schema_failure("required_checks as a bare string rejected",
                      lambda d: d["protection_policy"]["develop"].update({"required_checks": "ci / build"}))
expect_schema_failure("required_checks with a non-string entry rejected",
                      lambda d: d["protection_policy"]["develop"].update({"required_checks": [1]}))
expect_schema_failure("required_checks with a blank context rejected",
                      lambda d: d["protection_policy"]["develop"].update({"required_checks": ["  "]}))
expect_schema_failure("required_checks with a duplicate context rejected",
                      lambda d: d["protection_policy"]["develop"].update(
                          {"required_checks": ["ci / build", "ci / build"]}))


# --- rulesets (backend#1681) ---------------------------------------------------
#
# The layer nothing audited. These assert that a MISSING ruleset, a WEAKENED one,
# and an UNEXPECTED bypass actor are each findings -- the three shapes that were
# live on the fleet when this was written.

RPOLICY = {
    "promotion_merge_commit_only": {
        "target": "branch", "require_rule_types": ["pull_request"],
        "allowed_merge_methods": ["merge"],
        "must_cover_roles": ["staging", "prod"], "bypass_actors": [],
    },
    "tag_trust_root": {
        "target": "tag",
        "require_rule_types": ["creation", "update", "deletion"],
        "include_refs": ["refs/tags/v*"],
        "bypass_actors": ["OrganizationAdmin", "Team:18304481"],
    },
}


def _rs_entry(promotion="required", tag=("exempt", "no v* tags")):
    return {"rulesets": {
        "promotion_merge_commit_only": (promotion, "") if isinstance(promotion, str)
        else promotion,
        "tag_trust_root": tag if isinstance(tag, tuple) else (tag, ""),
    }}


PROMO_OK = {"id": 1, "name": "promotion-branches-merge-commit-only", "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/main", "refs/heads/staging"]}},
            "rules": [{"type": "pull_request",
                       "parameters": {"allowed_merge_methods": ["merge"]}}],
            "bypass_actors": []}


def _stub_rulesets(*full):
    """Stub the two-call read: a listing, then each ruleset by id."""
    def handler(args):
        # Match the listing call, query string and all: read_rulesets now
        # requests `/rulesets?includes_parents=false`, so strip the query before
        # the suffix check (must still not match `/rulesets/{id}`).
        if any(a.split("?", 1)[0].endswith("/rulesets") for a in args):
            return ndjson(*[{"id": r["id"]} for r in full])
        for r in full:
            if any(a.endswith(f"/rulesets/{r['id']}") for a in args):
                return json.dumps(r)
        if _prot(args):
            raise guard.GhError(404, "Branch not protected (HTTP 404)")
        if _rules(args):
            return ""
        return "{}"
    stub(handler)


BR = {"develop", "staging", "main"}

# POSITIVE CONTROL
_stub_rulesets(PROMO_OK)
f, u = [], []
guard.evaluate_rulesets("repo", _rs_entry(), RPOLICY, BR, "acme", f, u)
record(not f and not u, "rulesets: a conforming promotion ruleset reports nothing", f"findings={f}")

# MISSING ENTIRELY -- start-training's live state before backend#1681.
_stub_rulesets()
f, u = [], []
guard.evaluate_rulesets("repo", _rs_entry(), RPOLICY, BR, "acme", f, u)
record(len(f) == 1 and "has NO promotion_merge_commit_only ruleset" in f[0],
       "rulesets: a repo with NO promotion ruleset IS a finding", f"findings={f}")

# WEAKENED: squash allowed alongside merge.
weak = json.loads(json.dumps(PROMO_OK))
weak["rules"][0]["parameters"]["allowed_merge_methods"] = ["merge", "squash"]
_stub_rulesets(weak)
f, u = [], []
guard.evaluate_rulesets("repo", _rs_entry(), RPOLICY, BR, "acme", f, u)
record(any("allows merge methods" in x for x in f),
       "rulesets: permitting squash on a promotion branch IS a finding", f"findings={f[:1]}")

# EVALUATE-ONLY: looks protective, enforces nothing.
inert = json.loads(json.dumps(PROMO_OK))
inert["enforcement"] = "evaluate"
_stub_rulesets(inert)
f, u = [], []
guard.evaluate_rulesets("repo", _rs_entry(), RPOLICY, BR, "acme", f, u)
record(any("not `active`" in x for x in f),
       "rulesets: enforcement=evaluate IS a finding", f"findings={f[:1]}")

# UNEXPECTED BYPASS ACTOR -- the release-python shape.
tag_ok = {"id": 2, "name": "R8 trust root - protect v* release tags", "target": "tag",
          "enforcement": "active",
          "conditions": {"ref_name": {"include": ["refs/tags/v*"]}},
          "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}],
          "bypass_actors": [{"actor_type": "OrganizationAdmin", "actor_id": None},
                            {"actor_type": "Team", "actor_id": 18304481}]}
_stub_rulesets(PROMO_OK, tag_ok)
f, u = [], []
guard.evaluate_rulesets("repo", _rs_entry(tag="required"), RPOLICY, BR, "acme", f, u)
record(not f, "rulesets: a differently-NAMED tag ruleset still matches (target+rules)",
       f"findings={f}")

extra = json.loads(json.dumps(tag_ok))
extra["bypass_actors"].append({"actor_type": "Team", "actor_id": 18689454})
_stub_rulesets(PROMO_OK, extra)
f, u = [], []
guard.evaluate_rulesets("repo", _rs_entry(tag="required"), RPOLICY, BR, "acme", f, u)
record(any("unexpected ['Team:18689454']" in x for x in f),
       "rulesets: an EXTRA bypass actor IS a finding (the release-python shape)",
       f"findings={f[:1]}")

# A stale exemption must say so.
_stub_rulesets(PROMO_OK)
f, u = [], []
guard.evaluate_rulesets("repo", _rs_entry(promotion=("exempt", "documented")), RPOLICY, BR, "acme", f, u)
record(any("is `exempt` but a matching ruleset exists" in x for x in f),
       "rulesets: an exemption contradicted by reality IS a finding", f"findings={f[:1]}")

# FAIL-CLOSED: an unreadable ruleset must never read as "absent".
def _rulesets_500(args):
    if any(a.split("?", 1)[0].endswith("/rulesets") for a in args):
        raise guard.GhError(500, "server error (HTTP 500)")
    return "{}"


stub(_rulesets_500)
f, u = [], []
guard.evaluate_rulesets("repo", _rs_entry(), RPOLICY, BR, "acme", f, u)
record(not f and len(u) == 1,
       "rulesets: an unreadable read is UNREADABLE, never a silent pass", f"unreadable={u}")

# COVERAGE ARITHMETIC (Bugbot, #278). `evaluated` drives a die() that discards the
# whole report, and it assumed every name in `unreadable` was an AUDITED one. The
# stale-inventory probe records repos from `inventory - active`, which are by
# definition not audited -- so routing them into the shared bucket subtracts names
# that were never in the total. Enough of them and a run with plenty of verdicts
# aborts as though nothing could be read, precisely in the partial-installation
# case the probe exists to describe.
# WHICH BUCKET GETS THE BLAME (Bugbot, #278). The caller/copy count is whatever the
# merged list has left after every typed bucket is removed, and the subtraction was
# written out twice -- so a fourth bucket landed in only one of them and listing gaps
# were announced as caller-read failures. Asserted through the real function so a
# missed bucket reddens here instead of agreeing with a copy of the sum.
# THE WIRING, not just the function (backend#1729 rule 5: a test that still passes
# under the mutation is vacuous). Breaking caller_read_failures() reddens the cases
# below, but DROPPING A BUCKET AT ITS CALL SITE did not -- and that is precisely the
# defect being fixed: the bucket existed and was not passed. main() is not reachable
# from this selftest (no org-listing stub), so assert it from the SOURCE instead:
# every `*_unreadable` bucket main() declares must appear in the call. Derived from
# the declarations, so a fifth bucket is covered the moment it is declared.
_src = pathlib.Path(guard.__file__).read_text()

# EVERY *_unreadable OUTPUT MUST BE SURFACED BY THE WORKFLOW (Bugbot, #278, third
# time). The script emits a decomposed count per bucket so the watchdog headline can
# name WHICH read failed; a bucket the workflow never reads falls back to the merged
# count, or -- worse, when mixed with another cause -- is dropped from the sentence
# entirely. That has now happened three times in one PR: decide_exit, the step
# outputs, and the watchdog phrase were each updated separately and each was missed
# once. Derive the expectation from what the script EMITS rather than from a list
# someone maintains, so a fifth bucket is covered the moment it is written out.
_wf = pathlib.Path(__file__).resolve().parent.parent.parent / ".github/workflows/caller-drift.yml"
# Keyed on the EMITTED STRING LITERAL, not on the shape of the call around it.
# The first version matched `handle.write(f"x_unreadable=` on one line, and
# `caller_unreadable` is written across two -- so the guard built to stop a
# bucket being dropped silently omitted the very bucket that had been dropped
# three times. Dropping CALLER_UNREADABLE from the workflow left all three
# wiring assertions green (Bugbot, #278). A guard blind to one of the four
# things it guards is worse than no guard: it reports coverage it lacks.
_emitted = set(re.findall(r'"(\w+_unreadable)=', _src))
_surfaced = {m.lower() for m in re.findall(r'^\s*(\w+_UNREADABLE):', _wf.read_text(), re.M)}
record(bool(_emitted) and _emitted <= _surfaced,
       "wiring: every *_unreadable output the script emits is read by the workflow",
       f"emitted={sorted(_emitted)} surfaced={sorted(_surfaced)}")
_phrase = re.search(r'what=""(.*?)\[ -z "\$what" \]', _wf.read_text(), re.S)
_named = {m.lower() for m in re.findall(r'\$\{(\w+_UNREADABLE):', _phrase.group(1) if _phrase else "")}
record(bool(_phrase) and _emitted <= _named,
       "wiring: every *_unreadable bucket gets its own clause in the watchdog phrase",
       f"emitted={sorted(_emitted)} named={sorted(_named)}")

_declared = set(re.findall(r'^\s*(\w+_unreadable): "list\[str\]" = \[\]', _src, re.M))
_call = re.search(r'=\s*caller_read_failures\((.*?)\n\s*\)', _src, re.S)
_passed = set(re.findall(r'len\((\w+_unreadable)\)', _call.group(1) if _call else ""))
record(bool(_declared) and bool(_call) and _declared == _passed,
       "wiring: every declared *_unreadable bucket is passed to caller_read_failures",
       f"declared={sorted(_declared)} passed={sorted(_passed)}")

record(guard.caller_read_failures(5, 0, 0, 4) == 1,
       "caller_read_failures: listing gaps are subtracted, not blamed on callers",
       f"got {guard.caller_read_failures(5, 0, 0, 4)}, want 1")
record(guard.caller_read_failures(10, 2, 3, 4) == 1,
       "caller_read_failures: all typed buckets are removed",
       f"got {guard.caller_read_failures(10, 2, 3, 4)}, want 1")
record(guard.caller_read_failures(3, 0, 0, 0) == 3,
       "caller_read_failures: with no typed buckets every record is a caller failure",
       f"got {guard.caller_read_failures(3, 0, 0, 0)}, want 3")
record(guard.caller_read_failures(2, 2, 2, 0) == 0,
       "caller_read_failures: a double-counted bucket clamps at 0, never negative",
       f"got {guard.caller_read_failures(2, 2, 2, 0)}, want 0")

record(guard.coverage(["a", "b", "c"], []) == 3,
       "coverage: nothing unreadable means everything was evaluated",
       f"got {guard.coverage(['a','b','c'], [])}")
record(guard.coverage(["a", "b", "c"], ["b: protection unreadable"]) == 2,
       "coverage: an AUDITED repo's read failure reduces coverage",
       f"got {guard.coverage(['a','b','c'], ['b: x'])}")
record(guard.coverage(["a", "b", "c"],
                      ["x: absent from listing", "y: absent from listing",
                       "z: absent from listing", "w: absent from listing"]) == 3,
       "coverage: listing gaps are NOT audited repos and must not reduce coverage",
       "four non-audited names must not zero out three real verdicts")
record(guard.coverage(["a"], ["a: unreadable", "x: absent", "y: absent"]) == 0,
       "coverage: a genuinely unreadable audited repo still reaches zero",
       f"got {guard.coverage(['a'], ['a: u', 'x: a', 'y: a'])}")

# ABSENT bypass_actors IS NOT AN EMPTY ALLOWLIST (Bugbot, #278).
#
# GitHub returns `bypass_actors` only to a caller with WRITE access to the
# ruleset; everyone else gets a 200 without the field. This is THE fail-open case
# and it is invisible without these two cases: `promotion_merge_commit_only`
# asserts `bypass_actors: []`, so folding a missing field into `[]` satisfies the
# assertion WITHOUT the field ever having been read -- on all 32 promotion
# branches. It was unreachable under the org-admin PAT, which always saw the
# field, so no existing case could have caught it (backend#2036).
promo_no_bypass = json.loads(json.dumps(PROMO_OK))
del promo_no_bypass["bypass_actors"]
_stub_rulesets(promo_no_bypass)
f, u = [], []
guard.evaluate_rulesets("repo", _rs_entry(), RPOLICY, BR, "acme", f, u)
record(not f and len(u) == 1 and "bypass_actors" in u[0],
       "rulesets: a 200 that OMITS bypass_actors is UNREADABLE, not an empty allowlist",
       f"findings={f} unreadable={u}")

# THE MUTATION ANCHOR for the case above. If the omission is ever folded back into
# `raw.get("bypass_actors") or []`, the case above still passes trivially unless
# something asserts the DISTINCTION itself: present-and-empty must stay a real,
# assertable value, or the fix would just make every empty allowlist unreadable
# and break the 32 branches it is meant to protect.
record(guard.RepoRuleset(PROMO_OK).bypass_present is True
       and guard.RepoRuleset(promo_no_bypass).bypass_present is False
       and guard.RepoRuleset(PROMO_OK).bypass == [],
       "rulesets: present-and-empty and absent are DISTINGUISHED, not both []",
       f"present={guard.RepoRuleset(PROMO_OK).bypass_present} "
       f"absent={guard.RepoRuleset(promo_no_bypass).bypass_present}")

# The other direction of the same root cause: a policy expecting a NON-empty
# allowlist went falsely RED, naming actors as missing that are simply invisible.
# Six of these on .github#278 before the fix. Must now read as unreadable too.
tag_no_bypass = {"id": 9, "name": "Protect v* release tags", "target": "tag",
                 "enforcement": "active",
                 "conditions": {"ref_name": {"include": ["refs/tags/v*"]}},
                 "rules": [{"type": "creation"}, {"type": "update"},
                           {"type": "deletion"}]}
_stub_rulesets(PROMO_OK, tag_no_bypass)
f, u = [], []
guard.evaluate_rulesets("repo", _rs_entry(tag="required"), RPOLICY, BR, "acme", f, u)
record(not any("bypass actors: missing" in x for x in f) and len(u) == 1,
       "rulesets: an invisible allowlist is not reported as MISSING actors",
       f"findings={f} unreadable={u}")

# Schema
# The family must not be switchable off by deletion (Bugbot, .github#262). Both
# of these leave the top-level key PRESENT, so schema validation passes -- and
# the audit would then measure every caller against no inputs at all while
# reporting the fleet conformant.
expect_schema_failure("caller_inputs: null rejected",
                      lambda d: d.__setitem__("caller_inputs", None))
expect_schema_failure("caller_inputs: {} rejected",
                      lambda d: d.__setitem__("caller_inputs", {}))

expect_schema_failure("ruleset_policy missing a kind rejected",
                      lambda d: d["ruleset_policy"].pop("tag_trust_root"))
expect_schema_failure("ruleset_policy with an unknown kind rejected",
                      lambda d: d["ruleset_policy"].update({"nope": {}}))
expect_schema_failure("ruleset kind missing bypass_actors rejected",
                      lambda d: d["ruleset_policy"]["tag_trust_root"].pop("bypass_actors"))
expect_schema_failure("ruleset kind with a bad target rejected",
                      lambda d: d["ruleset_policy"]["tag_trust_root"].update({"target": "repo"}))
expect_schema_failure("MISSING rulesets cell is a failure, not a default",
                      lambda d: d["repos"]["hub"]["rulesets"].clear())
expect_schema_failure("rulesets exemption with no reason rejected",
                      lambda d: d["repos"]["hub"]["rulesets"].update(
                          {"tag_trust_root": {"exempt": "  "}}))


# --- source-reusable enumeration (backend#1681) -------------------------------
#
# The guard iterated the inventory's `reusables` list and never the source
# directory, so a reusable that shipped without being listed was compared against
# no repo and reported by nothing. version-bump-pr.yml lived in that blind spot;
# it was deleted under backend#1563 rather than wired up, but the blind spot is
# what these cases are about, so they do not depend on that file existing.

def _src_tree(names_and_bodies):
    """A throwaway source dir with .github/workflows/<name> files."""
    root = tempfile.mkdtemp()
    wf = os.path.join(root, ".github", "workflows")
    os.makedirs(wf)
    for name, body in names_and_bodies.items():
        with open(os.path.join(wf, name), "w", encoding="utf-8") as fh:
            fh.write(body)
    return root


REUSABLE = "on:\n  workflow_call:\n    inputs: {}\njobs: {}\n"
NOT_REUSABLE = "on:\n  push:\n    branches: [main]\njobs: {}\n"


def _expect_exit(name, fn, detail_ok="SystemExit(2)"):
    try:
        fn()
    except SystemExit as exc:
        record(exc.code == 2, name, f"SystemExit({exc.code})")
    else:
        record(False, name, "ACCEPTED what should have been refused")


root = _src_tree({"a.yml": REUSABLE, "b.yml": NOT_REUSABLE})
try:
    guard.check_source_reusables(root, ["a.yml"])
    record(True, "source reusables: a fully-tracked source dir passes", "no exit")
except SystemExit as exc:
    record(False, "source reusables: a fully-tracked source dir passes", f"SystemExit({exc.code})")

# THE POINT: shipped but unlisted.
root = _src_tree({"a.yml": REUSABLE, "sneaky.yml": REUSABLE})
_expect_exit("source reusables: an UNLISTED reusable is refused",
             lambda: guard.check_source_reusables(root, ["a.yml"]))

# The inverse: listed but gone (a rename/delete leaves every repo asserting a ghost).
root = _src_tree({"a.yml": REUSABLE})
_expect_exit("source reusables: a listed-but-absent reusable is refused",
             lambda: guard.check_source_reusables(root, ["a.yml", "ghost.yml"]))

# A non-reusable workflow must NOT be demanded in the list.
root = _src_tree({"a.yml": REUSABLE, "b.yml": NOT_REUSABLE})
try:
    guard.check_source_reusables(root, ["a.yml"])
    record(True, "source reusables: a push-triggered workflow is not a reusable", "no exit")
except SystemExit as exc:
    record(False, "source reusables: a push-triggered workflow is not a reusable",
           f"SystemExit({exc.code})")

# Missing directory is a malfunction, never "nothing to check".
_expect_exit("source reusables: a missing workflows dir is refused, not passed",
             lambda: guard.check_source_reusables(tempfile.mkdtemp(), ["a.yml"]))


# --- the conformance matrix (backend#1608 increment 3) ------------------------
#
# A screen that can only ever render OK is the same vacuous pass this guard
# exists to refuse, so each cell state is asserted, including the two that cannot
# be produced against a healthy fleet.

INV_M = {"repos": {
    "alpha": {"release_train": True},
    "beta": {"release_train": False},
}}

CLEAN_CELLS = {"callers": 0, "copies": 0, "quality_files": 0, "protection": 0,
               "rulesets": 0}

rows = guard.render_matrix({"alpha": dict(CLEAN_CELLS)}, INV_M)
record(any("| `alpha` | yes | OK | OK | OK | OK | OK |" in r for r in rows),
       "matrix: a clean repo renders OK in every family", rows[-3])

rows = guard.render_matrix({"alpha": dict(CLEAN_CELLS, callers=2)}, INV_M)
record(any("**2**" in r for r in rows)
       and not any("| OK | OK | OK | OK | OK |" in r for r in rows),
       "matrix: findings render as a COUNT, not as OK", rows[-3])

rows = guard.render_matrix({"alpha": {"unread": True}}, INV_M)
record(any("| ? | ? | ? | ? | ? |" in r for r in rows),
       "matrix: an unreadable repo renders ? in every family, never OK", rows[-3])

rows = guard.render_matrix({"alpha": dict(CLEAN_CELLS, protection_unread=1)}, INV_M)
record(any("| OK | OK | OK | ? | OK |" in r for r in rows),
       "matrix: an unreadable FAMILY renders ? in that column only", rows[-3])

# The new family gets its own column, and a finding in it must not smear into a
# neighbour's cell -- the table is the artefact people read instead of the log.
rows = guard.render_matrix({"alpha": dict(CLEAN_CELLS, quality_files=3)}, INV_M)
record(any("| OK | OK | **3** | OK | OK |" in r for r in rows)
       and any("quality_files" in r for r in rows),
       "matrix: quality_files is its own column, counted in its own cell", rows[-3])

# The distinction that matters most: zero findings because it was checked, versus
# zero findings because it was never read. Those must not render the same.
clean = guard.render_matrix({"alpha": dict(CLEAN_CELLS)}, INV_M)
unread = guard.render_matrix({"alpha": {"unread": True}}, INV_M)
record(clean != unread,
       "matrix: 'checked and clean' and 'never read' do NOT render identically",
       "clean != unread")

rows = guard.render_matrix({"beta": dict(CLEAN_CELLS)}, INV_M)
record(any("| `beta` | - |" in r for r in rows),
       "matrix: a non-train repo is marked as such", rows[-3])

# ---------------------------------------------------------------- remediation
# --create-prs WRITES to every repo in the org, so its restraint matters more than
# its reach. The safety property is not "does it fix drift" but "does it ever touch
# something a human deliberately decided about".


# THE ONE THAT MATTERS. `divergent` records a written reason why a repo differs --
# cli pins actions/stale@v11 where canon pins v9, and the newer pin may well be the
# better one. `exempt` records that the file should not be there at all. A harness
# that overwrote either would destroy the judgement the inventory exists to hold.
#
# Asserted structurally rather than by grep: parse main(), find the `state ==
# "required"` branch, and require every `remediable` mutation to live inside it.
# A grep for "divergent" near "remediable" would pass whatever the code did.
_src = textwrap.dedent(inspect.getsource(guard.main))
_tree = ast.parse(_src)


def _remediable_lines(node) -> "set[int]":
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "remediable":
            out.add(sub.lineno)
    return out


_all_rem = _remediable_lines(_tree)
_required_rem = set()
for _n in ast.walk(_tree):
    # the `if state == "required":` test inside the copies loop
    if isinstance(_n, ast.If) and isinstance(_n.test, ast.Compare):
        left, comps = _n.test.left, _n.test.comparators
        if (isinstance(left, ast.Name) and left.id == "state"
                and comps and isinstance(comps[0], ast.Constant)
                and comps[0].value == "required"):
            for _stmt in _n.body:
                _required_rem |= _remediable_lines(_stmt)

# the declaration line is not a mutation site
_mutations = {ln for ln in _all_rem if ln not in _required_rem}
_decl = min(_all_rem) if _all_rem else 0
_mutations.discard(_decl)
# lines inside the `if args.create_prs:` reporting block read it, not write it
_reads = set()
for _n in ast.walk(_tree):
    if isinstance(_n, ast.If) and isinstance(_n.test, ast.Attribute) \
            and _n.test.attr == "create_prs":
        _reads |= _remediable_lines(_n)
_mutations -= _reads
record(not _mutations,
       "remediation: only the `required` branch can enqueue a copy - "
       "`divergent`/`exempt` are never rewritten",
       f"unexpected remediable use at lines {sorted(_mutations)}")

record(bool(_required_rem),
       "remediation: the `required` branch DOES enqueue (the check above is not vacuous)",
       f"required-branch lines {sorted(_required_rem)}")

# --- the write path -----------------------------------------------------------
CALLS = []


def _rem_stub(missing_on_branch=True, fail=None, has_pr=False, ref_status=None,
              branch_body=b"drifted bytes\n"):
    """`branch_body` is what the remediation branch already carries.

    The contents read returns the REAL API shape (sha + base64 content), because
    remediate_copies compares that content against the canon to skip an identical
    re-write. Returning a bare sha here modelled the old --jq call and made the
    skip untestable.
    """
    def handler(args):
        CALLS.append(list(args))
        joined = " ".join(args)
        if "git/ref/heads/" in joined and "-X" not in joined:
            return "basesha123\n"
        if "git/refs" in joined and "-X" in args:
            if ref_status is not None:
                raise guard.GhError(ref_status, f"HTTP {ref_status}")
            return "{}"
        if "contents/" in joined and "-X" not in args:
            if missing_on_branch:
                raise guard.GhError(404, "Not Found (HTTP 404)")
            return json.dumps({
                "sha": "existingsha",
                "content": base64.b64encode(branch_body).decode("ascii"),
            })
        if "-X" in args and "PUT" in args:
            if fail:
                raise guard.GhError(fail, f"HTTP {fail}")
            return "{}"
        if args[0] == "pr" and args[1] == "list":
            return "7\n" if has_pr else "\n"
        if args[0] == "pr" and args[1] == "create":
            return "https://github.com/acme/repo/pull/9\n"
        return "{}"
    return handler


os.makedirs("/tmp/rem-src/.github/workflows", exist_ok=True)
with open("/tmp/rem-src/.github/workflows/copy-a.yml", "wb") as _h:
    _h.write(b"canonical bytes\n")

CALLS.clear()
stub(_rem_stub())
_err = guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", False)], "/tmp/rem-src", 1608)
_puts = [c for c in CALLS if "PUT" in c]
record(_err is None and len(_puts) == 1
       and any("contents/.github/workflows/copy-a.yml" in a for a in _puts[0])
       and any(a.startswith("branch=chore/1608") for a in _puts[0]),
       "remediation: writes the named copy to the remediation branch",
       f"err={_err} puts={len(_puts)}")

# A file already present on the branch must be sent WITH its sha, or the API
# rejects the update; absent means create. Getting this backwards fails every
# second dispatch, which is the shape that looks intermittent.
CALLS.clear()
stub(_rem_stub(missing_on_branch=False))
guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", True)], "/tmp/rem-src", 1608)
_puts = [c for c in CALLS if "PUT" in c]
record(any(a.startswith("sha=existingsha") for a in _puts[0]),
       "remediation: an existing file on the branch is updated with its sha",
       str(_puts[0]))

# A RE-DISPATCH MUST BE IDEMPOTENT. The second `--create-prs` run finds the branch
# already carrying the canon from the first. PUTting identical bytes answers 409
# (or races the sha) and remediation records a failure, so the audit goes red as if
# the fleet could not be written -- while the open PR already has the canon.
# standards-sync.py skips the write for the same reason. (Bugbot, PR #238.)
CALLS.clear()
stub(_rem_stub(missing_on_branch=False, branch_body=b"canonical bytes\n"))
_err = guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", True)],
                              "/tmp/rem-src", 1608)
_puts = [c for c in CALLS if "PUT" in c]
record(_err is None and not _puts,
       "remediation: a copy already matching the canon is not re-written",
       f"err={_err} puts={len(_puts)}")

# ...and the PR is still ensured, or the first run's work would never be reviewable.
record(any(c[0] == "pr" and c[1] in ("list", "create") for c in CALLS),
       "remediation: the PR is still ensured when every write was skipped",
       str([c[:2] for c in CALLS]))

CALLS.clear()
stub(_rem_stub(missing_on_branch=True))
guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", False)], "/tmp/rem-src", 1608)
_puts = [c for c in CALLS if "PUT" in c]
record(not any(a.startswith("sha=") for a in _puts[0]),
       "remediation: a missing file is CREATED, with no sha argument",
       str(_puts[0]))

# 422 on the ref create means the branch exists: reuse it. That is what makes a
# re-dispatch idempotent rather than a second PR.
CALLS.clear()
stub(_rem_stub(ref_status=422))
_err = guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", False)], "/tmp/rem-src", 1608)
record(_err is None and any("PUT" in c for c in CALLS),
       "remediation: an existing branch (422) is reused, not treated as fatal",
       f"err={_err}")

# Any OTHER branch-create failure is fatal: a branch we could not create is not a
# branch we may write to.
CALLS.clear()
stub(_rem_stub(ref_status=403))
_err = guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", False)], "/tmp/rem-src", 1608)
record(_err is not None and not any("PUT" in c for c in CALLS),
       "remediation: a non-422 branch failure aborts before writing anything",
       f"err={_err}")

# A failed write must return an error. Returning None would report the drift as
# fixed while it is still there -- the fail-open this whole guard refuses.
CALLS.clear()
stub(_rem_stub(fail=409))
_err = guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", False)], "/tmp/rem-src", 1608)
record(_err is not None and "cannot write" in _err,
       "remediation: a rejected write returns an error, never a silent success",
       f"err={_err}")

# An open PR already tracking the branch must not produce a second one.
CALLS.clear()
stub(_rem_stub(has_pr=True))
_err = guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", False)], "/tmp/rem-src", 1608)
record(_err is None and not any(c[:2] == ["pr", "create"] for c in CALLS),
       "remediation: an existing open PR is refreshed, not duplicated",
       f"err={_err}")


# --- the eventual-consistency race (Bugbot, #227) -----------------------------
# A ref created an instant ago can 404 for a file the base demonstrably has. For a
# DRIFTED copy that 404 is provably a lie, and believing it means a sha-less PUT
# against an existing path -> 422 -> remediation fails on the commonest dispatch
# there is: fresh branch, drifted file.
guard.time.sleep = lambda _s: None  # no real backoff in tests


def _flaky(n_404s, on_fresh_branch=True):
    state = {"reads": 0}

    def handler(args):
        CALLS.append(list(args))
        joined = " ".join(args)
        if "git/ref/heads/" in joined and "-X" not in joined:
            return "basesha123\n"
        if "git/refs" in joined and "-X" in args:
            if on_fresh_branch:
                return "{}"
            raise guard.GhError(422, "Reference already exists (HTTP 422)")
        if "contents/" in joined and "-X" not in args:
            state["reads"] += 1
            if state["reads"] <= n_404s:
                raise guard.GhError(404, "Not Found (HTTP 404)")
            # Drifted content, so the write is not skipped and this case still
            # exercises the sha-bearing PUT it was written for.
            return json.dumps({
                "sha": "realsha",
                "content": base64.b64encode(b"drifted bytes\n").decode("ascii"),
            })
        if "-X" in args and "PUT" in args:
            return "{}"
        if args[0] == "pr" and args[1] == "list":
            return "\n"
        if args[0] == "pr" and args[1] == "create":
            return "https://github.com/acme/repo/pull/9\n"
        return "{}"
    return handler


CALLS.clear()
stub(_flaky(2))
_err = guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", True)],
                              "/tmp/rem-src", 1608)
_puts = [c for c in CALLS if "PUT" in c]
record(_err is None and _puts and any(a.startswith("sha=realsha") for a in _puts[0]),
       "remediation: a transient 404 on a fresh branch is retried, not read as absence",
       f"err={_err} put={_puts[0] if _puts else None}")

# ...and if it NEVER appears, refuse. A sha-less write against a path the base has
# is the thing being prevented; failing closed is the correct outcome.
CALLS.clear()
stub(_flaky(99))
_err = guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", True)],
                              "/tmp/rem-src", 1608)
record(_err is not None and "refusing a sha-less write" in _err
       and not any("PUT" in c for c in CALLS),
       "remediation: a 404 that never resolves fails CLOSED, with no sha-less write",
       f"err={_err}")

# A MISSING copy is allowed to 404 - that is the create case - but only after a
# confirming re-read, so a single blip is not trusted.
CALLS.clear()
stub(_flaky(99))
_err = guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", False)],
                              "/tmp/rem-src", 1608)
_reads = [c for c in CALLS if "contents/" in " ".join(c) and "-X" not in c]
record(_err is None and len(_reads) == 2,
       "remediation: a missing copy confirms absence with a re-read before creating",
       f"err={_err} reads={len(_reads)}")


# A REUSED branch may legitimately 404: it can have been cut before the file
# existed on the base, so that 404 is honest and permanent. Retrying then failing
# closed strands the repo forever -- the shape that stuck standards-sync (#197).
CALLS.clear()
stub(_flaky(99, on_fresh_branch=False))
_err = guard.remediate_copies("acme", "repo", "develop", [("copy-a.yml", True)],
                              "/tmp/rem-src", 1608)
_puts = [c for c in CALLS if "PUT" in c]
record(_err is None and len(_puts) == 1
       and not any(a.startswith("sha=") for a in _puts[0]),
       "remediation: a REUSED branch's 404 is honest - create, do not fail closed",
       f"err={_err} puts={len(_puts)}")


# --------------------------------------------------------------- exit contract
# backend#1965. The rule lived inline at the bottom of main(), reachable only by
# a full audit against the live org -- so nothing asserted it, and `--create-prs`
# reported a fully successful remediation as a red "Drift" run for as long as it
# did. decide_exit() is that rule as a pure function, so the outcomes below are
# assertions rather than a description.
#
# The three the ticket names, plus the ones that make the fix safe: partial
# remediation must stay RED, and an unremediable family must not be swept green
# just because --create-prs was passed.

def _exit(**kw):
    base = dict(findings=0, caller_unreadable=0, protection_unreadable=0,
                ruleset_unreadable=0, listing_unreadable=0,
                remediation_failures=0, remediated=0)
    base.update(kw)
    return guard.decide_exit(**base)


_c, _r = _exit()
record(_c == 0 and _r == "", "exit: a clean fleet is 0", f"{_c} {_r!r}")

_c, _r = _exit(findings=3)
record(_c == 1 and "3 repo-conformance" in _r,
       "exit: un-remediated findings are 1 (a plain audit is unchanged)", f"{_c} {_r!r}")

# A listing gap is exit 2, and the headline must name IT rather than blaming
# caller/copy reads (Bugbot, #278). Its fix is "widen the App installation", which
# no other clause would ever suggest -- a true count under a false name sends the
# reader to the wrong place, the same defect the decomposition fixed in #238.
_c, _r = _exit(listing_unreadable=4)
record(_c == 2 and "missing from the org listing" in _r
       and "caller/copy" not in _r,
       "exit: a listing gap is 2 and is NOT announced as a caller-read failure",
       f"{_c} {_r!r}")

# All four buckets at once: every cause must be named, none swallowed.
_c, _r = _exit(caller_unreadable=1, protection_unreadable=2,
               ruleset_unreadable=3, listing_unreadable=4)
record(_c == 2 and "caller/copy" in _r and "branch-protection" in _r
       and "ruleset" in _r and "org listing" in _r,
       "exit: all four unreadable causes are named separately", f"{_c} {_r!r}")

# THE TICKET. Every finding got a PR -> the run did what it was asked.
_c, _r = _exit(findings=3, remediated=3)
record(_c == 0 and _r == "",
       "exit: findings ALL remediated is 0, not 1 -- the reported bug", f"{_c} {_r!r}")

# The half that keeps it honest, and the reason standards-sync's blanket
# `return 0 if create_prs` could not simply be copied: `findings` here mixes
# remediable copies with protection/ruleset/quality findings that no PR can fix.
_c, _r = _exit(findings=5, remediated=2)
record(_c == 1 and "3 repo-conformance drift finding(s) remain un-remediated" in _r
       and "2 got a PR" in _r,
       "exit: PARTIAL remediation stays RED and says how many are left", f"{_c} {_r!r}")

_c, _r = _exit(findings=4, remediated=0)
record(_c == 1,
       "exit: --create-prs that remediated NOTHING is still red", f"{_c} {_r!r}")

# Unknown outranks both, in both directions.
_c, _r = _exit(findings=3, remediated=3, remediation_failures=1)
record(_c == 2 and "could not be remediated" in _r,
       "exit: a failed remediation is 2 even when the others succeeded", f"{_c} {_r!r}")

_c, _r = _exit(findings=3, remediated=3, caller_unreadable=1)
record(_c == 2 and "caller/copy state UNKNOWN" in _r,
       "exit: an unreadable repo is 2 even when every finding was remediated", f"{_c} {_r!r}")

_c, _r = _exit(protection_unreadable=1)
record(_c == 2 and "protection state UNKNOWN" in _r,
       "exit: an unreadable protection read is 2 and names protection", f"{_c} {_r!r}")

_c, _r = _exit(ruleset_unreadable=1)
record(_c == 2 and "ruleset state UNKNOWN" in _r,
       "exit: an unreadable ruleset read is 2 and names rulesets", f"{_c} {_r!r}")

# A remediator that ever fixes two findings with one entry must not read as
# "some left over" -- pins the `>=` rather than `==`.
_c, _r = _exit(findings=2, remediated=3)
record(_c == 0, "exit: over-counting remediation is still green, not partial", f"{_c} {_r!r}")

# Green must require the count to ACCOUNT for the findings, not merely be
# non-zero. Without this, `remediated and ...` could decay into `if remediated`.
_c, _r = _exit(findings=9, remediated=1)
record(_c == 1, "exit: one PR does not make nine findings green", f"{_c} {_r!r}")


failed = [row for row in RESULTS if not row[0]]
print(f"\npass={len(RESULTS) - len(failed)} fail={len(failed)}")
if failed:
    for _ok, name, detail in failed:
        print(f"  FAILED: {name} :: {detail}")
sys.exit(1 if failed else 0)
