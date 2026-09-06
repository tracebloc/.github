#!/usr/bin/env python3
"""Mutation harness for the closing-ref gate (tracebloc/backend#2364).

`closing-ref-gate-selftest.py` asserts the gate's behaviour; this asserts the
SELFTEST. Break a rule in `scripts/closing-ref-gate.py`, watch the suite redden,
restore. A case that stays green under its own rule being deleted is vacuous, and
a green log cannot tell you which of a hundred assertions are load-bearing.

THE MUTATION CALLS THE CODE UNDER TEST (CLAUDE.md rule 9). It edits
`scripts/closing-ref-gate.py` on disk and re-runs the real suite, which imports
that same file by path. There is no second copy of any rule in here -- the
alternative shape, re-implementing the check inline and mutating the copy, is
indistinguishable from real coverage in a log and has bitten this org twice
(e2e-test-agent#114, #115).

EVERY ANCHOR MUST MATCH EXACTLY ONCE. An anchor matching twice mutates an
arbitrary one, so the run reports "uncaught" for the wrong reason; an anchor
matching zero times is stale and fails the run exactly like an uncaught
mutation. That is the assertion that the anchor ACTUALLY APPLIED -- an inert
mutation and good coverage look identical in a log otherwise. `--dry` resolves
every anchor without running the suite, which is what belongs in the fast tier.

  closing-ref-gate-mutations.py          run them all
  closing-ref-gate-mutations.py --dry    resolve anchors only

A MUTATION MUST BREAK THE RULE, NOT THE HARNESS. Several of the guards below
raise `Unreadable` from a path where removing the guard leads to a TypeError or
an AttributeError instead. Those still count as caught, and deliberately so: the
suite's `expect_unreadable` reports "raised TypeError instead of Unreadable" as a
FAILURE rather than letting it escape, so the suite still reports and the
mutation is still detected by a named case. What does NOT count is a mutation
that stops the suite reporting at all -- that is scored UNCAUGHT with the reason
said out loud, because a broken harness is not coverage.

THE BYTECODE CACHE IS DISARMED for the reason bugbot-gate-mutations.py records
at length: a pyc is revalidated on (mtime-to-the-second, byte size), several of
these mutations change the file by the same number of bytes, and back-to-back
runs inside one second would otherwise serve the previous mutation's bytecode --
turning a caught mutation into an uncaught report with nothing in the log saying
so.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "closing-ref-gate.py"
SUITE = ROOT / "scripts" / "tests" / "closing-ref-gate-selftest.py"

# THE BASELINE THIS RUN MEASURES AGAINST MUST BE VERIFIABLE, NOT ASSUMED
# (backend#2441). The `finally` below restores the file on a crash; it cannot
# restore it after SIGKILL, a runner timeout, or a second harness racing this
# one in the same worktree -- and a mutation left on disk becomes the NEXT run's
# `pristine`, which then reports `0 uncaught` about a premise nobody typed.
# See scripts/tests/mutation_baseline.py.
#
# dont_write_bytecode BEFORE the import, deliberately: `selftests-cover` rejects
# anything under scripts/tests/ that is not a suite or a runner, and a
# `__pycache__/` left by this import is exactly that.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402


# (label, old, new)
MUTATIONS = [
    # --- what the TITLE names: precision, and the measured negative case -----
    ("a loose `#N` anywhere in the title counts as a ticket again",
     r'PAREN_BARE_RE = re.compile(r"\(\s*#(\d+)\s*\)")',
     r'PAREN_BARE_RE = re.compile(r"#(\d+)")'),
    ("the type list is hardcoded, so a declared type stops being read",
     r'SCOPE_RE = re.compile(r"^\s*[A-Za-z]+\s*\(([^()]*)\)\s*!?:")',
     r'SCOPE_RE = re.compile(r"^\s*(?:fix|feat)\s*\(([^()]*)\)\s*!?:")'),
    ("a breaking-change `!` hides the scope",
     r'SCOPE_RE = re.compile(r"^\s*[A-Za-z]+\s*\(([^()]*)\)\s*!?:")',
     r'SCOPE_RE = re.compile(r"^\s*[A-Za-z]+\s*\(([^()]*)\)\s*:")'),
    ("a dotted repo name (`.github`) stops parsing",
     r'PAREN_REPO_RE = re.compile(r"\(\s*(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9.][A-Za-z0-9._-]*)#(\d+)\s*\)")',
     r'PAREN_REPO_RE = re.compile(r"\(\s*(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)\s*\)")'),
    ("a `#N` scope stops being a ticket",
     r'SCOPE_BARE_RE = re.compile(r"^#?(\d+)$")',
     r'SCOPE_BARE_RE = re.compile(r"^(\d+)$")'),
    # Anchor updated when the repo class gained a leading dot (.github#314). Left as a
    # separate entry from the leading-dot mutation below: this one makes the `#` optional
    # (a bare word scope becomes a repo+number), that one removes the dot. Different
    # properties, different cases.
    ("a scope that is a word is read as a repo plus a number",
     r'SCOPE_REPO_RE = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9.][A-Za-z0-9._-]*)#(\d+)$")',
     r'SCOPE_REPO_RE = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9.][A-Za-z0-9._-]*)#?(\d+)$")'),
    ("the same ticket named twice becomes two references",
     "    unique = []\n    for ref in refs:",
     "    unique = list(refs)\n    for ref in []:"),
    ("a blank title reads as `names no ticket` instead of a cannot-tell",
     "    if title is None or not str(title).strip():",
     "    if False and (title is None or not str(title).strip()):"),

    # --- the comparison: one function, three verdicts ------------------------
    # A bare number names NO repo. Resolving it locally is the mapping this file
    # is forbidden to hold, and it is what makes client-runtime's `fix(2218)`
    # (backend#2218) look wrong.
    ("a bare number stops being satisfied by another repo",
     "            return LINKED\n        if ref.repo.lower() != name.lower():",
     "            return WRONG_REPO\n        if ref.repo.lower() != name.lower():"),
    ("the wrong-repo verdict collapses into `missing`, losing the trap's name",
     "        number_seen = True",
     "        number_seen = False"),
    ("repo comparison becomes case-sensitive",
     "        if ref.repo.lower() != name.lower():",
     "        if ref.repo != name:"),
    ("an owner mismatch is ignored, so another org's issue satisfies the title",
     "        if ref.owner is not None and ref.owner.lower() != owner.lower():",
     "        if False and ref.owner is not None and ref.owner.lower() != owner.lower():"),
    ("the number no longer has to match at all",
     "        if number != ref.number:",
     "        if False and number != ref.number:"),

    # --- the three reported states stay three --------------------------------
    ("a title naming no ticket is treated as a checked PASS",
     "    if not named:",
     "    if False and not named:"),
    ("the wrong-repo finding is reported as a plain missing link",
     "        elif verdict == WRONG_REPO:",
     "        elif False and verdict == WRONG_REPO:"),

    # --- the one deliberate fail-open, pinned in BOTH directions -------------
    ("every PR is treated as a draft, so the check never fires",
     '    if pr.get("isDraft"):',
     '    if True or pr.get("isDraft"):'),
    ("the draft exemption is removed, so a draft is checked like a ready PR",
     '    if pr.get("isDraft"):',
     '    if False and pr.get("isDraft"):'),

    # --- fail closed: each of these is a "cannot tell" turned quiet ----------
    # THE ONE THAT MATTERS MOST. A link the token cannot read is missing from
    # `nodes` while `totalCount` still counts it -- identical to "not linked".
    ("the truncation/permission-filter test never fires, so a filtered graph reads as unlinked",
     "    if total > len(nodes):",
     "    if False and total > len(nodes):"),
    ("truncation compares against the CAP, refusing an exactly-full page",
     "    if total > len(nodes):",
     "    if total >= PAGE_CAP:"),
    ("a missing totalCount no longer rules truncation out",
     '    if total is None:\n        raise Unreadable(',
     '    if False and total is None:\n        raise Unreadable('),
    ("a non-connection is read as an empty one instead of refused",
     "    if not isinstance(conn, dict):",
     "    if False and not isinstance(conn, dict):"),
    ("a null node in the link graph is accepted",
     "        if not isinstance(node, dict):",
     "        if False and not isinstance(node, dict):"),
    ("a node with no owner/name repository is accepted",
     '        if "/" not in full or not isinstance(number, int):',
     '        if False and ("/" not in full or not isinstance(number, int)):'),
    ("PAGE_CAP stops being derived and is hardcoded wrong",
     r'PAGE_CAP = max([int(n) for n in re.findall(r"first:\s*(\d+)", QUERY)] or [0])',
     "PAGE_CAP = 100"),
    ("the totalCount self-check never reports a blind connection",
     '        if match is None or "totalCount" not in match.group(1):',
     '        if False and (match is None or "totalCount" not in match.group(1)):'),
    ("the guarded-connection list is emptied, so the self-check checks nothing",
     'PAGED_CONNECTIONS = ("closingIssuesReferences",)',
     "PAGED_CONNECTIONS = ()"),
    ("the preflight no longer refuses a query that stopped asking for totalCount",
     "    if blind:",
     "    if False and blind:"),

    # --- the read seam ------------------------------------------------------
    ("a nonzero gh exit is ignored",
     "    if proc.returncode != 0:",
     "    if False and proc.returncode != 0:"),
    ("a GraphQL errors[] payload at exit 0 is accepted",
     '    if payload.get("errors"):',
     '    if False and payload.get("errors"):'),
    ("pullRequest: null is read as an empty PR instead of refused",
     '    if pr is None:\n        raise Unreadable("no such pull request',
     '    if False and pr is None:\n        raise Unreadable("no such pull request'),

    # --- SOFT_FAIL governs FINDINGS, never the check's own integrity ---------
    ("SOFT_FAIL starts softening a cannot-tell",
     "        _emit(FAIL, [], exc)\n        return 2",
     "        _emit(FAIL, [], exc)\n        return 0 if soft else 2"),
    ("SOFT_FAIL is read as any non-empty value",
     '    soft = (os.environ.get("SOFT_FAIL") or "").strip().lower() == "true"',
     '    soft = (os.environ.get("SOFT_FAIL") or "").strip() != ""'),
    ("a finding always exits 0, so nothing is ever reported red",
     "    return 0 if soft else 1",
     "    return 0"),
    ("a malformed REPO/PR_NUMBER is guessed at instead of refused",
     '    if "/" not in repo or not number.isdigit():',
     '    if False and ("/" not in repo or not number.isdigit()):'),
    # --- the remedy must not guess a repo (Bugbot, .github#314) --------------
    # Reverts the FIX, not the prose around it: put a guessed repo back into the
    # remedy a bare title number gets. This is the shape that greened the gate on a
    # wrong link and closed the wrong ticket.
    ("the bare-number remedy names a guessed repo instead of a placeholder",
     '"which repo owns it -- and will accept a link to any repo at that "\n                "number. Add `Closes <owner>/<repo>#%d` for the repo that actually "',
     '"which repo owns it -- and will accept a link to any repo at that "\n                "number. Add `Closes tracebloc/backend#%d` for the repo that actually "'),
    # Reverts the one-character fix: the scope repo class stops admitting a leading
    # dot, so a `.github` scope falls back to being rescued by PAREN_REPO_RE and
    # reports source='parenthetical' again (Asad, .github#314).
    ("the scope repo class stops admitting a leading dot",
     '^(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9.][A-Za-z0-9._-]*)#(\\d+)$',
     '^(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?([A-Za-z0-9][A-Za-z0-9._-]*)#(\\d+)$'),

    # --- THE SECOND ADMISSIBLE FORM (tracebloc/backend#2616) -----------------
    # A new accepted form that nothing breaks is a form nobody notices, so each
    # of these reverts one piece of it and must redden a named case.

    # The whole fix, reverted: stop reading the body at all.
    # Anchor updated when the body scan started stripping HTML comments
    # (design-system-v2#123). Same property, same mutation: read no body.
    ("the body is never read, so a child PR is back to `Closes or nothing`",
     '    mentions = [\n        resolve_repo(ref, inventory)\n'
     '        for ref in parse_body(readable_text(pr.get("body")), keywords)\n    ]',
     "    mentions = []"),
    # The query stops asking for the body. EVERY evaluate case hands the body in
    # directly, so without the query assertion this is invisible -- which is the
    # point of pinning it: the gate would read nothing in production and the
    # suite would stay green.
    ("the query stops asking for the body, so the fix cannot fire live",
     "      isDraft\n      body\n",
     "      isDraft\n"),
    # The widening is undone at the verdict instead of at the read.
    ("a MENTIONED ref is counted as a finding again",
     "    bad = [(ref, verdict) for ref, verdict in results if verdict not in (LINKED, MENTIONED)]",
     "    bad = [(ref, verdict) for ref, verdict in results if verdict != LINKED]"),
    # THE ORDERING. A truthful `Part of tracebloc/backend#304` must not mask a
    # `Closes #304` that closes `.github#304` on merge.
    ("a body reference is consulted BEFORE the wrong-repo trap, so a truthful "
     "mention masks a link that closes the wrong issue",
     "    if number_seen:\n        return WRONG_REPO\n    for mention in mentions:\n"
     "        if same_ticket(ref, mention):\n            return MENTIONED\n    return MISSING",
     "    for mention in mentions:\n        if same_ticket(ref, mention):\n"
     "            return MENTIONED\n    if number_seen:\n        return WRONG_REPO\n    return MISSING"),
    ("a green run stops saying WHICH form satisfied the ref",
     '               else "declared body reference (does not close it)")',
     '               else "closing link")'),

    # --- THE SECOND DIRECTION (tracebloc/design-system-v2#123) ---------------
    # A body claim the graph does not carry. Two halves have to be mutated
    # separately and BOTH matter: the assertion itself, and each of the four
    # narrowing rules that keep it quiet enough to arm. A narrowing rule that
    # nothing breaks is a rule that can be deleted for looking arbitrary --
    # which is exactly how a guard becomes noisy and then gets turned off.

    # The whole direction, reverted: compute it and throw it away. This is the
    # gate as it shipped, and .github#381's measured case must redden.
    ("the inert check never fires, so a body claim the graph does not carry "
     "passes again (the one-way check design-system-v2#123 reported)",
     '    inert = inert_closing_refs(pr.get("body"), links, targets_default_branch(pr, default_branch))',
     "    inert = []"),
    # The verdict stops depending on it while the scan still runs -- the shape
    # the ticket describes: a check that reads the right thing and does not act.
    ("the inert finding is reported but does not fail, so the run stays green",
     "        return FAIL, lines + inert_lines + _why_lines()",
     "        pass"),
    # And the same on the path that has no title reference, which is where
    # .github#381 and design-system-v2#119 both lived.
    ("a PR whose title names nothing is exempt from the inert check, restoring "
     "the exact hole: the assertion is conditional on the title again",
     "        if not inert:\n            return NOTHING_NAMED, [",
     "        if True:\n            return NOTHING_NAMED, ["),
    # The query stops asking for what rule C reads. Rule C fails OPEN, so this
    # disarms the direction silently in production while every synthetic case
    # that hands the fields in directly stays green.
    ("the query stops asking for the base branch, so rule C fails open and the "
     "inert check silently disarms live",
     "      baseRefName\n",
     ""),
    # backend#3240: the default branch travels in the event payload. main() that
    # stops reading it leaves every unit case green (they hand the value in) and
    # rule C declining on every live PR -- only the end-to-end case sees it.
    ("main stops reading PR_BASE_DEFAULT_BRANCH, so rule C declines on every "
     "live PR while the unit cases stay green",
     '    default_branch = os.environ.get("PR_BASE_DEFAULT_BRANCH") or None\n',
     '    default_branch = None\n'),
    # And the query quietly asking GitHub for it again is the exact read that
    # took every private repo down; it must stay out of the query.
    ("the query asks for baseRepository.defaultBranchRef again -- the private-repo "
     "contents read backend#3240 removed",
     "      baseRefName\n",
     "      baseRefName\n      baseRepository { defaultBranchRef { name } }\n"),

    # Rule A -- the graph must be empty. Deleting it makes prose about other
    # tickets a finding on any PR that already links one correctly.
    ("rule A goes, so a PR that DID register a link is reported for prose about "
     "another ticket",
     "    if links:\n        return []",
     "    if False:\n        return []"),
    # Rule C -- both directions, because it is a fail-open.
    ("rule C goes, so every stacked PR and every train promotion with an honest "
     "`Closes` is reported",
     "    if base_default is not True:\n        return []",
     "    if False:\n        return []"),
    ("rule C swallows everything instead, so the direction never fires at all -- "
     "the fail-open nobody can break is the fail-open nobody notices",
     "    if base_default is not True:\n        return []",
     "    if base_default is not None:\n        return []"),
    ("a non-default base reads as the default, so rule C's exemption evaporates",
     "    return base == default",
     "    return True"),
    # Rule B -- line-initial. The rule the 300-PR measurement added.
    ("rule B goes, so a past participle used adjectivally (`the closed "
     "backend#2643`) is a finding again -- 3 of 4 measured false positives",
     "        if LINE_LEAD_RE.match(lead) is None:\n            continue",
     "        if False:\n            continue"),
    ("rule B admits words before the claim, which is the same thing by another "
     "route",
     r'LINE_LEAD_RE = re.compile(r"^[\s>*+\-#`_~]*$")',
     r'LINE_LEAD_RE = re.compile(r"^.*$")'),
    ("rule B stops admitting the backtick, so design-system-v2#119's measured "
     "code-span shape stops being detected",
     r'LINE_LEAD_RE = re.compile(r"^[\s>*+\-#`_~]*$")',
     r'LINE_LEAD_RE = re.compile(r"^[\s>*+\-#_~]*$")'),

    # The keyword scan itself.
    ("the colon form stops being detected, so `Fixes: #47` reads as a working "
     "closing keyword",
     r'    + r")\s*:?\s+"',
     r'    + r")\s+"'),

    # --- the body a scan is allowed to see (readable_text) -------------------
    # HTML comments. Both directions: leaving them in makes the org's own PR
    # template a claim on every PR; and not stripping them at all restores the
    # invisible-satisfaction half of the ticket.
    ("HTML comments stop being stripped, so the PR template's own instruction "
     "comment (`Closes #123`) becomes a claim on every PR that keeps it",
     '    return HTML_COMMENT_RE.sub(STRIPPED, str(body))',
     "    return str(body)"),
    ("a stripped comment collapses to whitespace, so a keyword before it splices "
     "onto a `#N` after it and the fix manufactures a reference nobody wrote",
     'STRIPPED = "\N{REPLACEMENT CHARACTER}"',
     'STRIPPED = " "'),
    ("the comment pattern stops spanning newlines, so a multi-line template "
     "comment is only half removed",
     'HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)',
     'HTML_COMMENT_RE = re.compile(r"<!--.*?-->")'),

    # --- the derived vocabulary (rule 1) ------------------------------------
    ("the non-closing vocabulary is hardcoded instead of parsed from the canon",
     "    keywords = declared_reference_keywords(text)",
     '    keywords = ["Part of"]'),
    ("a multi-word declared keyword (`Part of`) stops being parsed",
     r'    r"^([A-Za-z][A-Za-z]*(?:\s+[A-Za-z]+)*?)\s+[^\s#]+#(?:\d+|N)$"',
     r'    r"^([A-Za-z][A-Za-z]*)\s+[^\s#]+#(?:\d+|N)$"'),
    ("GitHub's closing keywords stop being subtracted, so `Closes` reads as a "
     "non-closing form",
     '        if keyword.split()[0].lower() in GITHUB_CLOSING_KEYWORDS:',
     '        if False and keyword.split()[0].lower() in GITHUB_CLOSING_KEYWORDS:'),
    ("an empty derived vocabulary reverts to closing-only instead of refusing",
     "    if not keywords:",
     "    if False and not keywords:"),
    ("an unreadable canon is folded into `declares nothing`, losing the refusal's name",
     '    except OSError as exc:\n        raise Unreadable(\n'
     '            "%s could not be read (%s), so the org\'s declared non-closing "',
     '    except OSError as exc:\n        text = ""\n        _ = Unreadable(\n'
     '            "%s could not be read (%s), so the org\'s declared non-closing "'),

    # --- what counts as a body reference ------------------------------------
    ("a loose `#N` in the body counts, so any prose mention satisfies the title",
     '        pattern = re.compile(r"(?<![A-Za-z0-9])" + spaced + r"\\s+" + BODY_TAIL_RE, re.IGNORECASE)',
     "        pattern = re.compile(BODY_TAIL_RE, re.IGNORECASE)"),
    ("the keyword no longer has to stand alone, so `Apart of` counts",
     '        pattern = re.compile(r"(?<![A-Za-z0-9])" + spaced + r"\\s+" + BODY_TAIL_RE, re.IGNORECASE)',
     '        pattern = re.compile(spaced + r"\\s+" + BODY_TAIL_RE, re.IGNORECASE)'),

    # --- same_ticket: the body-side comparison ------------------------------
    ("a body reference to a different NUMBER satisfies the title",
     "    if ref.number != other.number:",
     "    if False and ref.number != other.number:"),
    ("a body reference in a different REPO satisfies a repo-named title ref",
     "    if ref.repo.lower() != other.repo.lower():",
     "    if False and ref.repo.lower() != other.repo.lower():"),
    # --- the shorthand map is DERIVED from repo-inventory.yml (.github#416) ---
    ("shorthand resolution goes, so `engine#898` is repo `engine` again and a "
     "correctly linked tracebloc-engine PR is red",
     '    candidates = [name for name in names if name.lower() == full]',
     '    candidates = []'),
    ("an unknown short repo name is no longer a finding: the gate guesses a repo "
     "that does not exist, silently",
     "    if inventory is not None and not is_known_repo(ref, inventory):\n        return UNKNOWN_REPO",
     "    if False:\n        return UNKNOWN_REPO"),
    ("the repo list is hardcoded instead of parsed from the inventory",
     "    org, names = declared_repos(text)",
     '    org, names = ("tracebloc", ["backend", ".github"])'),
    ("an inventory declaring no repositories no longer refuses, so every short "
     "name in every title reads as unknown",
     "    if len(names) == 0:",
     "    if False:"),
    ("a missing `org:` no longer refuses",
     "    if org is None:\n        raise Unreadable(",
     '    org = org or "tracebloc"\n    if False:\n        raise Unreadable('),
    ("another org's repo is resolved against OUR inventory, so `otherorg/engine#5` "
     "becomes a finding about this org",
     "    if ref.owner is not None and ref.owner.lower() != org.lower():\n        return ref\n    short",
     "    if False:\n        return ref\n    short"),
    ("the unknown-repo verdict is reported as a plain missing link, so the remedy "
     "names a repo that does not exist",
     "        if verdict == UNKNOWN_REPO:",
     "        if False:"),
    ("nested inventory keys are read as repos, so an entry's properties become "
     "repo names",
     'INVENTORY_REPO_KEY_RE = re.compile(r"^  ([A-Za-z0-9.][A-Za-z0-9._-]*):(?:\\s.*)?$")',
     'INVENTORY_REPO_KEY_RE = re.compile(r"^ +([A-Za-z0-9.][A-Za-z0-9._-]*):(?:\\s.*)?$")'),
    ("the `repos:` block no longer ends at the next column-0 key, so a later "
     "block's members are read as repos",
     '            if not line.startswith(" "):\n                break',
     '            if not line.startswith(" "):\n                continue'),
    ("the inventory is read on the ticket-less path again, so a docs/ci/chore PR "
     "becomes a cannot-tell when the inventory it never needed is unreadable",
     "    if not named:\n        if not inert:",
     "    if not named and known_repos(root=standards_root):\n        if not inert:"),
    ("the remedy hardcodes the org again, so `otherorg/engine#5` is told to "
     "close `tracebloc/engine#5`",
     '    return "%s/%s#%d" % (ref.owner or org, ref.repo, ref.number)',
     '    return "%s/%s#%d" % (org, ref.repo, ref.number)'),
]

# MUTATIONS IN THE WORKFLOW FILES, not the checker (tracebloc/backend#2556).
#
# Two of this gate's guarantees are declared in YAML rather than in Python: the
# caller listening for `edited`, and `set-status` writing only while the PR is
# open. Rule 5 does not exempt a guard for living in a different language -- and
# these two are the pair most likely to be "tidied" by someone shortening a
# types: list. So the harness mutates those files too and requires the same
# suite to redden.
WORKFLOW_MUTATIONS = [
    (".github/workflows/set-pr-status-caller.yml",
     "the caller stops listening for `edited`, restoring the retitle bypass",
     "    types: [opened, reopened, ready_for_review, converted_to_draft, edited]",
     "    types: [opened, reopened, ready_for_review, converted_to_draft]"),
    (".github/workflows/set-pr-status.yml",
     "set-status loses its open-state guard, so editing a merged PR demotes its card",
     "  set-status:\n    if: ${{ github.event.pull_request.state == 'open' }}\n",
     "  set-status:\n"),
    # backend#3240: the default branch reaches the checker through the payload.
    # A workflow that stops passing it disarms rule C live with every unit case
    # green; a mint that grows contents:read again widens an org-scoped token on
    # every repo for a value the event already carries.
    (".github/workflows/set-pr-status.yml",
     "the workflow stops handing PR_BASE_DEFAULT_BRANCH to the checker",
     "          PR_BASE_DEFAULT_BRANCH: ${{ github.event.pull_request.base.repo.default_branch }}\n",
     ""),
    (".github/workflows/set-pr-status.yml",
     "the closing-ref mint grows a contents scope again",
     "          permission-issues: read\n          # NO `contents` scope",
     "          permission-issues: read\n          permission-contents: read\n          # NO `contents` scope"),
    (".github/workflows/set-pr-status.yml",
     "closing-ref loses its open-state guard, so a merged PR gets a late red X",
     "    if: ${{ inputs.closing-ref-check && github.event.pull_request.state == 'open' }}",
     "    if: ${{ inputs.closing-ref-check }}"),
    # backend#2731. The card fallback is the fix for a check that went red on a
    # board it had already made correct; deleting it restores that, and softening
    # the refusal restores the ORIGINAL backend#2037 defect (a green check over a
    # card left at No Status). Both are one-line edits to YAML, which is exactly
    # what the note above says rule 5 does not exempt.
    (".github/workflows/set-pr-status.yml",
     "set-status stops adding the missing card, so it loses the race again",
     "addProjectV2ItemById(input: {projectId: $p, contentId: $c}) { item { id } }",
     "clientMutationId"),
    (".github/workflows/set-pr-status.yml",
     "a card that could not be added reports success instead of failing closed",
     '"added to it. The board write cannot proceed; this is not a race."\n            exit 1',
     '"added to it. The board write cannot proceed; this is not a race."\n            exit 0'),
]


def _drop_bytecode_cache():
    """Remove any cached bytecode for the gate. See the header: a stale pyc makes
    a caught mutation report as uncaught."""
    try:
        cached = importlib.util.cache_from_source(str(GATE))
    except (ValueError, NotImplementedError):
        return
    try:
        os.unlink(cached)
    except OSError:
        pass


def apply_one(src, old, new):
    n = src.count(old)
    if n != 1:
        raise LookupError("anchor matched %d times, expected exactly 1: %r" % (n, old[:80]))
    out = src.replace(old, new, 1)
    return None if out == src else out


def main():
    dry = "--dry" in sys.argv

    # EVERY FILE THIS RUN REWRITES, in one list. The baseline guard and the
    # left-mutated check below both read it, so a target added to
    # WORKFLOW_MUTATIONS cannot be left out of either.
    targets = [GATE] + [ROOT / rel for rel, _, _, _ in WORKFLOW_MUTATIONS]
    unique_targets = []
    for path in targets:
        if path not in unique_targets:
            unique_targets.append(path)

    # Refuse rather than measure against a baseline nothing vouches for. Only the
    # writing path: `--dry` writes nothing, so it has no restore to lose -- and it
    # is what `make check` runs on every push, where refusing on an uncommitted
    # edit would block the pre-push tier for whoever is editing the target.
    if not dry:
        rc = mutation_baseline.guard(ROOT, unique_targets)
        if rc:
            return rc

    pristine_of = {path: path.read_text(encoding="utf-8") for path in unique_targets}
    stale, uncaught = [], []

    # (target, label, old, new) for every mutation, the gate's implicitly first.
    plan = [(GATE, label, old, new) for label, old, new in MUTATIONS]
    plan += [(ROOT / rel, label, old, new) for rel, label, old, new in WORKFLOW_MUTATIONS]

    for target, label, old, new in plan:
        pristine = pristine_of[target]
        try:
            mutated = apply_one(pristine, old, new)
        except LookupError as exc:
            stale.append((label, str(exc)))
            continue
        if mutated is None:
            stale.append((label, "NO-OP: the mutation changed nothing"))
            continue
        if dry:
            print("  anchor ok  %s" % label)
            continue
        target.write_text(mutated, encoding="utf-8")
        _drop_bytecode_cache()
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            run = subprocess.run(
                [sys.executable, "-B", str(SUITE)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
        finally:
            # ALWAYS restore, including on a crash. A mutation left on disk makes
            # every later run measure the wrong file, and the tell is a suite
            # that reddens for reasons nobody typed.
            target.write_text(pristine, encoding="utf-8")
            _drop_bytecode_cache()
        caught = [
            line.strip()[6:].strip()
            for line in run.stdout.splitlines()
            if line.strip().startswith("FAIL:")
        ]
        # A crash counts as caught ONLY if the suite actually ran and reported; a
        # bare traceback with no assertion output means the mutation broke the
        # harness rather than being detected by a case, which is not coverage.
        reported = "closing-ref-gate-selftest:" in run.stdout
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (label, ", ".join(caught)[:120]))
        elif not reported:
            uncaught.append((label, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % label)
        else:
            uncaught.append((label, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % label)

    for path in unique_targets:
        if path.read_text(encoding="utf-8") != pristine_of[path]:
            sys.stderr.write("::error::%s was left mutated. Restore it from git.\n" % path.name)
            return 2

    print("\n%d mutation(s): %d stale, %d uncaught" % (len(plan), len(stale), len(uncaught)))
    for label, why in stale:
        sys.stderr.write("::error::STALE mutation `%s`: %s\n" % (label, why))
    for label, why in uncaught:
        sys.stderr.write(
            "::error::UNCAUGHT `%s`: %s. Add a case that fails under it, or delete "
            "the mutation and say why it is not worth pinning.\n" % (label, why)
        )
    return 1 if (stale or uncaught) else 0


if __name__ == "__main__":
    raise SystemExit(main())
