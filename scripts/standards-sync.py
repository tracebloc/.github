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
EXEMPT: "dict[str, str]" = {
    "devex-bootstrap": (
        "archive-vs-harden undecided (backend#1597 item 3); stamping a managed "
        "block into a repo that may be archived is deferred until Lukas's call."
    ),
}

# What a repo gets when it has no CLAUDE.md at all: a stub inviting repo-owned
# content, then the managed block. Wave 2 of backend#1602 fills the stub.
STUB = (
    "# CLAUDE.md\n\n"
    "Repo-specific guidance for Claude Code sessions goes here, above the\n"
    "managed org block: build/test commands, architecture notes, gotchas,\n"
    "and this repo's default reviewer. Rollout: tracebloc/backend#1602.\n"
)


class Unreadable(Exception):
    """A read that produced neither a value nor a clean 404."""


def die(message: str) -> "None":
    sys.stderr.write(f"::error::{message}\n")
    sys.stderr.write("::error::Refusing to report on standards drift from an incomplete read.\n")
    raise SystemExit(2)


def gh(*args: str) -> "tuple[int, str, str]":
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
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


def remediate(org: str, repo: str, base: str, desired: str, issue: int) -> "str | None":
    """Push the sync branch and open/refresh the PR. Returns an error string or None."""
    head = f"docs/{issue}-org-standards-sync"
    full = f"{org}/{repo}"

    code, out, err = gh("api", f"repos/{full}/git/ref/heads/{base}", "--jq", ".object.sha")
    if code != 0 or not out.strip():
        return f"cannot resolve {base} head: {err.strip()}"
    base_sha = out.strip()

    code, _, err = gh("api", "-X", "POST", f"repos/{full}/git/refs",
                      "-f", f"ref=refs/heads/{head}", "-f", f"sha={base_sha}")
    if code != 0 and http_status(err) != 422:  # 422: branch already exists — reuse it
        return f"cannot create branch {head}: {err.strip()}"

    head_sha = None
    code, out, err = gh("api", f"repos/{full}/contents/CLAUDE.md?ref={head}")
    if code == 0:
        payload = json.loads(out)
        head_sha = payload.get("sha")
        current = base64.b64decode(payload.get("content", "")).decode("utf-8")
        if current == desired:
            return _ensure_pr(full, head, base, issue)  # content already pushed; just ensure the PR
    elif http_status(err) != 404:
        return f"cannot read CLAUDE.md on {head}: {err.strip()}"

    message = (
        f"docs(claude): sync org-standards block (backend#{issue})\n\n"
        "Managed sync from tracebloc/.github/org-standards.md.\n\n"
        "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
    )
    encoded = base64.b64encode(desired.encode("utf-8")).decode("ascii")
    put_args = ["api", "-X", "PUT", f"repos/{full}/contents/CLAUDE.md",
                "-f", f"message={message}", "-f", f"content={encoded}", "-f", f"branch={head}"]
    if head_sha:
        put_args += ["-f", f"sha={head_sha}"]
    code, _, err = gh(*put_args)
    if code != 0:
        return f"cannot write CLAUDE.md on {head}: {err.strip()}"

    return _ensure_pr(full, head, base, issue)


def _ensure_pr(full: str, head: str, base: str, issue: int) -> "str | None":
    code, out, err = gh("pr", "list", "-R", full, "--head", head, "--base", base,
                        "--state", "open", "--json", "number", "--jq", ".[0].number // empty")
    if code != 0:
        return f"cannot list PRs: {err.strip()}"
    if out.strip():
        return None  # open PR already tracks the branch; the push above refreshed it

    body = (
        "Managed sync of the org-standards block into this repo's `CLAUDE.md` — canonical\n"
        "source: `tracebloc/.github/org-standards.md`. Do not hand-edit the block; to change\n"
        "a rule, open a PR against tracebloc/.github and the sync propagates it.\n\n"
        f"Part of tracebloc/backend#{issue} (org-wide engineering standards).\n\n"
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
    )
    code, out, err = gh("pr", "create", "-R", full, "--base", base, "--head", head,
                        "--title", f"docs(claude): sync org-standards block (backend#{issue})",
                        "--body", body)
    if code != 0:
        return f"cannot open PR: {err.strip()}"

    # Assignee = whoever dispatched the sync (D31: the person doing the work).
    # Non-fatal: a missing assignee is visible on the PR and cheap to add by hand.
    actor = os.environ.get("GITHUB_ACTOR", "").strip()
    if actor:
        code, _, err = gh("pr", "edit", out.strip() or head, "-R", full, "--add-assignee", actor)
        if code != 0:
            sys.stderr.write(f"::warning::{full}: could not assign @{actor}: {err.strip()}\n")
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

    rows: "list[tuple[str, str, str, str]]" = []
    drifted = unreadable = write_errors = 0

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
                error = remediate(org, repo, branch, build_desired(text, canon, state), args.issue)
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
    raise SystemExit(main())
