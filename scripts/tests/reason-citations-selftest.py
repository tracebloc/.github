#!/usr/bin/env python3
"""Suite for scripts/reason-citations.py (tracebloc/backend#2449).

The check says "a citation inside a repo-inventory reason must still be live".
Everything below drives it against FIXTURE inventories and a STUBBED `gh`,
because a suite that named the real inventory would redden every time a reason is
rewritten or a ticket is closed -- and the burn-down is the point.

HERMETIC, WITH THE REAL SEAM EXERCISED. There is no network and no token: a
throwaway `gh` executable is put first on PATH, so the subprocess call, the
exit-code path and the JSON decoding are all covered by the same cases rather
than being the part nobody tests.

INPUTS ARE WRITTEN DOWN INDEPENDENTLY OF THE MATCHER (CLAUDE.md rule 9's
corollary). The citation strings, the typenames and the states below are
LITERALS. Iterating the module's own `LIVE`/`DEAD` sets to check the module would
be self-consistent and therefore blind -- typo one and the fixture carries the
same typo and still passes.

Each case pins a behaviour a mutation would break. `reason-citations-mutations.py`
breaks each one and asserts this suite reddens; a case that survives its own
mutation is vacuous and worse than absent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE.parent / "reason-citations.py"

RESULTS = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        {detail}")


# --- the stub `gh` ---------------------------------------------------------
# Answers the ONE GraphQL document the guard sends, from a table the case hands
# it. Modes cover the failure shapes a real `gh` produces: a nonzero exit with a
# message, a 200 whose body is not JSON, and GitHub's own partial answer -- a
# null node plus an `errors[]` entry, at exit 0, alongside good data.
STUB = r'''#!/usr/bin/env python3
import json, os, re, sys

mode = os.environ.get("STUB_MODE", "ok")
if mode == "exit":
    sys.stderr.write("gh: HTTP 401 Bad credentials\n")
    sys.exit(1)
if mode == "garbage":
    sys.stdout.write("<html>rate limited</html>")
    sys.exit(0)
if mode == "nodata":
    print(json.dumps({"errors": [{"message": "boom"}]}))
    sys.exit(1)

query = ""
for arg in sys.argv:
    if arg.startswith("query="):
        query = arg[len("query="):]
states = json.loads(os.environ.get("STUB_STATES", "{}"))
data = {}
errors = []
pattern = r'(c\d+): repository\(owner: "([^"]*)", name: "([^"]*)"\) \{ issueOrPullRequest\(number: (\d+)\)'
for alias, owner, name, number in re.findall(pattern, query):
    key = "%s/%s#%s" % (owner, name, number)
    hit = states.get(key)
    if hit is None:
        data[alias] = {"issueOrPullRequest": None}
        errors.append({"type": "NOT_FOUND", "path": [alias]})
        continue
    data[alias] = {"issueOrPullRequest": {"__typename": hit[0], "state": hit[1]}}
out = {"data": data}
if errors:
    out["errors"] = errors
print(json.dumps(out))
'''


def _stub_dir() -> str:
    d = tempfile.mkdtemp()
    gh = Path(d, "gh")
    gh.write_text(STUB, encoding="utf-8")
    gh.chmod(0o755)
    return d


STUB_DIR = _stub_dir()


def run(inventory: str, *, states: "dict | None" = None, exempt: "str | None" = "",
        mode: str = "ok", extra_env: "dict | None" = None, write: bool = True):
    """Write a fixture inventory, run the guard over it, return (rc, out, err).

    PASS `exempt` ON EVERY CASE, including the default `""`. Letting the LIVE
    exemption map apply would mean every real row reads as stale against a
    fixture -- so the suite would redden on production state rather than on the
    case under test. `mint-scope-selftest.py` learned that the hard way.
    """
    d = tempfile.mkdtemp()
    path = Path(d, "repo-inventory.yml")
    if write:
        path.write_text(inventory, encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = STUB_DIR + os.pathsep + env.get("PATH", "")
    env["REASON_CITATIONS_INVENTORY"] = str(path)
    env["STUB_STATES"] = json.dumps(states or {})
    env["STUB_MODE"] = mode
    env.pop("GITHUB_REPOSITORY", None)
    if exempt is not None:
        env["REASON_CITATIONS_EXEMPT"] = exempt
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, str(GUARD)], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def inv(reason: str, *, org: str = "tracebloc", source_repo: str = ".github",
        head: str = "", second: str = "") -> str:
    """One inventory carrying exactly one written reason.

    `second` adds a SECOND reason, under its own caller, so a case can author an
    inventory that cites the same issue a known number of times -- which the
    pinned-count cases need and could not otherwise express.
    """
    extra = f"""      other.yml:
        exempt: >-
          {second}
