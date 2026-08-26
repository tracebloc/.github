#!/usr/bin/env python3
"""Sync the org-standards block into every active repo's CLAUDE.md.

tracebloc/backend#1602. Rules that live only in prose drift: measured
2026-08-06, 5 of 14 repo CLAUDE.mds carried the develop-only rule in five
phrasings, one mandated a retired assignee policy, one named a default branch
that no longer exists, and five active repos had no CLAUDE.md at all. Same fix
shape as repo-inventory.yml / caller-drift.py (backend#1415): one canonical
file, one guard.

MODES
  report (default)  read every target repo's CLAUDE.md (develop-first) and
                    classify it against the canon: IN_SYNC / DRIFTED /
                    MISSING_BLOCK / NO_FILE / MALFORMED / UNREADABLE.
                    Exit 1 unless the whole fleet is IN_SYNC.
  --create-prs      additionally remediate: for each repo not IN_SYNC, push a
                    `docs/<issue>-org-standards-sync` branch updating CLAUDE.md
                    and open (or refresh) a PR against that repo's develop.

DESIGN RULES, inherited from caller-drift.py:

1.  NEVER REPORT ALL-CLEAR FROM A FAILED READ. Every repo read either yields a
    classification or an UNREADABLE record, and one unreadable record exits 2.
    A 403, a rate limit or a truncated response is never folded into "no file".

2.  DEVELOP-FIRST. CLAUDE.md is read from `develop` where that branch exists,
    else from the default branch — the same correction repo-inventory.yml
    encodes as `audit_branch: develop-first`.

3.  ABSENCE IS NEVER IMPLICIT. Every repo in repo-inventory.yml is either a
    sync target or carries a written exemption in EXEMPT below. An exemption
    naming a repo the inventory does not know is itself a failure — stale
    exemptions rot exactly like stale citations.

4.  MALFORMED MARKERS FAIL CLOSED. A file with unpaired or duplicated markers
    is never auto-edited — a bad splice could destroy hand-written repo
    content. It is reported and exits 2 until a human repairs the markers.

5.  FRESH REFS ARE EVENTUALLY CONSISTENT. A file read on a just-created
    branch can transiently 404 even though the base it was cut from has the
    file (seen live: run 31373298821, frontend-app leg — the 404 was
    believed, the write went out sha-less against an existing path, and
    GitHub rejected it). Reads the base proves must succeed are retried
    with backoff; a write rejected over a missing/stale sha refreshes the
    sha and retries exactly once. After the retries: fail closed, never
    silently.

The canon is read from the CHECKOUT (it lives next to this script); target
state is read over the API. A PR that edits org-standards.md therefore sees
the whole fleet as DRIFTED — which is true: the fleet IS behind the proposed
canon until the sync PRs land. The workflow runs the audit on schedule and
dispatch only; PRs run the offline selftest.

Exit codes (the workflow treats everything except 0 as failure):
  0  fleet in sync — or, with --create-prs, every remediation PR opened/refreshed
  1  drift found (report mode)
  2  could not evaluate or could not remediate: unreadable repo, malformed
     markers, bad inventory, bad canon, or a refused write
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time

BEGIN = "<!-- org-standards:begin -->"
END = "<!-- org-standards:end -->"
HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")

IN_SYNC = "IN_SYNC"
DRIFTED = "DRIFTED"
MISSING_BLOCK = "MISSING_BLOCK"
NO_FILE = "NO_FILE"
MALFORMED = "MALFORMED"
UNREADABLE = "UNREADABLE"

# Every repo in repo-inventory.yml must be a target or appear here with a
# written reason (design rule 3). An entry naming an unknown repo fails the run.
# Empty on purpose. devex-bootstrap was the only entry, and its
# archive-vs-harden question (backend#1597 item 3) was answered by archiving the
# repo -- so the exemption went with the inventory entry. Design rule 3 above
# makes a stale exemption a hard failure: `load_targets` refuses an EXEMPT name
# the inventory does not know, which would have taken the whole scheduled audit
# down rather than skipping one repo.
EXEMPT: "dict[str, str]" = {}

# What a repo gets when it has no CLAUDE.md at all: a stub inviting repo-owned
# content, then the managed block. Wave 2 of backend#1602 fills the stub.
STUB = (
    "# CLAUDE.md\n\n"
    "Repo-specific guidance for Claude Code sessions goes here, above the\n"
    "managed org block: build/test commands, architecture notes, gotchas,\n"
    "and this repo's default reviewer. Rollout: tracebloc/backend#1602.\n"
)


# WHO OPENS THE SYNC PRs, AND WHY IT CANNOT BE THE APP (tracebloc/backend#2590).
# Cursor Bugbot keys its review on the PR AUTHOR's Cursor seat. Authenticating
# `gh pr create` with the tracebloc-release-train installation token makes the
# author `tracebloc-release-train[bot]` (type: Bot), which has no seat -- so
# Bugbot never reviews, and `bugbot / review` fails closed on every sync PR.
# Measured 2026-08-26: zero `Cursor Bugbot` check runs across all 14 open sync
# PRs, while every human-authored PR that day got a verdict. Cursor attributes an
# explicit `bugbot run` to the author too, so commenting on the PR cannot rescue
# it -- it answers "Bugbot is not enabled for your user on this team".
#
# So PR CREATION -- and only PR creation -- runs as a human PAT. The fleet reads
# and the branch push keep the App token: `owner:`-scoped, short-lived, and not
# tied to one person's account, which is the whole of backend#2036.
#
# NO FALLBACK TO THE APP TOKEN. An empty PAT is a hard per-repo error, not a
# quiet downgrade, because the downgrade IS the bug: it opens a bot-authored PR
# that looks identical to a working one and that Bugbot silently skips.
# `standards-sync.yml` already refuses a fallback in the other direction for the
# same reason.
AUTHOR_TOKEN_ENV = "SYNC_PR_AUTHOR_TOKEN"

# Reviewer AND assignee on every sync PR. The two are ordinarily distinct roles
# (RFC-BACKEND-0008 D31: the assignee owns landing it, the reviewer owns judging
# it) and they are deliberately collapsed here: nobody "does the work" on a
# machine-generated prose sync, so one person owning both is the honest reading.
#
# It CANNOT be the author. GitHub refuses an approving review from a PR's own
# author, and LukasWodka was the requested reviewer on 4 of the 14 open sync PRs
# (docs#143, release-train#130, .github#344, claude-skills#39) -- authoring as
# LukasWodka without moving those would deadlock them permanently: a required
# review nobody eligible can give.
SYNC_REVIEWER = "saqlainsyed007"


def author_login(token: str) -> "str | None":
    """Who `token` actually is, asked of GitHub rather than assumed.

    THE INVARIANT IS reviewer != AUTHOR, AND ONLY ONE HALF OF IT WAS DERIVED
    (@saqlainsyed007, #348). `SYNC_REVIEWER` is a literal here and the selftest
    pinned the other side as a literal too -- so if `SYNC_PR_AUTHOR_TOKEN` is
    ever re-provisioned to saqlainsyed007's PAT, author == reviewer, every
    `--add-reviewer` 422s, and the backend#2590 deadlock this whole change
    exists to remove comes back **with the tests still green**.

    A credential's owner is a fact about the credential, so it is read from the
    credential. Returns None when the token cannot be resolved at all, which is
    its own refusal rather than a guess.
    """
    code, out, _ = gh("api", "user", "--jq", ".login", token=token)
    login = out.strip()
    return login if code == 0 and login else None


class Unreadable(Exception):
    """A read that produced neither a value nor a clean 404."""


class AuthorUnusable(Exception):
    """`pr create` failed as the PAT -- a FLEET-WIDE fact, not a per-repo one.

    RAISED RATHER THAN RETURNED, because it has to stop the loop (Bugbot,
    #348). `check_author_identity` proves the token EXISTS, RESOLVES, and is
    not the reviewer; none of that proves it can open a PR in this org. A
    token with the wrong fine-grained permissions, or one never SSO-authorized,
    passes every one of those checks and then fails at `pr create` -- by which
    point `remediate` has pushed a branch and a commit to every drifted repo,
    which is exactly the half-rollout that gate was added to prevent.

    Nothing read-only can fully prove "this token can open a PR"; the only
    proof is opening one. So the guarantee is BOUNDED instead of claimed: the
    first failed creation aborts the fleet, leaving at most ONE repo with a
    branch and no PR rather than all of them.
    """


def die(message: str) -> "None":
    sys.stderr.write(f"::error::{message}\n")
    sys.stderr.write("::error::Refusing to report on standards drift from an incomplete read.\n")
    raise SystemExit(2)


def gh(*args: str, token: "str | None" = None) -> "tuple[int, str, str]":
    """Run `gh`. With `token`, run it as THAT identity instead of the ambient one.

    Every call but PR creation wants the App installation token the workflow
    already exports as GH_TOKEN, so `token` defaults to None and nothing about
    the fleet reads changes. `_ensure_pr` is the one caller that passes it --
    see AUTHOR_TOKEN_ENV for why the PR author has to be a human.
    """
    env = None
    if token is not None:
        # BOTH names, because `gh` reads GITHUB_TOKEN too and whichever the
        # workflow happens to export would otherwise win over this argument --
        # silently restoring the App identity this exists to displace.
        env = dict(os.environ, GH_TOKEN=token, GITHUB_TOKEN=token)
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def http_status(stderr: str) -> "int | None":
    match = HTTP_STATUS.search(stderr or "")
    return int(match.group(1)) if match else None


def load_canon(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            canon = handle.read()
    except OSError as exc:
        die(f"cannot read the canon at {path}: {exc}")
    if not canon.strip():
        die(f"the canon at {path} is empty — an empty canon would blank every block")
    if BEGIN in canon or END in canon:
        die(f"the canon at {path} contains a sync marker — stamping it would nest markers")
    return canon


def load_targets(path: str) -> "tuple[str, list[str]]":
    # Lazy import so the offline selftest runs without PyYAML; the workflow
    # installs it explicitly and this hard-fails rather than degrading.
    try:
        import yaml
    except ImportError:
        die("PyYAML is not importable; there is no trustworthy degraded mode")
    try:
        with open(path, encoding="utf-8") as handle:
            inventory = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        die(f"cannot read the inventory at {path}: {exc}")
    if not isinstance(inventory, dict) or "org" not in inventory or "repos" not in inventory:
        die(f"{path} lacks the org/repos keys this guard needs — schema drift, not absence of repos")
    repos = inventory["repos"]
    if not isinstance(repos, dict) or not repos:
        die(f"{path} `repos` is not a non-empty mapping — refusing to treat that as an empty org")
    for name in EXEMPT:
        if name not in repos:
            die(f"EXEMPT names '{name}', which repo-inventory.yml does not know — stale exemption")
    targets = sorted(name for name in repos if name not in EXEMPT)
    return str(inventory["org"]), targets


def render_block(canon: str) -> str:
    return f"{BEGIN}\n{canon.strip(chr(10))}\n{END}"


def classify(text: "str | None", canon: str) -> str:
    if text is None:
        return NO_FILE
    begins, ends = text.count(BEGIN), text.count(END)
    if begins == 0 and ends == 0:
        return MISSING_BLOCK
    if begins != 1 or ends != 1:
        return MALFORMED
    start, stop = text.index(BEGIN), text.index(END)
    if stop < start:
        return MALFORMED
    inner = text[start + len(BEGIN):stop].strip("\n")
    return IN_SYNC if inner == canon.strip("\n") else DRIFTED


def build_desired(text: "str | None", canon: str, state: str) -> str:
    block = render_block(canon)
    if state == NO_FILE:
        return f"{STUB}\n{block}\n"
    assert text is not None
    if state == MISSING_BLOCK:
        return text.rstrip("\n") + f"\n\n{block}\n"
    if state == DRIFTED:
        start = text.index(BEGIN)
        stop = text.index(END) + len(END)
        return text[:start] + block + text[stop:]
    # IN_SYNC needs nothing; MALFORMED must never be spliced (design rule 4).
    raise AssertionError(f"build_desired called for state {state}")


def resolve_branch(org: str, repo: str) -> str:
    # Exact-match ref lookup (git/ref/heads/...), not /branches/{name}: the same
    # endpoint remediate() trusts for the base sha, so existence and base-sha
    # reads can never disagree about what "develop exists" means (Bugbot,
    # .github#170).
    code, out, err = gh("api", f"repos/{org}/{repo}/git/ref/heads/develop", "--jq", ".object.sha")
    if code == 0 and out.strip():
        return "develop"
    if http_status(err) == 404:
        code2, out2, err2 = gh("api", f"repos/{org}/{repo}", "--jq", ".default_branch")
        if code2 == 0 and out2.strip():
            return out2.strip()
        raise Unreadable(f"default-branch lookup failed: {err2.strip() or 'empty response'}")
    raise Unreadable(f"develop lookup failed: {err.strip() or 'empty response'}")


def fetch_claude_md(org: str, repo: str, branch: str) -> "str | None":
    code, out, err = gh("api", f"repos/{org}/{repo}/contents/CLAUDE.md?ref={branch}", "--jq", ".content")
    if code == 0:
        try:
            return base64.b64decode(out).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise Unreadable(f"CLAUDE.md content did not decode: {exc}") from exc
    if http_status(err) == 404:
        return None
    raise Unreadable(f"CLAUDE.md read failed: {err.strip() or 'empty response'}")


def _read_head_file(full: str, head: str, expect_file: bool) -> "tuple[str | None, str | None, str | None]":
    """Read CLAUDE.md on the sync branch. Returns (sha, content, error).

    A ref created an instant ago is eventually consistent: reading a file on
    it can transiently 404 even though the base it was cut from has the file
    (design rule 5; run 31373298821). When the caller KNOWS the base has the
    file (expect_file), a 404 here cannot be true yet — retry with backoff
    and fail CLOSED if it never appears, because believing it produces a
    sha-less write against an existing path. When the base has no file, one
    confirming re-read still guards against trusting a single blip.
    """
    attempts = 5 if expect_file else 2
    delay = 1.0
    last = ""
    for attempt in range(1, attempts + 1):
        code, out, err = gh("api", f"repos/{full}/contents/CLAUDE.md?ref={head}")
        if code == 0:
            try:
                payload = json.loads(out)
                content = base64.b64decode(payload.get("content", "")).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                return None, None, f"CLAUDE.md on {head} did not decode: {exc!r}"
            return payload.get("sha"), content, None
        if http_status(err) == 404:
            if not expect_file and attempt >= 2:
                return None, None, None  # absence confirmed by a re-read
            last = f"404 (attempt {attempt}/{attempts})"
        else:
            last = err.strip() or "empty error"
        if attempt < attempts:
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
    if expect_file:
        return None, None, (
            f"cannot read CLAUDE.md on {head}: the base branch has the file but this "
            f"ref kept answering {last} after {attempts} attempts — refusing a sha-less write"
        )
    return None, None, f"cannot read CLAUDE.md on {head} after {attempts} attempts: {last}"


def _write_head_file(full: str, head: str, desired: str, head_sha: "str | None", issue: int) -> "str | None":
    """PUT CLAUDE.md on the sync branch; refresh the sha and retry once on rejection.

    A 409/422 means the file exists under a different (or unsupplied) sha —
    the write-side face of the consistency window _read_head_file() guards.
    Exactly one refreshed retry: a second rejection is a real conflict and
    fails closed (design rule 5).
    """
    message = (
        f"docs(claude): sync org-standards block (backend#{issue})\n\n"
        "Managed sync from tracebloc/.github/org-standards.md.\n\n"
        "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
    )
    encoded = base64.b64encode(desired.encode("utf-8")).decode("ascii")
    for attempt in (1, 2):
        put_args = ["api", "-X", "PUT", f"repos/{full}/contents/CLAUDE.md",
                    "-f", f"message={message}", "-f", f"content={encoded}", "-f", f"branch={head}"]
        if head_sha:
            put_args += ["-f", f"sha={head_sha}"]
        code, _, err = gh(*put_args)
        if code == 0:
            return None
        if attempt == 1 and http_status(err) in (409, 422):
            # The file provably exists on the ref now — expect_file=True.
            head_sha, _, rerr = _read_head_file(full, head, expect_file=True)
            if rerr:
                return f"write rejected (HTTP {http_status(err)}) and the sha refresh then failed: {rerr}"
            continue
        if attempt == 2 and http_status(err) in (409, 422):
            # A REAL conflict, not the consistency window: we refreshed the sha
            # and were rejected anyway, so something else is writing this branch.
            # This used to fall through to the generic message below, leaving the
            # specific one after the loop UNREACHABLE (Bugbot, #197) -- so the one
            # failure worth distinguishing was the one nobody could see.
            return (f"cannot write CLAUDE.md on {head}: rejected twice "
                    f"(HTTP {http_status(err)}) with a freshly-read sha — "
                    "another writer is racing this branch")
        return f"cannot write CLAUDE.md on {head}: {err.strip()}"
    # Unreachable by construction: every path in the loop returns or continues,
    # and `continue` on attempt 2 is impossible. Kept as a fail-closed backstop
    # rather than falling off the end and returning None, which would read as
    # SUCCESS.
    return f"cannot write CLAUDE.md on {head}: exhausted retries"


def remediate(org: str, repo: str, base: str, desired: str, issue: int,
              file_on_base: bool, author_token: str) -> "str | None":
    """Push the sync branch and open/refresh the PR. Returns an error string or None."""
    head = f"docs/{issue}-org-standards-sync"
    full = f"{org}/{repo}"

    code, out, err = gh("api", f"repos/{full}/git/ref/heads/{base}", "--jq", ".object.sha")
    if code != 0 or not out.strip():
        return f"cannot resolve {base} head: {err.strip()}"
    base_sha = out.strip()

    code, _, err = gh("api", "-X", "POST", f"repos/{full}/git/refs",
                      "-f", f"ref=refs/heads/{head}", "-f", f"sha={base_sha}")
    branch_is_fresh = code == 0
    if code != 0 and http_status(err) != 422:  # 422: branch already exists — reuse it
        return f"cannot create branch {head}: {err.strip()}"

    # expect_file means "a 404 here CANNOT be true", and that holds only when the
    # ref was cut from a base that has the file MOMENTS ago -- the eventual
    # consistency window the retry exists for.
    #
    # A REUSED branch is a different situation. It may have been cut before
    # CLAUDE.md existed on the base, in which case a 404 is honest and permanent.
    # Passing file_on_base alone made _read_head_file retry five times and then
    # fail CLOSED, so the sha-less create could never happen and that repo was
    # stuck forever (Bugbot, #197). The previous code read the 404 as absence and
    # created the file, which was right for this case.
    head_sha, current, rerr = _read_head_file(
        full, head, expect_file=file_on_base and branch_is_fresh
    )
    if rerr:
        return rerr
    if current is not None and current == desired:
        return _ensure_pr(full, head, base, issue, author_token)  # content pushed; ensure the PR

    werr = _write_head_file(full, head, desired, head_sha, issue)
    if werr:
        return werr

    return _ensure_pr(full, head, base, issue, author_token)


def check_author_identity(token: str) -> "str | None":
    """Why the PAT cannot be used, or None. Called BEFORE any repo is written.

    EXTRACTED SO IT CAN BE TESTED (@saqlainsyed007, #348). Inline in `main()`
    this was unreachable from the selftest, and the mutation harness said so
    out loud: "SYNC_REVIEWER becomes the account that authors the PRs" came
    back UNCAUGHT once the old literal-vs-literal check was retired. A guard
    nothing can exercise is the shape this repo keeps removing, so the gate is a
    function and the three refusals are pinned individually.

    ORDER MATTERS. Emptiness first, because an empty token cannot be resolved;
    resolution second, because an unresolvable one cannot be compared; the
    identity comparison last. Each says what it could not establish rather than
    collapsing into one message.
    """
    if not token:
        return (f"{AUTHOR_TOKEN_ENV} is empty. Opening these PRs as the App is "
                "the defect backend#2590 removed -- Bugbot never reviews them. "
                "Refusing before any branch is pushed.")
    login = author_login(token)
    if login is None:
        return (f"{AUTHOR_TOKEN_ENV} is set but GitHub will not say who it "
                "belongs to, so the reviewer-is-not-the-author invariant cannot "
                "be checked. Refusing before any branch is pushed.")
    if login.lower() == SYNC_REVIEWER.lower():
        return (f"{AUTHOR_TOKEN_ENV} belongs to @{login}, who is also "
                f"SYNC_REVIEWER. GitHub refuses a review request on one's own "
                "PR, so every sync PR would open un-reviewable and unmergeable "
                "-- the backend#2590 deadlock, restored. Re-provision the token "
                "or move SYNC_REVIEWER.")
    return None


def _ensure_pr(full: str, head: str, base: str, issue: int,
               author_token: str) -> "str | None":
    # THE AUTHOR IS READ, NOT ASSUMED (Bugbot, #348). The number alone was
    # enough while every open PR on this branch was one this code had just
    # opened. It is not enough now: the sync PRs already open were opened as
    # `tracebloc-release-train[bot]`, and **a PR's author cannot be changed
    # after the fact**. Repairing the roles and returning None reported those
    # repos as ensured while leaving them exactly as unreviewable as before --
    # so the first green `--create-prs` run after this change would have landed
    # the author split on none of the fourteen PRs it was written for.
    # `is_bot` is asked of GitHub rather than pattern-matched off the login,
    # whose shape varies (`app/<slug>` from `pr list`, `<slug>[bot]` elsewhere).
    code, out, err = gh("pr", "list", "-R", full, "--head", head, "--base", base,
                        "--state", "open", "--json", "number,author",
                        "--jq", r'.[0] | select(.) | "\(.number)\t\(.author.login)\t\(.author.is_bot)"')
    if code != 0:
        return f"cannot list PRs: {err.strip()}"
    if out.strip():
        row = out.strip().split("\t")
        if len(row) != 3:
            # CANNOT TELL IS A FINDING (design rule 1). An unparseable row is
            # not evidence that the author is human, and reporting the repo as
            # ensured off one is the same all-clear-from-a-failed-read this
            # script refuses everywhere else.
            return (f"an open PR tracks {head} but its author could not be read "
                    f"from {out.strip()!r}. Refusing to report it as ensured.")
        number, login, is_bot = row
        if is_bot != "false":
            return (f"#{number} tracks {head} but was opened by @{login} "
                    f"(is_bot={is_bot}), and GitHub cannot reassign a PR's "
                    "author. Bugbot keys its review on the author, so this PR "
                    "stays unreviewable however its roles are repaired "
                    f"(backend#2590). Close #{number}; the next run reopens it "
                    f"as {AUTHOR_TOKEN_ENV}'s owner.")
        # AN EXISTING PR STILL NEEDS ITS ROLES CHECKED (@saqlainsyed007, #348).
        # This returned here, so a PR whose `--add-reviewer` failed on an
        # earlier run was never repaired on any later one -- and since the
        # reviewer is what makes it mergeable, it would sit un-mergeable for
        # ever while every subsequent run reported success. Self-healing is the
        # difference between a warning and a permanent state.
        return _assign_roles(full, number, author_token)

    body = (
        "Managed sync of the org-standards block into this repo's `CLAUDE.md` — canonical\n"
        "source: `tracebloc/.github/org-standards.md`. Do not hand-edit the block; to change\n"
        "a rule, open a PR against tracebloc/.github and the sync propagates it.\n\n"
        f"Part of tracebloc/backend#{issue} (org-wide engineering standards).\n\n"
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
    )
    # THE PAT IS VALIDATED IN `main()` NOW, before any branch is pushed
    # (@saqlainsyed007, #348) -- the check used to live here, which is after
    # `remediate()` has already written to every drifted repo. Belt-and-braces
    # only: an empty token reaching this point means the caller skipped the
    # gate, and opening the PR as the App is the defect backend#2590 removed.
    if not author_token:
        return (f"{AUTHOR_TOKEN_ENV} is empty, so this PR could only be opened as the "
                "App, whose PRs Bugbot never reviews (backend#2590). Refusing to open it.")

    code, out, err = gh("pr", "create", "-R", full, "--base", base, "--head", head,
                        # NO TICKET IN THIS TITLE, deliberately. closing-ref-gate.py requires every
                        # ticket a title names to appear in the PR's closingIssuesReferences,
                        # and these PRs must NOT close #1602 -- one sync PR per repo, all
                        # naming the same epic, means the first to merge closes it and the
                        # rest re-close it. The body carries "Part of ..." instead, which is
                        # traceability without a closing link. Pinned by the selftest.
                        "--title", "docs(claude): sync the org-standards block",
                        "--body", body,
                        # As the human, so the PR has an author Bugbot can see.
                        token=author_token)
    if code != 0:
        # FLEET-WIDE BY CONSTRUCTION: the same credential opens every one of
        # these, so a refusal here will refuse the next repo too (Bugbot, #348).
        raise AuthorUnusable(f"cannot open PR: {err.strip()}")

    return _assign_roles(full, out.strip() or head, author_token)


def _assign_roles(full: str, pr_ref: str, author_token: str) -> "str | None":
    """Reviewer and assignee on a sync PR. The reviewer half is FATAL.

    Reviewer AND assignee = SYNC_REVIEWER, never the dispatcher. That used to be
    GITHUB_ACTOR, which was right while a Bot opened the PR and is wrong now: the
    dispatcher IS the author, and GitHub will not take an approving review from a
    PR's own author. See SYNC_REVIEWER.

    ASYMMETRIC ON PURPOSE, and the asymmetry moved (@saqlainsyed007, #348). A
    missing assignee is cosmetic, so it stays a warning. A missing REVIEWER
    means a PR that CANNOT MERGE -- branch protection requires one -- so
    reporting it as a warning let a reviewer-less, un-mergeable PR ship on a
    green run, which is the same class of silent failure this whole change
    exists to kill. It is an error now, and the run reports the repo as a failed
    write.
    """
    code, _, err = gh("pr", "edit", pr_ref, "-R", full, "--add-reviewer",
                      SYNC_REVIEWER, token=author_token)
    if code != 0:
        return (f"opened, but could not request @{SYNC_REVIEWER} as reviewer: "
                f"{err.strip()}. Branch protection needs one, so the PR cannot "
                "merge until it is added by hand.")
    code, _, err = gh("pr", "edit", pr_ref, "-R", full, "--add-assignee",
                      SYNC_REVIEWER, token=author_token)
    if code != 0:
        sys.stderr.write(f"::warning::{full}: could not set @{SYNC_REVIEWER} as "
                         f"assignee (cosmetic): {err.strip()}\n")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", default="org-standards.md")
    parser.add_argument("--inventory", default="repo-inventory.yml")
    parser.add_argument("--issue", type=int, default=1602)
    parser.add_argument("--create-prs", action="store_true")
    parser.add_argument("--repo", action="append", default=[],
                        help="limit to these repos (repeatable); unknown names fail")
    parser.add_argument("--report-file", default="")
    args = parser.parse_args()

    canon = load_canon(args.canonical)
    org, targets = load_targets(args.inventory)
    if args.repo:
        unknown = sorted(set(args.repo) - set(targets))
        if unknown:
            die(f"--repo names {unknown}, which are not sync targets")
        targets = sorted(set(args.repo))

    # BEFORE ANY WRITE, NOT AT THE FIRST PR (@saqlainsyed007, #348). The
    # PAT-empty guard used to live inside `_ensure_pr`, which runs AFTER
    # `remediate()` has already created the branch and pushed CLAUDE.md -- so a
    # missing token produced a fleet-wide half-rollout: branches pushed
    # everywhere, no PRs anywhere, and nothing to review the change through.
    # Fail closed before the first repo is touched.
    author_token = ""
    if args.create_prs:
        author_token = os.environ.get(AUTHOR_TOKEN_ENV, "").strip()
        refusal = check_author_identity(author_token)
        if refusal:
            die(refusal)

    rows: "list[tuple[str, str, str, str]]" = []
    drifted = unreadable = write_errors = 0
    aborted_after = ""

    for repo in targets:
        try:
            branch = resolve_branch(org, repo)
            text = fetch_claude_md(org, repo, branch)
        except Unreadable as exc:
            rows.append((repo, "?", UNREADABLE, str(exc)))
            unreadable += 1
            continue

        state = classify(text, canon)
        action = ""
        if state == MALFORMED:
            unreadable += 1
            action = "unpaired/duplicated markers — repair by hand, never auto-spliced"
        elif state != IN_SYNC:
            drifted += 1
            if args.create_prs:
                try:
                    error = remediate(org, repo, branch,
                                      build_desired(text, canon, state),
                                      args.issue,
                                      file_on_base=(state != NO_FILE),
                                      author_token=author_token)
                except AuthorUnusable as exc:
                    # STOP THE FLEET (Bugbot, #348). The credential is the
                    # same for every repo, so carrying on would push a branch
                    # to all of them and open a PR on none -- the half-rollout
                    # the `main()` gate exists to prevent, arriving through the
                    # one failure mode that gate cannot see: a token that
                    # resolves but cannot create.
                    write_errors += 1
                    aborted_after = repo
                    rows.append((repo, branch, state,
                                 f"REMEDIATION FAILED: {exc} -- ABORTING the "
                                 "remaining repos; this credential cannot open "
                                 "PRs anywhere"))
                    break
                if error:
                    write_errors += 1
                    action = f"REMEDIATION FAILED: {error}"
                else:
                    action = f"sync PR ensured on docs/{args.issue}-org-standards-sync"
        rows.append((repo, branch, state, action))

    for name, reason in sorted(EXEMPT.items()):
        rows.append((name, "-", "EXEMPT", reason.split(";")[0]))

    lines = ["| repo | branch | state | action |", "|---|---|---|---|"]
    lines += [f"| {r} | {b} | {s} | {a} |" for r, b, s, a in rows]
    lines.append("")
    lines.append(f"{len(targets)} targets: {drifted} drifted, {unreadable} unreadable/malformed, "
                 f"{write_errors} failed writes, {len(EXEMPT)} exempt.")
    if aborted_after:
        # SAID IN THE REPORT, not only in the log. A run that stopped early
        # and did not say so reads as a complete sweep of a smaller fleet.
        lines.append("")
        lines.append(f"**ABORTED at `{aborted_after}`** -- `{AUTHOR_TOKEN_ENV}` "
                     "resolves but cannot open PRs, so the remaining targets "
                     "were left untouched rather than given a branch each.")
    report = "\n".join(lines)
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(f"## Org-standards sync\n\n{report}\n")
    if args.report_file:
        with open(args.report_file, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")

    if unreadable or write_errors:
        return 2
    if drifted:
        # With --create-prs every drifted repo now has a sync PR open: the run
        # did what was asked, so it is green; the weekly report stays the drift
        # signal until those PRs merge.
        return 0 if args.create_prs else 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the edge
        # An unhandled crash (malformed API payload, filesystem error, missing
        # gh binary) must read as "could not evaluate" (2), never as drift —
        # Python's default exit code for a traceback is 1, which this guard
        # reserves for confirmed drift (Bugbot, .github#170).
        sys.stderr.write(f"::error::standards-sync crashed before completing: {exc!r}\n")
        raise SystemExit(2)
