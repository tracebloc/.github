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

def _policy(**over):
    base = {
        "classic_protection": True,
        "min_reviews": 1,
        "enforce_admins": None,
        "block_force_pushes": True,
        "block_deletions": True,
        "require_conversation_resolution": True,
        "strict": False,
    }
    base.update(over)
    return base


MINIMAL = {
    "schema_version": 2,
    "org": "acme",
    "pinned_ref": "main",
    "audit_branch": "develop-first",
    "source_repo": "hub",
    "reusables": ["a.yml"],
    "copies": ["c.yml"],
    "protection_policy": {
        "develop": _policy(),
        "staging": _policy(),
        "prod": _policy(enforce_admins=True),
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

failed = [row for row in RESULTS if not row[0]]
print(f"\npass={len(RESULTS) - len(failed)} fail={len(failed)}")
if failed:
    for _ok, name, detail in failed:
        print(f"  FAILED: {name} :: {detail}")
sys.exit(1 if failed else 0)