""" if second else ""
    return f"""org: {org}
source_repo: {source_repo}
{head}repos:
  demo:
    callers:
      thing.yml:
        exempt: >-
          {reason}
{extra}"""


OPEN_ISSUE = ["Issue", "OPEN"]
CLOSED_ISSUE = ["Issue", "CLOSED"]
OPEN_PR = ["PullRequest", "OPEN"]
MERGED_PR = ["PullRequest", "MERGED"]
CLOSED_PR = ["PullRequest", "CLOSED"]

# --- the finding this check exists for ------------------------------------
rc, out, err = run(inv("sequenced behind backend#1408, which is still being worked"),
                   states={"tracebloc/backend#1408": CLOSED_ISSUE})
record(rc == 1 and "tracebloc/backend#1408" in err and "is CLOSED" in err,
       "a citation to a CLOSED issue is a finding",
       f"rc={rc} err={err.strip()[:130]!r}")

rc, out, err = run(inv("sequenced behind backend#1408, which is still being worked"),
                   states={"tracebloc/backend#1408": OPEN_ISSUE})
record(rc == 0 and not err.strip(),
       "a citation to an OPEN issue is clean",
       f"rc={rc} out={out.strip()[:120]!r}")

# The report must name WHERE the reason lives, or the remedy is a grep.
rc, out, err = run(inv("blocked on backend#1408"),
                   states={"tracebloc/backend#1408": CLOSED_ISSUE})
record("repos.demo.callers.thing.yml.exempt" in err,
       "the finding names the inventory path of the reason that cites it",
       f"err={err.strip()[-160:]!r}")

# --- pull requests: merged is not staleness, abandoned is -------------------
rc, out, err = run(inv("remediated under model-zoo#115"),
                   states={"tracebloc/model-zoo#115": MERGED_PR})
record(rc == 0,
       "a MERGED pull request is not a finding (its terminal state is success)",
       f"rc={rc} err={err.strip()[:120]!r}")

rc, out, err = run(inv("goes in with model-zoo#115"),
                   states={"tracebloc/model-zoo#115": OPEN_PR})
record(rc == 0, "an OPEN pull request is not a finding", f"rc={rc}")

rc, out, err = run(inv("goes in with model-zoo#115"),
                   states={"tracebloc/model-zoo#115": CLOSED_PR})
record(rc == 1 and "never landed" in err,
       "a pull request CLOSED WITHOUT MERGING is a finding",
       f"rc={rc} err={err.strip()[:130]!r}")

# --- which repo a citation means -------------------------------------------
# A BARE `#N` RESOLVES AGAINST THE REPO THE INVENTORY LIVES IN, never `backend`.
# Assuming `backend` is a defect this org shipped and fixed (.github#314), so the
# stub is given ONLY the `.github` answer: a guard that guessed `backend` would
# get a null node and exit 2 instead of the clean 0 asserted here.
rc, out, err = run(inv("raised in the staging baseline (Bugbot, #277)"),
                   states={"tracebloc/.github#277": MERGED_PR})
record(rc == 0,
       "a bare `#N` resolves against `source_repo`, not `backend`",
       f"rc={rc} err={err.strip()[:130]!r}")

rc, out, err = run(inv("raised in the staging baseline (Bugbot, #277)",
                       source_repo="rfcs"),
                   states={"tracebloc/rfcs#277": MERGED_PR})
record(rc == 0,
       "the bare-`#N` repo is READ from `source_repo`, not typed into the guard",
       f"rc={rc} err={err.strip()[:130]!r}")

# `repo#N` takes its owner from the inventory's own `org:`.
rc, out, err = run(inv("see model-zoo#115", org="someorg"),
                   states={"someorg/model-zoo#115": MERGED_PR})
record(rc == 0,
       "`repo#N` takes its owner from the inventory's `org:`",
       f"rc={rc} err={err.strip()[:130]!r}")

rc, out, err = run(inv("see tracebloc/backend#1408", org="someorg"),
                   states={"tracebloc/backend#1408": OPEN_ISSUE})
record(rc == 0,
       "`owner/repo#N` is taken as written, overriding `org:`",
       f"rc={rc} err={err.strip()[:130]!r}")

# A repo name starting with a dot is real in this org and must not be dropped.
rc, out, err = run(inv("(Bugbot, .github#196)"),
                   states={"tracebloc/.github#196": CLOSED_PR})
record(rc == 1 and "tracebloc/.github#196" in err,
       "a leading-dot repo name (`.github#N`) is parsed, not skipped",
       f"rc={rc} err={err.strip()[:130]!r}")

# The bare rule's PREMISE is that this file lives in <org>/<source_repo>. When the
# runner says otherwise, the answer is "cannot tell" -- not a guess.
rc, out, err = run(inv("raised in the staging baseline (Bugbot, #277)"),
                   states={"tracebloc/.github#277": MERGED_PR},
                   extra_env={"GITHUB_REPOSITORY": "tracebloc/backend"})
record(rc == 2 and "bare citation" in err,
       "a bare `#N` is refused when GITHUB_REPOSITORY contradicts `source_repo`",
       f"rc={rc} err={err.strip()[:150]!r}")

# --- malformed ---------------------------------------------------------------
rc, out, err = run(inv("see tracebloc/backend/extra#12"),
                   states={"tracebloc/backend#12": OPEN_ISSUE})
record(rc == 1 and "not a legal" in err and "extra#12" in err,
       "a citation whose prefix is not a legal owner/repo is reported, not dropped",
       f"rc={rc} err={err.strip()[:150]!r}")

rc, out, err = run(inv("see backend#0"), states={})
record(rc == 1 and "not a legal" in err,
       "`#0` is malformed: GitHub numbers issues from 1",
       f"rc={rc} err={err.strip()[:150]!r}")

# --- fail closed --------------------------------------------------------------
# The premise is that reasons cite tickets. Finding none means the matcher broke.
rc, out, err = run(inv("no citation anywhere in this sentence"), states={})
record(rc == 2 and "no ticket citation found" in err,
       "ZERO citations found is a hard error, not a clean run",
       f"rc={rc} err={err.strip()[:150]!r}")

rc, out, err = run("", write=False)
record(rc == 2 and "could not be read" in err,
       "an inventory that does not exist is a hard error",
       f"rc={rc} err={err.strip()[:130]!r}")

rc, out, err = run("repos: [this is: not: valid\n")
record(rc == 2 and "could not be parsed" in err,
       "an unparseable inventory is a hard error, not a skip",
       f"rc={rc} err={err.strip()[:130]!r}")

rc, out, err = run("- just\n- a list\n")
record(rc == 2 and "did not parse to a mapping" in err,
       "an inventory that is not a mapping is a hard error",
       f"rc={rc} err={err.strip()[:130]!r}")

# CANNOT TELL, in every shape a real `gh` produces it.
rc, out, err = run(inv("blocked on backend#1408"), states={})
record(rc == 2 and "could not be read" in err and "cannot tell" in err,
       "a citation the API will not resolve is CANNOT TELL, never 'still open'",
       f"rc={rc} err={err.strip()[:170]!r}")

rc, out, err = run(inv("blocked on backend#1408"),
                   states={"tracebloc/backend#1408": OPEN_ISSUE}, mode="exit")
record(rc == 2, "a failing `gh` call is a hard error", f"rc={rc} err={err.strip()[:130]!r}")

rc, out, err = run(inv("blocked on backend#1408"),
                   states={"tracebloc/backend#1408": OPEN_ISSUE}, mode="garbage")
record(rc == 2 and "no JSON" in err,
       "a non-JSON GraphQL response is a hard error",
       f"rc={rc} err={err.strip()[:130]!r}")

rc, out, err = run(inv("blocked on backend#1408"),
                   states={"tracebloc/backend#1408": OPEN_ISSUE}, mode="nodata")
record(rc == 2 and "carried no data" in err,
       "a GraphQL payload with errors and no data is a hard error",
       f"rc={rc} err={err.strip()[:130]!r}")

# A PARTIAL read is not a clean read: one good node, one null, exit 0.
rc, out, err = run(inv("blocked on backend#1408 and on backend#9999"),
                   states={"tracebloc/backend#1408": OPEN_ISSUE})
record(rc == 2 and "backend#9999" in err,
       "one unresolvable citation among good ones still refuses the whole run",
       f"rc={rc} err={err.strip()[:150]!r}")

rc, out, err = run(inv("blocked on backend#1408"),
                   states={"tracebloc/backend#1408": ["Issue", "TRIAGED"]})
record(rc == 2 and "unrecognised citation state" in err,
       "a state the check has no verdict for is refused, not passed",
       f"rc={rc} err={err.strip()[:150]!r}")

rc, out, err = run("source_repo: .github\nrepos:\n  demo:\n    callers:\n"
                   "      t.yml:\n        exempt: see backend#1408\n")
record(rc == 2 and "no `org:`" in err,
       "an inventory with no `org:` is a hard error",
       f"rc={rc} err={err.strip()[:130]!r}")

rc, out, err = run("org: tracebloc\nrepos:\n  demo:\n    callers:\n"
                   "      t.yml:\n        exempt: see #277\n")
record(rc == 2 and "no `source_repo:`" in err,
       "a bare `#N` with no `source_repo:` is a hard error, not a guess",
       f"rc={rc} err={err.strip()[:150]!r}")

# --- where reasons live -------------------------------------------------------
# A `divergent:` written as a BARE STRING is the copies-family spelling, and the
# first draft of the guard read only `exempt`/`reason` -- so this shape went
# unscanned. Both `divergent` spellings are pinned here.
rc, out, err = run("""org: tracebloc
source_repo: .github
repos:
  demo:
    copies:
      add-to-kanban.yml:
        divergent: >-
          the pin was the other half of this entry until backend#1816 landed
