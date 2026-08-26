#!/usr/bin/env python3
"""Offline self-test for scripts/standards-sync.py (tracebloc/backend#1602).

Same contract as caller-drift-selftest.py: no network, no token. The sync's
whole job is splicing a managed block into files other people own, so the
splice logic and its fail-closed paths are asserted here rather than trusted.

Exit 0 when every path behaves the way it is supposed to.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, os.pardir, "standards-sync.py")

_spec = importlib.util.spec_from_file_location("standards_sync", GUARD)
if _spec is None or _spec.loader is None:
    sys.exit(f"cannot import {GUARD}")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

RESULTS: "list[tuple[bool, str, str]]" = []
CANON = "# tracebloc engineering standards\n\n- rule one\n- rule two\n"


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        {detail}")


def expect_exit_2(name: str, fn) -> None:
    try:
        fn()
    except SystemExit as exc:
        record(exc.code == 2, name, f"exited {exc.code} (want 2)")
        return
    record(False, name, "returned instead of failing closed")


# ---------------------------------------------------------------- classify()
record(sync.classify(None, CANON) == sync.NO_FILE,
       "classify: absent file", "None -> NO_FILE")
record(sync.classify("# repo notes\n", CANON) == sync.MISSING_BLOCK,
       "classify: no markers", "plain file -> MISSING_BLOCK")

stamped = f"# repo notes\n\n{sync.render_block(CANON)}\n"
record(sync.classify(stamped, CANON) == sync.IN_SYNC,
       "classify: freshly stamped file is IN_SYNC", "render_block round-trips")
record(sync.classify(stamped, CANON + "- rule three\n") == sync.DRIFTED,
       "classify: canon moved on", "old block vs new canon -> DRIFTED")

for label, text in [
    ("begin without end", f"x\n{sync.BEGIN}\ny\n"),
    ("end without begin", f"x\n{sync.END}\ny\n"),
    ("end before begin", f"{sync.END}\nmiddle\n{sync.BEGIN}\n"),
    ("two begins", f"{sync.BEGIN}\n{sync.BEGIN}\na\n{sync.END}\n"),
]:
    record(sync.classify(text, CANON) == sync.MALFORMED,
           f"classify: {label}", "unpaired/duplicated markers -> MALFORMED")

# ------------------------------------------------------------ build_desired()
created = sync.build_desired(None, CANON, sync.NO_FILE)
record(sync.classify(created, CANON) == sync.IN_SYNC and created.startswith("# CLAUDE.md"),
       "build: NO_FILE creates stub + block", "stub header present, result IN_SYNC")

repo_owned = "# repo notes\n\nlocal gotcha: keep this line.\n"
appended = sync.build_desired(repo_owned, CANON, sync.MISSING_BLOCK)
record(sync.classify(appended, CANON) == sync.IN_SYNC
       and "local gotcha: keep this line." in appended
       and appended.index("local gotcha") < appended.index(sync.BEGIN),
       "build: MISSING_BLOCK appends below repo content",
       "repo-owned prose preserved, block appended, result IN_SYNC")

old = f"pre kept\n{sync.render_block('old canon body')}\npost kept\n"
replaced = sync.build_desired(old, CANON, sync.DRIFTED)
record(sync.classify(replaced, CANON) == sync.IN_SYNC
       and replaced.startswith("pre kept\n") and replaced.endswith("post kept\n")
       and "old canon body" not in replaced,
       "build: DRIFTED replaces only the block",
       "prefix/suffix byte-identical, old inner gone, result IN_SYNC")

resynced = sync.build_desired(replaced, CANON, sync.classify(replaced, CANON)) \
    if sync.classify(replaced, CANON) != sync.IN_SYNC else replaced
record(resynced == replaced, "build: idempotent",
       "re-running the sync on an IN_SYNC file changes nothing")

try:
    sync.build_desired(f"{sync.BEGIN}\n{sync.BEGIN}\n{sync.END}\n", CANON, sync.MALFORMED)
    record(False, "build: MALFORMED is never spliced", "splice happened")
except AssertionError:
    record(True, "build: MALFORMED is never spliced", "AssertionError as designed")

# --------------------------------------------------------------- load_canon()
with tempfile.TemporaryDirectory() as tmp:
    empty = os.path.join(tmp, "empty.md")
    open(empty, "w", encoding="utf-8").close()
    expect_exit_2("canon: empty file fails closed", lambda: sync.load_canon(empty))

    nested = os.path.join(tmp, "nested.md")
    with open(nested, "w", encoding="utf-8") as handle:
        handle.write(f"rules\n{sync.BEGIN}\n")
    expect_exit_2("canon: marker inside canon fails closed", lambda: sync.load_canon(nested))

    expect_exit_2("canon: unreadable path fails closed",
                  lambda: sync.load_canon(os.path.join(tmp, "missing.md")))

# ------------------------------------------------------------- load_targets()
with tempfile.TemporaryDirectory() as tmp:
    no_repos = os.path.join(tmp, "inv.yml")
    with open(no_repos, "w", encoding="utf-8") as handle:
        handle.write("org: tracebloc\n")
    expect_exit_2("inventory: missing repos key fails closed",
                  lambda: sync.load_targets(no_repos))

    # These two exercise the EXEMPT MECHANISM, so they inject their own entry
    # rather than leaning on whatever real repo happens to be exempt. Naming a
    # live one made them break the moment that repo was archived and its entry
    # removed -- a test of the mechanism should not depend on today's fleet.
    _real_exempt = sync.EXEMPT
    try:
        sync.EXEMPT = {"exempt-fixture": "written reason, for the selftest"}

        stale_exempt = os.path.join(tmp, "inv2.yml")
        with open(stale_exempt, "w", encoding="utf-8") as handle:
            handle.write("org: tracebloc\nrepos:\n  backend: {}\n")
        # EXEMPT names exempt-fixture; an inventory without it must fail, not skip.
        expect_exit_2("inventory: exemption naming an unknown repo fails closed",
                      lambda: sync.load_targets(stale_exempt))

        good = os.path.join(tmp, "inv3.yml")
        with open(good, "w", encoding="utf-8") as handle:
            handle.write("org: tracebloc\nrepos:\n  backend: {}\n  exempt-fixture: {}\n")
        org, targets = sync.load_targets(good)
        record(org == "tracebloc" and targets == ["backend"],
               "inventory: exempt repo excluded from targets",
               f"targets={targets} (exempt-fixture exempt with written reason)")
    finally:
        sync.EXEMPT = _real_exempt

# ----------------------------------------------------------- crash semantics
# An operational crash must exit 2 ("could not evaluate"), never 1 — the
# workflow reads 1 as confirmed drift and would report a crash as a drift
# finding. Run the guard as a subprocess with `gh` stripped from PATH: the
# first repo read raises FileNotFoundError, and the entry point must map it.
with tempfile.TemporaryDirectory() as tmp:
    canon_path = os.path.join(tmp, "canon.md")
    with open(canon_path, "w", encoding="utf-8") as handle:
        handle.write(CANON)
    inv_path = os.path.join(tmp, "inv.yml")
    with open(inv_path, "w", encoding="utf-8") as handle:
        handle.write("org: tracebloc\nrepos:\n  backend: {}\n")
    empty_bin = os.path.join(tmp, "bin")
    os.makedirs(empty_bin)
    env = dict(os.environ, PATH=empty_bin)  # no gh anywhere on PATH
    env.pop("GITHUB_STEP_SUMMARY", None)
    proc = subprocess.run(
        [sys.executable, GUARD, "--canonical", canon_path,
         "--inventory", inv_path, "--repo", "backend"],
        capture_output=True, text=True, env=env, check=False,
    )
    record(proc.returncode == 2, "crash: gh unavailable exits 2, not 1",
           f"exited {proc.returncode} (want 2 — a crash must never read as drift)")

# ---------------------------------------------------- fresh-branch read race
# Run 31373298821 (design rule 5): a file read on a just-created branch
# transiently 404'd, the 404 was believed, and the sha-less write was
# rejected as 'Invalid request'. These stubs assert the retry / fail-closed
# contract with no network: sync.gh is replaced by a scripted fake and the
# backoff sleep by a no-op.
class GhScript:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if not self.steps:
            raise AssertionError("gh called more often than scripted")
        return self.steps.pop(0)


OK_PAYLOAD = json.dumps({"sha": "abc123", "content": base64.b64encode(b"hello").decode()})
NOT_FOUND = (1, "", "gh: Not Found (HTTP 404)")
_real_gh, _real_sleep = sync.gh, sync.time.sleep
sync.time.sleep = lambda _s: None
try:
    stub = GhScript([NOT_FOUND, (0, OK_PAYLOAD, "")])
    sync.gh = stub
    sha, content, err = sync._read_head_file("o/r", "docs/x", expect_file=True)
    record(err is None and sha == "abc123" and content == "hello" and len(stub.calls) == 2,
           "race: transient 404 is retried to success",
           f"attempts={len(stub.calls)} sha={sha} err={err}")

    stub = GhScript([NOT_FOUND] * 5)
    sync.gh = stub
    sha, content, err = sync._read_head_file("o/r", "docs/x", expect_file=True)
    record(err is not None and len(stub.calls) == 5,
           "race: persistent 404 fails closed after all retries",
           f"attempts={len(stub.calls)} err={(err or '')[:70]}")

    stub = GhScript([NOT_FOUND] * 2)
    sync.gh = stub
    sha, content, err = sync._read_head_file("o/r", "docs/x", expect_file=False)
    record(err is None and sha is None and len(stub.calls) == 2,
           "race: genuine absence is confirmed by a re-read, not trusted once",
           f"attempts={len(stub.calls)}")

    stub = GhScript([
        (1, "", "gh: Invalid request (HTTP 422)"),  # sha-less PUT rejected
        (0, OK_PAYLOAD, ""),                        # sha refresh read
        (0, "{}", ""),                              # retried PUT succeeds
    ])
    sync.gh = stub
    err = sync._write_head_file("o/r", "docs/x", "new content", None, 1602)
    record(err is None and any(a == "sha=abc123" for a in stub.calls[-1]),
           "race: rejected write refreshes the sha and retries exactly once",
           f"err={err}, retried PUT carries the refreshed sha")

    stub = GhScript([
        (1, "", "gh: Invalid request (HTTP 422)"),
        (0, OK_PAYLOAD, ""),
        (1, "", "gh: Conflict (HTTP 409)"),
    ])
    sync.gh = stub
    err = sync._write_head_file("o/r", "docs/x", "new content", None, 1602)
    record(err is not None and len(stub.calls) == 3,
           "race: a second rejection fails closed",
           f"err={(err or '')[:70]}")

    # The second-rejection message used to be UNREACHABLE: attempt 2 fell through
    # to the generic return inside the loop, so the one failure worth naming was
    # the one nobody could see (Bugbot .github#197).
    record("racing this branch" in (err or ""),
           "race: a second rejection SAYS it is a real conflict, not a generic error",
           f"err={(err or '')[:90]}")

    # A REUSED sync branch may predate CLAUDE.md on the base, so a 404 on it is
    # honest and permanent. Treating file_on_base alone as expect_file made the
    # read retry five times and fail closed, so the sha-less create could never
    # run and that repo was stuck forever (Bugbot .github#197).
    #
    # ASSERTS THE READ COUNT, not the end state. A first version of this check
    # asserted only "the file got created", and passed with the bug still in
    # place: the retrying read swallowed the scripted PUT response, read its `{}`
    # body as the file, and the run limped to the same end state by a completely
    # different path. It proved nothing in either direction. The count is the
    # thing that actually differs -- 2 reads when the 404 is believed, 5 when it
    # is not.
    stub = GhScript([
        (0, "sha_of_base", ""),                       # resolve base head
        (1, "", "gh: Reference already exists (HTTP 422)"),   # branch REUSED, not fresh
        NOT_FOUND, NOT_FOUND,                          # absence, confirmed by one re-read
        (0, "{}", ""),                                 # sha-less PUT creates it
        (0, "[]", ""),                                 # pr list
        (0, "https://x/pull/1", ""),                   # pr create
    ])
    sync.gh = stub
    try:
        err = sync.remediate("o", "r", "develop", "content", 1602, file_on_base=True)
        reads = [c for c in stub.calls if any("contents/CLAUDE.md?ref=" in str(a) for a in c)]
        record(err is None and len(reads) == 2,
               "reused branch: a genuine 404 is absence after ONE re-read, not a retry storm",
               f"err={err} reads={len(reads)} (5 would mean it wrongly expected the file)")
    except AssertionError as exc:
        # The stub ran out, which here means the read kept retrying past the
        # absence confirmation -- the bug itself.
        record(False,
               "reused branch: a genuine 404 is absence after ONE re-read, not a retry storm",
               f"read retried past the absence confirmation ({exc})")
finally:
    sync.gh, sync.time.sleep = _real_gh, _real_sleep

# ------------------------------------------------- _ensure_pr(): the PR title
# WHY: the title had no coverage at all, and that is how it shipped naming
# backend#1602 parenthetically. closing-ref-gate.py then refused every sync PR
# the remediation opened -- 19 repos, all red, none of them mergeable. The rule
# is not restated here: parse_title is imported from the REAL gate, so if the
# gate's notion of "names a ticket" changes, this test moves with it.
_gate_path = os.path.join(HERE, os.pardir, "closing-ref-gate.py")
_gspec = importlib.util.spec_from_file_location("closing_ref_gate", _gate_path)
if _gspec is None or _gspec.loader is None:
    sys.exit(f"cannot import {_gate_path}")
gate = importlib.util.module_from_spec(_gspec)
_gspec.loader.exec_module(gate)

try:
    stub = GhScript([
        (0, "", ""),                    # pr list -> no open PR
        (0, "https://x/pull/7", ""),    # pr create
        (0, "", ""),                    # pr edit --add-assignee
    ])
    sync.gh = stub
    sync._ensure_pr("o/r", "head", "develop", 1602)
    created = [c for c in stub.calls if "create" in c]
    title = created[0][created[0].index("--title") + 1] if created else ""
    body = created[0][created[0].index("--body") + 1] if created else ""

    named = gate.parse_title(title)
    record(bool(created) and not named,
           "_ensure_pr: the PR title names no ticket the PR does not close",
           f"title={title!r} -> closing-ref-gate.parse_title found {len(named)} ref(s); "
           "any ref here would demand a closing link to an epic 19 PRs share")

    record("backend#1602" in body and "Closes" not in body,
           "_ensure_pr: the body keeps traceability WITHOUT a closing keyword",
           "'Part of ...#1602' is a reference; 'Closes' would close the epic on the "
           f"first merge (body mentions 1602={'backend#1602' in body}, "
           f"has Closes={'Closes' in body})")

    # Mutation anchor: prove the assertion above is live rather than vacuous.
    # If parse_title cannot see a ticket in a title that plainly has one, the
    # check would pass for the wrong reason and the bug would return unseen.
    record(bool(gate.parse_title("docs(claude): sync org-standards block (backend#1602)")),
           "_ensure_pr: the title assertion is not vacuous",
           "the pre-fix title IS seen as naming a ticket, so a regression reddens")
finally:
    sync.gh = _real_gh

# ---------------------------------------------------------------------- tally
failed = [name for ok, name, _ in RESULTS if not ok]
print(f"\n{len(RESULTS)} checks, {len(failed)} failed.")
if failed:
    for name in failed:
        print(f"  FAIL: {name}")
    sys.exit(1)
