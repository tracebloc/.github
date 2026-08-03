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

import base64
import copy
import importlib.util
import json
import os
import sys
import tempfile

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

MINIMAL = {
    "schema_version": 1,
    "org": "acme",
    "pinned_ref": "main",
    "audit_branch": "develop-first",
    "source_repo": "hub",
    "reusables": ["a.yml"],
    "copies": ["c.yml"],
    "repos": {
        "hub": {
            "visibility": "public",
            "release_train": False,
            "callers": {"a.yml": "required"},
            "copies": {"c.yml": "required"},
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
expect_schema_failure("audit_branch other than develop-first rejected",
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
META = {"visibility": "private", "default_branch": "main"}
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
    read = guard.read_repo("acme", "repo", META, COPIES)
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
            raise AssertionError(f"not develop-first: {args[1]}")
        return TREE_ONE
    return blob(
        b"name: FR gate\n"
        b"on:\n  pull_request:\n"
        b"jobs:\n  gate:\n"
        b"    uses: tracebloc/.github/.github/workflows/fr-gate.yml@main\n"
        b"    secrets: inherit\n")


stub(_good)
read = guard.read_repo("acme", "repo", META, COPIES)
record(
    read.ok
    and read.branch == "develop"
    and read.callers.get("fr-gate.yml") == [("fr-gate-caller.yml", "main")],
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
read = guard.read_repo("acme", "repo", META, COPIES)
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
read = guard.read_repo("acme", "repo", META, COPIES)
record(read.ok and read.callers.get("fr-gate.yml") == [("fr-gate-caller.yml", "v1.2.3")],
       "a caller on an unexpected ref is captured with its ref, for the pin check",
       f"callers={read.callers}")


def _copy_sha(args):
    if _branches(args):
        return "main\n"
    if _tree(args):
        return json.dumps({"truncated": False, "tree": [
            {"type": "blob", "path": ".github/workflows/add-to-kanban.yml", "sha": "deadbeef"}]})
    return blob(b"name: add to kanban\non:\n  issues:\n")


stub(_copy_sha)
read = guard.read_repo("acme", "repo", META, COPIES)
record(read.ok and read.copies.get("add-to-kanban.yml") == "deadbeef",
       "a copy is recorded by blob sha so content can be compared",
       f"copies={read.copies}")

record(guard.blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a",
       "blob_sha matches git's own object id",
       guard.blob_sha(b"hello\n"))

failed = [row for row in RESULTS if not row[0]]
print(f"\npass={len(RESULTS) - len(failed)} fail={len(failed)}")
if failed:
    for _ok, name, detail in failed:
        print(f"  FAILED: {name} :: {detail}")
sys.exit(1 if failed else 0)