""", states={"tracebloc/backend#1816": CLOSED_ISSUE})
record(rc == 1 and "backend#1816" in err,
       "a `divergent:` written as a bare string is scanned",
       f"rc={rc} err={err.strip()[:150]!r}")

rc, out, err = run("""org: tracebloc
source_repo: .github
repos:
  demo:
    protection:
      develop:
        divergent:
          reason: >-
            armed on top of the baseline under backend#1975
          min_reviews: 2
""", states={"tracebloc/backend#1975": CLOSED_ISSUE})
record(rc == 1 and "backend#1975" in err,
       "a `divergent:` mapping's `reason:` is scanned",
       f"rc={rc} err={err.strip()[:150]!r}")

# An anchor DEFINED BUT NOT YET ALIASED is invisible to a walk of `repos:` alone,
# and that is the state a new reason is in on the PR that introduces it.
rc, out, err = run("""org: tracebloc
source_repo: .github
shared_reasons:
  brand_new: &brand_new >-
    staged behind backend#1408
repos:
  demo:
    callers:
      thing.yml:
        exempt: nothing cited here at all, deliberately
""", states={"tracebloc/backend#1408": CLOSED_ISSUE})
record(rc == 1 and "shared_reasons.brand_new" in err,
       "an anchor defined in `shared_reasons:` but not yet aliased is scanned",
       f"rc={rc} err={err.strip()[:170]!r}")

# COMMENTS ARE NOT REASONS. The scan runs over parsed YAML on purpose -- the
# inventory's header carries dozens of ticket numbers that justify nothing.
rc, out, err = run(inv("blocked on backend#1408",
                       head="# a header comment citing backend#9999\n"),
                   states={"tracebloc/backend#1408": OPEN_ISSUE})
record(rc == 0,
       "a ticket number in a YAML COMMENT is not treated as a citation",
       f"rc={rc} err={err.strip()[:130]!r}")

# --- the exemption map, both halves ------------------------------------------
rc, out, err = run(inv("blocked on backend#1408"),
                   states={"tracebloc/backend#1408": CLOSED_ISSUE},
                   exempt="tracebloc/backend#1408")
record(rc == 0,
       "an EXEMPTED dead citation is not a finding",
       f"rc={rc} err={err.strip()[:130]!r}")

rc, out, err = run(inv("blocked on backend#1408"),
                   states={"tracebloc/backend#1408": OPEN_ISSUE},
                   exempt="tracebloc/backend#1408")
record(rc == 1 and "no longer a finding" in err,
       "a STALE exemption is a finding too",
       f"rc={rc} err={err.strip()[:150]!r}")

# --- the PIN: an exemption must still describe its own population ------------
# saadqbal on .github#374. A row whose reason reasons about N specific citations
# must expire when N changes, or a NEW citation -- possibly one deferring live
# work to the closed issue -- is covered by prose written about different ones.
# Counts here are written down independently of the matcher (rule 9's corollary):
# each inventory below is authored to cite the issue a known number of times.

rc, out, err = run(inv("blocked on backend#1408"),
                   states={"tracebloc/backend#1408": CLOSED_ISSUE},
                   exempt="tracebloc/backend#1408=1")
record(rc == 0,
       "a pinned exemption whose count MATCHES is not a finding",
       f"rc={rc} err={err.strip()[:150]!r}")

rc, out, err = run(inv("blocked on backend#1408", second="also backend#1408"),
                   states={"tracebloc/backend#1408": CLOSED_ISSUE},
                   exempt="tracebloc/backend#1408=1")
record(rc == 1 and "pinned count" in err and "2 reason(s) cite it" in err,
       "a pinned exemption EXPIRES when a citation is ADDED (pinned 1, now 2)",
       f"rc={rc} err={err.strip()[:200]!r}")

rc, out, err = run(inv("blocked on backend#1408"),
                   states={"tracebloc/backend#1408": CLOSED_ISSUE},
                   exempt="tracebloc/backend#1408=2")
record(rc == 1 and "pinned count" in err and "1 reason(s) cite it" in err,
       "a pinned exemption EXPIRES when a citation is REMOVED too (pinned 2, now 1)",
       f"rc={rc} err={err.strip()[:200]!r}")

rc, out, err = run(inv("blocked on backend#1408"),
                   states={"tracebloc/backend#1408": CLOSED_ISSUE},
                   exempt="tracebloc/backend#1408")
record(rc == 0,
       "an UNPINNED exemption still works -- the pin is opt-in per row",
       f"rc={rc} err={err.strip()[:150]!r}")

# --- only the offender is named ----------------------------------------------
rc, out, err = run(inv("landed under model-zoo#115; blocked on backend#1408"),
                   states={"tracebloc/model-zoo#115": MERGED_PR,
                           "tracebloc/backend#1408": CLOSED_ISSUE})
record(rc == 1 and "backend#1408" in err and "model-zoo#115" not in err,
       "only the dead citation is named, not its live neighbour",
       f"rc={rc} err={err.strip()[:150]!r}")

failed = [r for r in RESULTS if not r[0]]
print(f"\nreason-citations-selftest: {len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
