#!/usr/bin/env python3
"""Offline self-test for scripts/standards-sync.py (tracebloc/backend#1602).

Same contract as caller-drift-selftest.py: no network, no token. The sync's
whole job is splicing a managed block into files other people own, so the
splice logic and its fail-closed paths are asserted here rather than trusted.

Exit 0 when every path behaves the way it is supposed to.
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
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
        self.kwargs = []          # parallel to .calls; records the identity each ran as

    def __call__(self, *args, **kwargs):
        # KWARGS ARE RECORDED, not swallowed. `gh(..., token=...)` is how PR
        # creation runs as the human instead of the App (backend#2590), and a stub
        # that accepted **kwargs and dropped them would let that argument vanish
        # while every assertion here still passed.
        self.calls.append(args)
        self.kwargs.append(kwargs)
        if not self.steps:
            raise AssertionError("gh called more often than scripted")
        return self.steps.pop(0)

    def kwargs_for(self, needle):
        """The kwargs of the first call containing `needle`, or None."""
        for args, kw in zip(self.calls, self.kwargs):
            if needle in args:
                return kw
        return None


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
        # EMPTY, not "[]" -- `--jq '.[0].number // empty'` returns an empty
        # string when there is no open PR, and "[]" is TRUTHY. With the
        # existing-PR path now also making two edit calls (#348), a "[]" here
        # would send this test down that branch while still consuming the same
        # number of stub entries -- passing while exercising the opposite case.
        (0, "", ""),                                   # pr list -- none yet
        (0, "https://x/pull/1", ""),                   # pr create
        (0, "", ""),                                   # pr edit --add-reviewer
        (0, "", ""),                                   # pr edit --add-assignee
    ])
    sync.gh = stub
    try:
        err = sync.remediate("o", "r", "develop", "content", 1602, file_on_base=True,
                             author_token="pat-for-the-human")
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
        (0, "", ""),                    # pr edit --add-reviewer
        (0, "", ""),                    # pr edit --add-assignee
    ])
    sync.gh = stub
    os.environ[sync.AUTHOR_TOKEN_ENV] = "pat-for-the-human"
    sync._ensure_pr("o/r", "head", "develop", 1602, "pat-for-the-human")
    created = [c for c in stub.calls if "create" in c]
    title = created[0][created[0].index("--title") + 1] if created else ""
    body = created[0][created[0].index("--body") + 1] if created else ""

    named = gate.parse_title(title)
    record(bool(created) and not named,
           "_ensure_pr: the PR title names no ticket the PR does not close",
           f"title={title!r} -> closing-ref-gate.parse_title found {len(named)} ref(s); "
           "any ref here would demand a closing link to an epic 19 PRs share")

    # ALL THREE KEYWORD FAMILIES, not just "Closes". GitHub honours close/closes/
    # closed, fix/fixes/fixed and resolve/resolves/resolved, case-insensitively, and
    # any one of them creates the closing link. Asserting only "Closes" left the door
    # this whole PR exists to shut: `Fixes tracebloc/backend#1602` would have passed
    # and closed the epic on the first of nineteen merges (Asad, .github#345).
    # Written here rather than imported because closing-ref-gate.py has no such
    # constant to import -- it delegates to GitHub's computed
    # closingIssuesReferences and never scans text. If it ever grows one, import it
    # the way parse_title is imported above and delete this tuple.
    CLOSING_KEYWORDS = (
        "close", "closes", "closed",
        "fix", "fixes", "fixed",
        "resolve", "resolves", "resolved",
    )

    def closing_keyword_in(text: str) -> "str | None":
        low = text.lower()
        return next((k for k in CLOSING_KEYWORDS if f"{k} " in low), None)

    found = closing_keyword_in(body)
    record("backend#1602" in body and found is None,
           "_ensure_pr: the body keeps traceability WITHOUT any closing keyword",
           f"'Part of ...#1602' is a reference (mentions 1602={'backend#1602' in body}); "
           f"closing keyword found={found!r} — any of {len(CLOSING_KEYWORDS)} forms would "
           "close the epic on the first of nineteen merges")

    # Mutation anchor for the check above: the scan must catch a family it is not
    # named after, or it is just the old "Closes"-only assertion wearing a tuple.
    _fx = closing_keyword_in("Fixes tracebloc/backend#1602")
    _rs = closing_keyword_in("Resolves tracebloc/backend#1602")
    _cl = closing_keyword_in("Closed tracebloc/backend#1602")
    _pt = closing_keyword_in("Part of tracebloc/backend#1602")
    record(bool(_fx) and _fx.startswith("fix")
           and bool(_rs) and _rs.startswith("resolve")
           and bool(_cl) and _cl.startswith("clos")
           and _pt is None,
           "_ensure_pr: the keyword scan catches all three families, not only close/",
           f"Fixes -> {_fx!r}, Resolves -> {_rs!r}, Closed -> {_cl!r}, 'Part of' -> {_pt!r} "
           "(stem-prefix, not equality: the trailing space in the probe means the "
           "inflected form matches, so 'Fixes' resolves to 'fixes' and not 'fix')")

    # Mutation anchor: prove the assertion above is live rather than vacuous.
    # If parse_title cannot see a ticket in a title that plainly has one, the
    # check would pass for the wrong reason and the bug would return unseen.
    record(bool(gate.parse_title("docs(claude): sync org-standards block (backend#1602)")),
           "_ensure_pr: the title assertion is not vacuous",
           "the pre-fix title IS seen as naming a ticket, so a regression reddens")
finally:
    sync.gh = _real_gh
    os.environ.pop(sync.AUTHOR_TOKEN_ENV, None)

# ------------------------------------- _ensure_pr(): WHO the PR is opened as
# WHY: the author is the whole of backend#2590. An App installation token makes
# the author `tracebloc-release-train[bot]`, Cursor Bugbot keys its review on the
# author's seat, and a Bot has none -- so Bugbot reviewed 0 of 14 sync PRs and
# `bugbot / review` failed closed on every one. The identity is invisible on the
# resulting PR (same title, same body, same diff), so nothing but an assertion on
# the CALL can tell the two apart.
try:
    _real_gh = sync.gh

    # -- the PAT reaches `pr create`, and the ambient identity does not ----------
    stub = GhScript([
        (0, "", ""),                    # pr list -> no open PR
        (0, "https://x/pull/9", ""),    # pr create
        (0, "", ""),                    # pr edit --add-reviewer
        (0, "", ""),                    # pr edit --add-assignee
    ])
    sync.gh = stub
    os.environ[sync.AUTHOR_TOKEN_ENV] = "pat-for-the-human"
    err = sync._ensure_pr("o/r", "head", "develop", 1602, "pat-for-the-human")

    create_kw = stub.kwargs_for("create")
    list_kw = stub.kwargs_for("list")
    record(err is None and (create_kw or {}).get("token") == "pat-for-the-human",
           "_ensure_pr: `pr create` runs as the author PAT, not the ambient App token",
           f"err={err} create kwargs={create_kw} -- without token= the author is "
           "tracebloc-release-train[bot] and Bugbot skips the PR")

    # The OTHER half of the split, and the half a careless fix breaks: passing the
    # PAT everywhere would work for Bugbot and quietly undo backend#2036's
    # org-scoped read. `pr list` must still run as the ambient App identity.
    record(list_kw == {},
           "_ensure_pr: only PR creation changes identity -- `pr list` stays the App",
           f"list kwargs={list_kw} (want {{}}: a token= here would mean the PAT "
           "leaked onto the fleet-read path, reverting backend#2036)")

    # -- reviewer AND assignee, both SYNC_REVIEWER ------------------------------
    edits = [c for c in stub.calls if "edit" in c]
    reviewer_edit = [c for c in edits if "--add-reviewer" in c]
    assignee_edit = [c for c in edits if "--add-assignee" in c]
    record(len(reviewer_edit) == 1 and sync.SYNC_REVIEWER in reviewer_edit[0]
           and len(assignee_edit) == 1 and sync.SYNC_REVIEWER in assignee_edit[0],
           "_ensure_pr: requests review from SYNC_REVIEWER and assigns the same person",
           f"reviewer={reviewer_edit} assignee={assignee_edit}")

    # THE REVIEWER MAY NOT BE THE AUTHOR. GitHub refuses an approving review from
    # a PR's own author, so a sync PR authored by X and reviewed by X can never
    # merge -- precisely the state 4 of the 14 open PRs were in before they were
    # reassigned (docs#143, release-train#130, .github#344, claude-skills#39).
    #
    # THIS USED TO READ `SYNC_REVIEWER != "LukasWodka"` (@saqlainsyed007, #348).
    # Two literals, in two files, agreeing with each other and with nothing
    # else: re-provision SYNC_PR_AUTHOR_TOKEN to saqlainsyed007's PAT and
    # author == reviewer, every --add-reviewer 422s, the deadlock returns, and
    # this check stays GREEN because neither literal moved. The invariant is
    # about the TOKEN's owner, so it is now asked of the token.

    # -- fail closed with no PAT ------------------------------------------------
    # The important direction. A fallback to the App token would open a PR that
    # looks identical and that Bugbot silently skips, so "no PAT" must produce NO
    # PR at all -- not a bot-authored one.
    # THE HAPPY PATH IS FULLY SCRIPTED, on purpose. Scripting only `pr list` also
    # "catches" a fallback -- but by over-running the stub and raising out of the
    # suite, which is a crash, not a verdict. Then the run is red with no FAIL line
    # naming this behaviour, and a mutation harness that greps for one records
    # UNCAUGHT (measured: it did). Give the fallback every step it would need to
    # SUCCEED, so what reddens is this assertion and not the scaffolding (rule 10:
    # assert the specific failure).
    stub = GhScript([
        (0, "", ""),                    # pr list -> no open PR
        (0, "https://x/pull/13", ""),   # pr create -- MUST NOT be reached
        (0, "", ""),                    # pr edit --add-reviewer
        (0, "", ""),                    # pr edit --add-assignee
    ])
    sync.gh = stub
    os.environ[sync.AUTHOR_TOKEN_ENV] = ""
    # THE EMPTY TOKEN IS THE INPUT, so it is passed as one. `main()` now refuses
    # before any write (#348), and this pins the belt-and-braces refusal that
    # remains here for a caller that skipped that gate.
    err = sync._ensure_pr("o/r", "head", "develop", 1602, "")
    created = [c for c in stub.calls if "create" in c]
    record(err is not None and not created and sync.AUTHOR_TOKEN_ENV in err,
           "_ensure_pr: an empty PAT opens NO PR and says which variable is missing",
           f"err={(err or '')[:80]!r} create calls={len(created)} (want 0: a "
           "bot-authored PR here is the backend#2590 defect reappearing)")

    # Mutation anchor for the two checks above: prove the stub can actually SEE a
    # token argument and an absent one, so neither assertion is passing vacuously.
    probe = GhScript([(0, "", "")])
    probe("pr", "create", token="sentinel")
    record(probe.kwargs_for("create") == {"token": "sentinel"}
           and probe.kwargs_for("nonexistent-verb") is None,
           "_ensure_pr: the identity assertions are not vacuous",
           f"stub observed {probe.kwargs_for('create')} for a scripted token= call, and "
           "None for a call that never happened -- so token= going missing reddens")
finally:
    sync.gh = _real_gh
    os.environ.pop(sync.AUTHOR_TOKEN_ENV, None)

# ---------------------------------------------------------------------- tally

# --------------------------------- the reviewer-is-not-the-author invariant,
# --------------------------------- asked of the TOKEN rather than of a literal
#
# @saqlainsyed007 on #348. The old form compared two hardcoded logins, so the
# one re-provisioning that breaks it -- pointing SYNC_PR_AUTHOR_TOKEN at
# SYNC_REVIEWER's own PAT -- was invisible to the suite. `author_login` asks
# GitHub who the credential is, and `main()` refuses before any write.
_real_gh = sync.gh
try:
    stub = GhScript([(0, "saqlainsyed007", "")])
    sync.gh = stub
    record(sync.author_login("pat") == "saqlainsyed007",
           "author_login: resolves the credential's owner from GitHub",
           f"got {sync.author_login!r}")

    stub = GhScript([(1, "", "gh: Bad credentials (HTTP 401)")])
    sync.gh = stub
    record(sync.author_login("pat") is None,
           "author_login: an unresolvable token is None, not a guess",
           "a token GitHub will not identify must not be treated as anybody")

    # AND IT ASKS AS THE TOKEN, not as the ambient App identity -- otherwise it
    # would resolve the App's login and compare the wrong pair.
    stub = GhScript([(0, "someone", "")])
    sync.gh = stub
    sync.author_login("the-pat")
    record(stub.kwargs_for("user") == {"token": "the-pat"},
           "author_login: asks as the PAT, not as the ambient identity",
           f"kwargs={stub.kwargs_for('user')} (the App's login would be the wrong pair)")
finally:
    sync.gh = _real_gh

# ------------------------- the gate that runs BEFORE any repo is written
#
# @saqlainsyed007 on #348, F3 + F4. The three refusals are pinned individually
# because each says something different about what could not be established --
# and because the mutation harness reported "SYNC_REVIEWER becomes the account
# that authors the PRs" as UNCAUGHT once the old literal-vs-literal check was
# retired. This is what catches it.
_real_gh, _real_login = sync.gh, sync.author_login
try:
    record(sync.check_author_identity("") is not None
           and sync.AUTHOR_TOKEN_ENV in sync.check_author_identity(""),
           "check_author_identity: an empty PAT refuses, naming the variable",
           "an empty token must stop the run before the first branch is pushed")

    sync.author_login = lambda _t: None
    refusal = sync.check_author_identity("pat")
    record(refusal is not None and "will not say who it belongs to" in refusal,
           "check_author_identity: an UNRESOLVABLE token refuses rather than guessing",
           f"got {refusal!r} -- 'cannot tell' must not read as 'fine'")

    sync.author_login = lambda _t: sync.SYNC_REVIEWER
    refusal = sync.check_author_identity("pat")
    record(refusal is not None and sync.SYNC_REVIEWER in refusal,
           "check_author_identity: author == SYNC_REVIEWER is refused",
           f"got {refusal!r} -- this is the backend#2590 deadlock, and the old "
           "literal-vs-literal check could not see it")

    # CASE-INSENSITIVELY, because GitHub logins are.
    sync.author_login = lambda _t: sync.SYNC_REVIEWER.upper()
    record(sync.check_author_identity("pat") is not None,
           "check_author_identity: the identity comparison ignores case",
           "GitHub logins are case-insensitive, so a differently-cased owner is "
           "the same person and the same deadlock")

    sync.author_login = lambda _t: "somebody-else"
    record(sync.check_author_identity("pat") is None,
           "check_author_identity: a DIFFERENT owner passes",
           "the gate must not refuse the configuration it exists to permit")
finally:
    sync.gh, sync.author_login = _real_gh, _real_login

# ---------------- the pre-flight PROBES pr-create capability, not just identity
#
# backend#3187. check_author_identity proves the token exists, resolves and is
# not the reviewer -- none of which proves it can reach the fleet. The measured
# fault (run 33868935395) was a fine-grained PAT that passed all three and then
# died on the first `gh pr create` at `repository.defaultBranchRef`, AFTER a
# branch was pushed. probe_pr_capability reads that exact GraphQL surface
# against one target, so the denial is caught before any write.
_real_gh = sync.gh
try:
    stub = GhScript([(0, '{"data":{"repository":{"defaultBranchRef":{"name":"develop"}}}}', "")])
    sync.gh = stub
    refusal = sync.probe_pr_capability("pat", "tracebloc", "averaging-service")
    record(refusal is None,
           "probe_pr_capability: a token that can read defaultBranchRef passes",
           f"got {refusal!r} -- the surface `gh pr create` needs is reachable")

    # AS THE TOKEN, not the ambient App. Probing the App's reach would prove
    # nothing about the PAT that actually opens the PR (the same split as
    # author_login and `pr create`).
    record(stub.kwargs_for("graphql") == {"token": "pat"},
           "probe_pr_capability: probes as the PAT, not the ambient identity",
           f"kwargs={stub.kwargs_for('graphql')} -- the App's reach is the wrong question")

    # THE MEASURED DENIAL. Exactly what a fine-grained PAT with no grant returns;
    # it must become a refusal naming the variable and the surface, not a pass.
    stub = GhScript([(1, "", "GraphQL: Resource not accessible by personal "
                             "access token (repository.defaultBranchRef)")])
    sync.gh = stub
    refusal = sync.probe_pr_capability("pat", "tracebloc", "averaging-service")
    record(refusal is not None and sync.AUTHOR_TOKEN_ENV in refusal
           and "defaultBranchRef" in refusal,
           "probe_pr_capability: the measured pr-create denial becomes a refusal",
           f"got {refusal!r} -- this is what killed the 2026-09-04 create-prs dispatch")

    # NOT VACUOUS: the stub can tell a graphql call from one that never happened,
    # so the token= assertion above is not passing blind.
    record(stub.kwargs_for("nonexistent-verb") is None,
           "probe_pr_capability: the probe assertions are not vacuous",
           "a call that never happened reads as None, so a dropped token= reddens")
finally:
    sync.gh = _real_gh

# ------------------- a PAT that RESOLVES but cannot CREATE stops the fleet
#
# Bugbot on #348. `check_author_identity` proves the token exists, resolves and
# is not the reviewer. None of that proves it can open a PR: the wrong
# fine-grained permissions, or a token never SSO-authorized for the org, passes
# every one of those checks and fails at `pr create` -- after `remediate` has
# pushed a branch and a commit to every drifted repo.
#
# Nothing read-only can fully prove "this token can open a PR"; the only proof
# is opening one. So the guarantee is BOUNDED rather than claimed: the first
# failed creation aborts, leaving at most ONE repo with a branch and no PR.
_real_gh = sync.gh
try:
    stub = GhScript([
        (0, "", ""),                                  # pr list -- none yet
        (1, "", "HTTP 403: Resource not accessible by personal access token"),
    ])
    sync.gh = stub
    raised = None
    try:
        sync._ensure_pr("o/r", "head", "develop", 1602, "pat")
    except sync.AuthorUnusable as exc:
        raised = exc
    record(raised is not None and "cannot open PR" in str(raised),
           "_ensure_pr: a PAT that cannot CREATE raises rather than returning",
           f"raised={raised!r} -- returning a string lets the loop carry on to "
           "the next repo, which is the fleet-wide half-rollout")

    # AND IT IS A DIFFERENT TYPE FROM THE ORDINARY PER-REPO ERROR, because the
    # loop has to tell "this repo failed" from "this credential fails
    # everywhere". A shared string would need matching on prose.
    record(issubclass(sync.AuthorUnusable, Exception)
           and not issubclass(sync.AuthorUnusable, sync.Unreadable),
           "AuthorUnusable is its own type, not a flavour of Unreadable",
           "the loop must distinguish a fleet-wide credential failure from a "
           "per-repo read problem without matching on message text")
finally:
    sync.gh = _real_gh

# ---------------------------------- an existing PR still gets its roles repaired
#
# @saqlainsyed007 on #348, F1. `_ensure_pr` returned as soon as an open PR
# tracked the branch, so a PR whose --add-reviewer failed once was never
# repaired -- and the reviewer is what makes it mergeable, so it sat
# un-mergeable for ever while every later run reported success.
_real_gh = sync.gh
try:
    stub = GhScript([
        # THREE TAB-SEPARATED FIELDS, because the real call now carries
        # `--json number,author` and a jq that renders number/login/is_bot. A
        # bare "42" is what the previous stub returned, and it now means "the
        # author could not be read" -- so leaving it would have this case
        # asserting the fail-closed path under the name of the happy one.
        (0, "42\tLukasWodka\tfalse", ""),   # pr list -- an open, human-authored PR
        (0, "", ""),        # pr edit --add-reviewer
        (0, "", ""),        # pr edit --add-assignee
    ])
    sync.gh = stub
    err = sync._ensure_pr("o/r", "head", "develop", 1602, "pat")
    edits = [c for c in stub.calls if "edit" in c]
    record(err is None and any("--add-reviewer" in c for c in edits),
           "_ensure_pr: an EXISTING PR still gets its reviewer re-requested",
           f"err={err} edits={edits} (returning early leaves a reviewer-less PR "
           "un-mergeable for ever)")
finally:
    sync.gh = _real_gh

# ------------------- an existing BOT-authored PR is refused, not "ensured"
#
# Bugbot on #348. The case above is the whole reason this one is needed: the
# existing-PR path repairs roles and returns None, which is right for a PR this
# code opened as the human and WRONG for the fourteen already open as
# `tracebloc-release-train[bot]`. An author cannot be reassigned, so no amount
# of role repair makes those reviewable -- and reporting them ensured would
# land this change on exactly none of the PRs it was written for.
#
# THE ROLE EDITS MUST NOT HAPPEN EITHER. Returning an error while still editing
# would leave the run reporting a failure it had half-performed.
_real_gh = sync.gh
try:
    stub = GhScript([
        (0, "848\tapp/tracebloc-release-train\ttrue", ""),   # pr list
        # THE ROLE EDITS ARE SCRIPTED THOUGH THEY MUST NOT RUN. Without them a
        # regression over-runs the stub and raises out of the suite: red, but
        # with no assertion naming the behaviour -- scaffolding, not a verdict
        # (CLAUDE.md rule 10). Scripting them makes `not edits` below the thing
        # that goes red.
        (0, "", ""),        # pr edit --add-reviewer  (must NOT be reached)
        (0, "", ""),        # pr edit --add-assignee  (must NOT be reached)
    ])
    sync.gh = stub
    err = sync._ensure_pr("o/r", "head", "develop", 1602, "pat")
    edits = [c for c in stub.calls if "edit" in c]
    record(err is not None and "848" in err and not edits,
           "_ensure_pr: an existing BOT-authored PR is an ERROR, not a repair",
           f"err={err!r} edits={edits} -- a PR's author cannot be changed, so "
           "repairing its roles reports an unreviewable PR as ensured")

    # AND IT NAMES THE REMEDY. The operator has to know that closing it is what
    # lets the next run reopen it as the human; "failed" alone strands the repo.
    record(err is not None and "close" in err.lower(),
           "_ensure_pr: the bot-author refusal says what to do about it",
           f"err={err!r} -- an error nobody can act on stalls the whole fleet")

    # NOT VACUOUS: the same path with is_bot=false must go through.
    stub = GhScript([
        (0, "848\tLukasWodka\tfalse", ""),
        (0, "", ""),
        (0, "", ""),
    ])
    sync.gh = stub
    ok = sync._ensure_pr("o/r", "head", "develop", 1602, "pat")
    record(ok is None,
           "_ensure_pr: the bot-author check is not vacuous",
           f"err={ok!r} -- is_bot=false must still be repaired and reported ensured")
finally:
    sync.gh = _real_gh

# ------------------------- an unreadable author is a finding, not an all-clear
#
# Design rule 1, one layer in. A row that does not parse says nothing about who
# opened the PR, and "not provably a bot" is not "human". Fail closed: three
# states, and only the one that reads `false` proceeds.
_real_gh = sync.gh
try:
    for row, why in (("42", "the old number-only shape"),
                     ("42\tsomebody\tnull", "is_bot absent from the payload")):
        # The role edits are scripted for the same reason as above: a
        # regression must redden the assertion, not over-run the stub.
        stub = GhScript([(0, row, ""), (0, "", ""), (0, "", "")])
        sync.gh = stub
        err = sync._ensure_pr("o/r", "head", "develop", 1602, "pat")
        record(err is not None and not [c for c in stub.calls if "edit" in c],
               f"_ensure_pr: an unreadable author fails closed ({why})",
               f"row={row!r} err={err!r} -- reporting ensured off a read that "
               "failed is the all-clear-from-a-failed-read design rule 1 refuses")
finally:
    sync.gh = _real_gh

# ------------------------------------- a reviewer that cannot be set is FATAL
#
# @saqlainsyed007 on #348, F2. Combined with F1 this was the silent shape: a
# warning, no self-heal, and a green run over a PR that could never merge.
_real_gh = sync.gh
try:
    stub = GhScript([
        # EMPTY, not "[]": the real call carries `--jq '.[0].number // empty'`,
        # so "no open PR" is an empty string. "[]" is truthy and would send this
        # down the existing-PR path instead -- which is how the first version of
        # this stub tested something other than what it names.
        (0, "", ""),                        # pr list -- none yet
        (0, "https://x/pull/7", ""),        # pr create
        (1, "", "HTTP 422: reviewer is the author"),   # --add-reviewer FAILS
    ])
    sync.gh = stub
    err = sync._ensure_pr("o/r", "head", "develop", 1602, "pat")
    record(err is not None and "reviewer" in err.lower(),
           "_ensure_pr: a reviewer that cannot be requested is an ERROR, not a warning",
           f"err={err!r} -- a warning here ships an un-mergeable PR on a green run")

    # THE OTHER HALF STAYS COSMETIC. Making both fatal would fail the run over
    # an assignee, which blocks nothing.
    stub = GhScript([
        (0, "", ""),                        # pr list -- none yet
        (0, "https://x/pull/8", ""),
        (0, "", ""),                        # --add-reviewer succeeds
        (1, "", "HTTP 422: assignee"),      # --add-assignee fails
    ])
    sync.gh = stub
    err = sync._ensure_pr("o/r", "head", "develop", 1602, "pat")
    record(err is None,
           "_ensure_pr: a failed ASSIGNEE stays cosmetic",
           f"err={err!r} -- an assignee blocks no merge, so it must not fail the run")
finally:
    sync.gh = _real_gh

# --------------- a refused credential DISARMS the writes, it does not abort
#
# Bugbot on #348. Moving the PAT gate into `main()` was right -- it stops the
# half-rollout -- but it was implemented with `die()`, which also skipped the
# read-only audit. standards-sync.yml argues against exactly that twenty lines
# above the secret: aborting before the audit turns "PRs could not be opened"
# into "fleet state unknown", which is strictly less information. So the run
# must still classify and report the whole fleet, push nothing, and exit 2.
def _run_main(argv, token, remediate_calls, targets=("alpha",), remediate=None,
              probe=None, fetch=None):
    """`main()` with the fleet reads stubbed. Returns (exit_code, report).

    `targets` and `remediate` are parameters because backend#2690 is a
    MULTI-REPO property: a write failure on the first of several repos must not
    stop the others being CLASSIFIED. A one-repo fleet cannot express that --
    there is no "rest of the fleet" for the abort to have eaten.
    """
    saved = {name: getattr(sync, name)
             for name in ("load_canon", "load_targets", "resolve_branch",
                          "fetch_claude_md", "remediate", "probe_pr_capability")}
    saved_argv, saved_env = sys.argv, os.environ.get(sync.AUTHOR_TOKEN_ENV)
    saved_summary = os.environ.pop("GITHUB_STEP_SUMMARY", None)

    def _remediate(*a, **kw):
        remediate_calls.append((a, kw))
        return None

    sync.load_canon = lambda _p: CANON
    sync.load_targets = lambda _p: ("tracebloc", list(targets))
    sync.resolve_branch = lambda _o, _r: "develop"
    # No markers, so it classifies as drifted -- the only state that reaches
    # the remediation branch at all. `fetch` overrides per-repo (org, repo, branch)
    # so a test can put some targets IN_SYNC and others drifted -- needed to prove
    # the pr-capability probe fires on the first *drifted* repo, not `targets[0]`.
    sync.fetch_claude_md = fetch or (
        lambda _o, _r, _b: "# CLAUDE.md\n\nrepo-owned prose.\n")
    sync.remediate = remediate or _remediate
    # The pr-capability probe (backend#3187) hits the network for real, so it is
    # neutralised by default and a test that wants a refusal passes `probe=`.
    sync.probe_pr_capability = probe or (lambda *_a, **_k: None)
    os.environ[sync.AUTHOR_TOKEN_ENV] = token
    sys.argv = ["standards-sync.py", *argv]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = sync.main()
    except SystemExit as exc:
        # A `die()` anywhere in main() is a RESULT here, not a crash. Letting
        # it propagate would take the whole suite down with no FAIL line --
        # red without a verdict, which the mutation harness cannot tell from a
        # vacuous test (CLAUDE.md rule 10). Aborting IS the regression these
        # checks are about, so it has to be observable as one.
        code = exc.code
    finally:
        for name, fn in saved.items():
            setattr(sync, name, fn)
        sys.argv = saved_argv
        if saved_env is None:
            os.environ.pop(sync.AUTHOR_TOKEN_ENV, None)
        else:
            os.environ[sync.AUTHOR_TOKEN_ENV] = saved_env
        if saved_summary is not None:
            os.environ["GITHUB_STEP_SUMMARY"] = saved_summary
    return code, buf.getvalue()


_real_stderr = sys.stderr
try:
    sys.stderr = io.StringIO()          # the ::error:: annotations are not the subject
    calls: list = []
    code, report = _run_main(["--create-prs"], "", calls)

    record("| alpha |" in report,
           "main: a refused PAT still AUDITS the fleet and prints the table",
           f"code={code} report={report.strip()[:160]!r} -- `die()` here made "
           "'PRs could not be opened' read as 'fleet state unknown'")
    record(not calls,
           "main: a refused PAT pushes nothing -- remediate() is never called",
           f"calls={calls} -- disarming the writes is the whole point of the gate")
    record("NOT REMEDIATED" in report and "REMEDIATION DISABLED" in report,
           "main: the report says the writes were skipped, and why",
           f"report={report.strip()[-200:]!r} -- a table of drifted repos with "
           "no explanation reads as a plain audit nobody asked to remediate")
    record(code == 2,
           "main: a refused PAT exits 2, not the --create-prs 0",
           f"code={code} -- 0 would claim every drifted repo has a PR open")

    # NOT VACUOUS. The same call with a usable credential must remediate and
    # be green, or the four checks above would pass over a script that has
    # simply stopped remediating.
    _real_check = sync.check_author_identity
    try:
        sync.check_author_identity = lambda _t: None
        calls = []
        code, report = _run_main(["--create-prs"], "a-usable-pat", calls)
        record(code == 0 and len(calls) == 1 and "NOT REMEDIATED" not in report,
               "main: the disarm is not vacuous -- a usable PAT still remediates",
               f"code={code} calls={len(calls)}")
    finally:
        sync.check_author_identity = _real_check

    # AND WITHOUT --create-prs the refusal path must not fire at all: the token
    # is irrelevant to a report-only run, so drift must still exit 1.
    calls = []
    code, report = _run_main([], "", calls)
    record(code == 1 and not calls and "NOT REMEDIATED" not in report,
           "main: report-only runs never consult the PAT",
           f"code={code} calls={calls} -- a read-only audit must not be gated "
           "on a credential it does not use")

    # ------------------------------------------------------------------
    # backend#3187. A PAT that PASSES check_author_identity but cannot open a PR
    # (no grant on the fleet -> `gh pr create` refused at defaultBranchRef) must
    # disarm the writes in the pre-flight, exactly like an identity refusal:
    # classify and report the whole fleet, push nothing, exit 2. Before the probe
    # existed this half-rolled-out -- a branch on repo 1 and no PRs anywhere.
    _real_check = sync.check_author_identity
    try:
        sync.check_author_identity = lambda _t: None   # identity is fine ...
        calls = []
        code, report = _run_main(
            ["--create-prs"], "resolves-but-cannot-create", calls,
            probe=lambda *_a, **_k: (            # ... but it cannot open a PR
                f"{sync.AUTHOR_TOKEN_ENV} resolves but cannot read "
                "repository.defaultBranchRef on tracebloc/alpha (backend#3187)."),
        )
        record(not calls,
               "main: a token refused by the pr-capability probe pushes nothing",
               f"calls={calls} -- the probe must disarm writes before the sweep")
        record("REMEDIATION DISABLED" in report and "| alpha |" in report,
               "main: a probe refusal still audits the fleet and says writes were skipped",
               f"report={report.strip()[-200:]!r}")
        record(code == 2,
               "main: a probe refusal exits 2, not the --create-prs 0",
               f"code={code} -- 0 would claim every drifted repo has a PR open")

        # NOT VACUOUS: a probe that passes (None) must let the usable PAT
        # remediate, or the three checks above would pass over a script that had
        # simply stopped remediating.
        calls = []
        code, report = _run_main(["--create-prs"], "a-usable-pat", calls,
                                 probe=lambda *_a, **_k: None)
        record(code == 0 and len(calls) == 1 and "REMEDIATION DISABLED" not in report,
               "main: the probe disarm is not vacuous -- a passing probe remediates",
               f"code={code} calls={len(calls)}")

        # #419 nit 2: THE PROBE IS FIRST-DRIFT, NOT EVERY-RUN. An in-sync fleet
        # has nothing to remediate, so the pr-capability canary must not fire --
        # a probe refusal reddening a no-op run (exit 2 where the pre-probe code
        # returned 0) was the regression. Every target IN_SYNC, and a probe that
        # WOULD refuse: it must never be consulted, and the run exits 0.
        in_sync = sync.build_desired(None, CANON, sync.NO_FILE)
        probe_seen: list = []
        calls = []
        code, report = _run_main(
            ["--create-prs"], "a-usable-pat", calls, targets=("alpha", "bravo"),
            fetch=lambda _o, _r, _b: in_sync,
            probe=lambda _t, _o, r: probe_seen.append(r) or "would refuse",
        )
        record(code == 0 and not probe_seen and not calls
               and "REMEDIATION DISABLED" not in report,
               "main: an in-sync fleet never fires the pr-capability probe (exit 0)",
               f"code={code} probe_seen={probe_seen} calls={calls} -- a no-op run "
               "must not redden on a credential it never needed")

        # #419 nit 1: THE CANARY MATCHES THE FIRST REAL WRITE. `targets[0]` is
        # alphabetically-first, not necessarily the first *drifted* repo, so a PAT
        # that cannot read `targets[0]` but can write the drifted repos was
        # disarming the whole fleet. The probe must hit the first drifted target
        # (`bravo` here, with `alpha` IN_SYNC) -- the repo `remediate()` writes
        # first -- so the canary and the first write are the same repo.
        drifted = "# CLAUDE.md\n\nrepo-owned prose.\n"
        by_repo = {"alpha": in_sync, "bravo": drifted}
        probe_seen = []
        calls = []
        code, report = _run_main(
            ["--create-prs"], "a-usable-pat", calls, targets=("alpha", "bravo"),
            fetch=lambda _o, r, _b: by_repo[r],
            probe=lambda _t, _o, r: probe_seen.append(r) or None,
        )
        record(probe_seen == ["bravo"] and code == 0 and len(calls) == 1,
               "main: the pr-capability probe hits the first DRIFTED target, not targets[0]",
               f"probe_seen={probe_seen} code={code} calls={len(calls)} -- alpha is "
               "IN_SYNC and targets[0]; the canary must match bravo, the first write")
    finally:
        sync.check_author_identity = _real_check

    # ------------------------------------------------------------------
    # backend#2690. A `pr create` refusal used to `break` the target loop, so a
    # credential that died on repo 1 of 16 left the report covering a PREFIX of
    # the fleet -- while the summary line went on saying `16 targets`. The write
    # must stop (#348: the same credential would push a branch to all of them
    # and open a PR on none); the AUDIT must not, because it is a read that
    # needs no credential. The `author_refusal` path above has always got this
    # right, and `standards-sync.yml` argues for it in prose. This path did the
    # opposite.
    FLEET = ("alpha", "beta", "gamma")

    def _refuse_on_beta(org, repo, *a, **kw):
        # A REAL `AuthorUnusable` OUT OF THE REAL FUNCTION'S CONTRACT, raised
        # mid-fleet rather than on the first repo, so the check distinguishes
        # "kept going" from "never started".
        _refuse_on_beta.seen.append(repo)
        if repo == "beta":
            raise sync.AuthorUnusable("cannot open PR: HTTP 403 from gh")
        return None
    _refuse_on_beta.seen = []

    _real_check = sync.check_author_identity
    try:
        sync.check_author_identity = lambda _t: None
        calls = []
        code, report = _run_main(["--create-prs"], "a-usable-pat", calls,
                                 targets=FLEET, remediate=_refuse_on_beta)

        # THE FINDING ITSELF: every target is classified, not just those before
        # the failure. Asserted per repo rather than by counting rows, so a
        # report that lost `gamma` cannot pass by gaining a footer line.
        missing = [r for r in FLEET if f"| {r} |" not in report]
        record(not missing,
               "main: a mid-fleet PR refusal still audits EVERY target",
               f"missing={missing} code={code} -- `break` here made the report "
               "a prefix of the fleet while the summary said the full count")

        # NO FURTHER WRITES. `beta` is where it died, so `gamma` must never have
        # been attempted -- that is #348's half, and it must survive this fix.
        record(_refuse_on_beta.seen == ["alpha", "beta"],
               "main: the refusal disarms remediation for the REST of the fleet",
               f"remediate saw {_refuse_on_beta.seen} -- `gamma` being attempted "
               "is the half-rollout #348 closed; not reaching `beta` would mean "
               "this case never exercised the raise at all")

        # AND THE REPORT SAYS BOTH HALVES, because "halted" and "audited" are
        # the two things a reader has to be able to tell apart.
        record("REMEDIATION HALTED at `beta`" in report,
               "main: the report names where remediation stopped",
               f"report={report.strip()[-300:]!r}")
        record(f"all {len(FLEET)} targets" in report,
               "main: the report states the audit is complete despite the halt",
               f"report={report.strip()[-300:]!r} -- a count line of "
               f"{len(FLEET)} over a prefix of rows is the overstatement")
        # SCOPED TO THE ROW, not the report (Bugbot, .github#368). The footer
        # this change adds contains the words "NOT REMEDIATED" itself, so a
        # whole-report search was satisfied before any row was consulted --
        # `gamma` could be a bare drifted classification and this still passed.
        # The missing-repo check three lines up already matches `| {r} |` per
        # target; this one had not been given the same treatment.
        post_halt = [ln for ln in report.splitlines() if ln.startswith("| gamma |")]
        record(len(post_halt) == 1 and "NOT REMEDIATED" in post_halt[0],
               "main: repos after the halt are named NOT REMEDIATED",
               f"gamma row={post_halt!r} -- an unexplained drifted row reads as "
               "a plain audit nobody asked to remediate")
        record(code == 2,
               "main: a mid-fleet refusal exits 2, not the --create-prs 0",
               f"code={code} -- 0 would claim every drifted repo has a PR open")

        # NON-VACUITY. The same fleet with a credential that never refuses must
        # remediate all three and be green, or every check above would pass over
        # a script that had simply stopped remediating anything.
        calls = []
        code, report = _run_main(["--create-prs"], "a-usable-pat", calls,
                                 targets=FLEET)
        record(code == 0 and len(calls) == len(FLEET)
               and "REMEDIATION HALTED" not in report,
               "main: the halt is not vacuous -- an unrefused fleet gets three PRs",
               f"code={code} calls={len(calls)}")
    finally:
        sync.check_author_identity = _real_check
finally:
    sys.stderr = _real_stderr

# COMPUTED HERE, NOT EARLIER. This sat above the last hundred lines of checks,
# so anything appended below it PRINTED its FAIL and did not count: the summary
# read `0 failed` while two checks had failed, and the script exited 0. The
# mutation harness is what surfaced it -- a gate mutation came back UNCAUGHT
# because the suite it was measured against could not go red (#348).
failed = [name for ok, name, _ in RESULTS if not ok]
print(f"\n{len(RESULTS)} checks, {len(failed)} failed.")
if failed:
    for name in failed:
        print(f"  FAIL: {name}")
    sys.exit(1)
