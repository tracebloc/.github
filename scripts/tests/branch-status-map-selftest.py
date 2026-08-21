#!/usr/bin/env python3
"""Cases for the one branch -> Status mapping (backend#2243)."""
import contextlib
import io
import pathlib
import re
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from branch_status_map import (  # noqa: E402
    DEFAULT_MAP,
    ENV_FOR_STATUS,
    UNKNOWN_STATUS_MARKER,
    resolve,
)

P, F = 0, 0
def ok(m):
    global P
    P += 1
    print(f"PASS  {m}")


def bad(m):
    global F
    F += 1
    print(f"FAIL  {m}")


def eq(what, got, want):
    if got == want:
        ok(f"{what}: {got!r}")
    else:
        bad(f"{what}: got {got!r}, want {want!r}")

# THE INPUT DOMAIN IS DERIVED FROM THE PRODUCER, not hand-listed (rule 6): every
# branch the mapping declares must resolve, and the env must agree with the Status.
for branch, (status, env) in DEFAULT_MAP.items():
    eq(f"default {branch}", resolve(branch), (status, env))
    eq(f"{branch}'s env agrees with its Status", ENV_FOR_STATUS.get(status), env)

# FAIL CLOSED on a branch nobody declared -- both callers skip on an empty Status,
# and a guessed one is a board write nobody asked for.
eq("an undeclared branch yields no Status", resolve("feature/whatever"), ("", ""))
eq("an empty branch name yields no Status", resolve(""), ("", ""))

# THE OVERRIDE IS THE WHOLE POINT OF THE TICKET: it was read by one writer and
# ignored by the other two sites.
eq("an override replaces the Status", resolve("staging", {"staging": "Prod"}), ("Prod", "prod"))
eq("an override moves Deploy environment with it",
   resolve("develop", {"develop": "FR on staging"}), ("FR on staging", "staging"))
eq("an override for ANOTHER branch does not leak",
   resolve("develop", {"staging": "Prod"}), ("On dev", "dev"))

eq("an override works on a branch DEFAULT_MAP does not declare",
   resolve("release/1.0", {"release/1.0": "Prod"}), ("Prod", "prod"))
eq("a non-string override is ignored", resolve("develop", {"develop": 7}), ("On dev", "dev"))
eq("an empty override value is ignored", resolve("develop", {"develop": ""}), ("On dev", "dev"))
eq("no override behaves as the default", resolve("main", None), ("Prod", "prod"))

# --- an override may only name a Status the mapping DECLARES (backend#2324) ---
#
# `resolve` used to return the override's Status verbatim. An unknown BRANCH was
# refused and an unknown STATUS was not, and that asymmetry is what made a typo
# destructive: the name reaches `kanban-closure-router.yml`, resolves to no option
# id, and the update step aborts WITHOUT WRITING -- at which point the project's
# built-in "Item closed" automation sets `Cancelled` and `kanban-archive.yml`
# archives the card within a day. The operator sees it vanish.
#
# THE INPUT DOMAIN IS DERIVED FROM THE PRODUCER (rule 6). The accepted half is every
# key `ENV_FOR_STATUS` declares -- all of it, not a sample -- so a column added to
# the table is exercised with no edit here. The refused half is MUTATED from those
# same keys, so a future column rename carries its own bad-name cases along with it
# and cannot re-open this by leaving a hand-written string behind.


def mutants(name):
    """Ways a Status name gets written wrong in a hand-edited YAML file."""
    return (name.lower(), name.upper(), name.replace(" ", ""),
            name + " ", name[:-1], name + "s")


# Retired column names, which is what an operator copying an older runbook actually
# types. Kept as data rather than derived because history is not derivable -- but
# FILTERED against the live table below, so re-adding one of these to
# `ENV_FOR_STATUS` retires its case instead of reddening this file for no reason.
RETIRED = ("Staging (human review)", "FR on dev", "Ready for staging", "Rework")


def refusal(branch, want):
    """(exit code, stderr) from resolving `want`, or None if it was NOT refused."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            got = resolve(branch, {branch: want})
    except SystemExit as exc:
        return exc.code, buf.getvalue()
    bad(f"{want!r} was accepted as a Status: {got!r}")
    return None


# ACCEPTED: every declared Status, on every declared branch, and the environment
# comes from the SAME dict the acceptance test reads -- so the two cannot disagree
# the way a second `.get(want, default)` fallback allowed.
for _st, _env in sorted(ENV_FOR_STATUS.items()):
    for _br in sorted(DEFAULT_MAP):
        # A refusal here escapes as SystemExit and would take the whole suite down
        # mid-run, reporting the abort instead of the case. Caught so a guard that
        # refuses EVERYTHING -- the opposite regression -- reads as one clean FAIL.
        try:
            eq(f"an override to {_st!r} is accepted on {_br}",
               resolve(_br, {_br: _st}), (_st, _env))
        except SystemExit:
            bad(f"a DECLARED Status was refused: {_st!r} on {_br}")

# REFUSED: everything else, and the cases are the declared names bent out of shape.
_cases = [m for name in ENV_FOR_STATUS for m in mutants(name)] + list(RETIRED)
# AN INERT CASE AND A WORKING GUARD LOOK IDENTICAL. A mutant that collides with a
# real declared name would be silently testing acceptance, so assert the domain is
# genuinely outside the table before asserting anything about it.
_cases = [c for c in _cases if c not in ENV_FOR_STATUS]
eq("the refusal cases are derived from every declared Status",
   len(_cases) >= len(ENV_FOR_STATUS) * 4, True)
for _bad in sorted(set(_cases)):
    _r = refusal("develop", _bad)
    if _r is None:
        continue
    _code, _err = _r
    # THE SPECIFIC REFUSAL, NOT ANY EXIT (rule 10). `read_override` raises SystemExit
    # for four other reasons; a case that cannot say WHICH one it got would go on
    # passing while exercising a different path than its name describes.
    eq(f"{_bad!r} is refused, and for the unknown-Status reason",
       (_code, UNKNOWN_STATUS_MARKER in _err), (1, True))
    # The message has to be actionable: the operator needs the branch, the value they
    # wrote, and what they were allowed to write.
    eq(f"the refusal for {_bad!r} names the branch, the value and the vocabulary",
       ("`develop`" in _err and repr(_bad) in _err
        and all(n in _err for n in ENV_FOR_STATUS)), True)

# AND THE GUARD MUST NOT BE "REFUSE EVERYTHING" -- the accepted loop above is that
# half, asserted here as one line so a future edit cannot delete it unnoticed.
eq("the vocabulary is non-empty, so the accepted loop is not vacuous",
   len(ENV_FOR_STATUS) > 0, True)

# THE REFUSAL REACHES THE CALLERS AS A NON-ZERO EXIT, which is the whole mechanism:
# all three consumers already handle that, and none of them handled a successful
# answer carrying an impossible name. `read_override` is stubbed so this exercises
# `main`'s own propagation rather than a fetch.
import branch_status_map as _mod  # noqa: E402

_orig_read = _mod.read_override
try:
    _mod.read_override = lambda repo, ref="HEAD": {"develop": "Pord"}
    _buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(_buf), contextlib.redirect_stdout(io.StringIO()):
            _rc = _mod.main(["branch_status_map.py", "develop", "o/r", "develop"])
        bad(f"main() returned {_rc} instead of refusing an unknown Status")
    except SystemExit as _exc:
        eq("main() exits non-zero on an unknown Status, so `if ! STATUS=$(...)` fires",
           (_exc.code, UNKNOWN_STATUS_MARKER in _buf.getvalue()), (1, True))
finally:
    _mod.read_override = _orig_read

# NO WORKFLOW MAY HOLD A SECOND COPY. This is the finding the ticket was filed for --
# three sites, two of which ignored the override -- so it is a machine check now
# rather than a sentence in a comment. Code lines only: the comments in these files
# legitimately DISCUSS the old mapping.
WF = pathlib.Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"
# NARROWED to the actual defect shape: a case arm keyed on a BRANCH NAME that
# assigns a Status. The first version matched any `STATUS="On dev"` and flagged three
# innocent sites -- a sibling-merge holding state and two "this closer has no base
# ref" floors. Those are policy defaults for cases where there IS no branch, not
# copies of the branch mapping, and a guard that cannot tell the difference would
# have been argued with and then switched off.
pat = re.compile(r'^\s*(?:main\|master|master\|main|develop|staging)[^)\n]*\)\s*'
                 r'(?:DEPLOY_ENV="[^"]*";\s*)?STATUS(?:_NAME)?="')
offenders = []
for f in sorted(WF.glob("*.yml")):
    for i, line in enumerate(f.read_text().splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        if pat.search(line):
            offenders.append(f"{f.name}:{i}")
eq("no workflow re-declares the branch mapping", offenders, [])

# ... and the guard must be able to SEE one, or it is decoration.
# The guard must SEE the real shape, and must NOT see the innocent ones -- both
# directions, because a pattern that matches nothing and a pattern that matches
# everything are equally useless and look identical on a clean tree.
eq("the guard matches a branch-keyed mapping arm",
   bool(pat.search('                main|master) STATUS="Prod" ;;')), True)
eq("the guard matches the DEPLOY_ENV form too",
   bool(pat.search('            master|main) DEPLOY_ENV="prod";    STATUS_NAME="Prod" ;;')), True)
eq("the guard ignores a no-branch policy floor",
   bool(pat.search('                  *)           STATUS="On dev" ;;')), False)
eq("the guard ignores a bare holding-state assignment",
   bool(pat.search('                  STATUS="On dev"')), False)

# --- `--no-override` answers without consulting anything -------------------
# The router needs a safe fallback: publishing NO Status leaves the built-in
# "Item closed" automation to set `Cancelled` and archive shipped work
# (.github#157), which is strictly worse than ignoring an override for one run.
# So the policy lives in the caller and this flag is how it asks (Bugbot, .github#295).
import subprocess as _sub  # noqa: E402

_MAP = str(pathlib.Path(__file__).resolve().parent.parent / "branch_status_map.py")
for _b, _want in (("develop", "On dev"), ("main", "Prod"), ("staging", "FR on staging")):
    _r = _sub.run([sys.executable, _MAP, _b, "--no-override"],
                  capture_output=True, text=True)
    eq(f"--no-override answers {_b} from DEFAULT_MAP", _r.returncode, 0)
    eq(f"--no-override gives {_b} its default Status", _want in _r.stdout, True)

# IT MUST NOT TOUCH THE NETWORK -- that is the whole point of the fallback. `gh` is
# removed from PATH, so any fetch attempt fails and the flag is proven to skip it.
_r = _sub.run([sys.executable, _MAP, "develop", "owner/repo", "develop",
               "--no-override"],
              capture_output=True, text=True, env={"PATH": "/nonexistent"})
eq("--no-override consults nothing even when a repo and ref are given",
   (_r.returncode, "On dev" in _r.stdout), (0, True))

# ... and WITHOUT the flag, the same call with no `gh` refuses rather than
# defaulting -- so the flag is doing the work, not a silent fallback.
_r = _sub.run([sys.executable, _MAP, "develop", "owner/repo", "develop"],
              capture_output=True, text=True, env={"PATH": "/nonexistent"})
eq("without the flag, an unreachable override refuses", _r.returncode != 0, True)

# --- every caller must pass the REF, not rely on the repo default ----------
# Defaulting to the API's HEAD reads the repo's DEFAULT branch, so an override present
# on `develop` but not yet on `main` was ignored by both writers -- and
# advance-deploy-env previously read `.kanban.yml` off the checked-out PUSHED branch,
# so it was a regression (Bugbot, .github#295). Asserted by reading the call sites,
# because the failure is a MISSING argument and no unit call can show that.
_WF = pathlib.Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"
for _f, _n in (("advance-deploy-env.yml", 1),
               ("kanban-closure-router.yml", 2),
               ("kanban-reconcile.yml", 1)):
    _txt = (_WF / _f).read_text()
    _lines = _txt.splitlines()
    _calls = []
    for _i, _ln in enumerate(_lines):
        if "branch_status_map.py" not in _ln or "python3" not in _ln:
            continue
        # A call's arguments can continue onto the next line.
        _stmt = " ".join(_lines[_i:_i + 2])
        # `--no-override` CALLS ARE A DIFFERENT SHAPE and are excluded on purpose: by
        # definition they consult no `.kanban.yml`, so demanding a ref of them would
        # be demanding the opposite of what they are for. Counting them was this
        # assertion's own first failure when the router gained its fallback.
        if "--no-override" in _stmt:
            continue
        _calls.append(_stmt)
    eq(f"{_f}: every override-consulting call site found", len(_calls), _n)
    # The ref is the 3rd positional. A call with only branch+repo silently reads the
    # default branch, which is the finding.
    for _stmt in _calls:
        _args = _stmt.split("branch_status_map.py", 1)[1]
        eq(f"{_f}: the call passes a ref, not just branch+repo",
           _args.count('"$') >= 3, True)

# THE THREE CONSUMERS' NO-WRITE PATHS, pinned as a set -- because saadqbal's point
# on .github#295 is that "refuse rather than guess" assumes doing nothing is safe,
# and that assumption is false at a decision point whose default is supplied by
# ANOTHER system. Each caller's answer is different for a stated reason, so each is
# asserted rather than one standing in for all three.
_router = (_WF / "kanban-closure-router.yml").read_text()
_recon = (_WF / "kanban-reconcile.yml").read_text()
_adv = (_WF / "advance-deploy-env.yml").read_text()

# ROUTER: writes the non-terminal HOLDING STATE, not the default mapping, and
# labels the card. Writing the default would claim a promotion happened on a read
# that failed; writing nothing lets the built-in Item-closed automation set
# `Cancelled`, and it acts on the close independently of this workflow.
eq("the router writes the holding state on an unusable override",
   _router.count('UNUSABLE_OVERRIDE="true"'), 2)
eq("the router does NOT fall back to the default mapping",
   "--no-override" in _router, False)
eq("the router labels the card for the weekly pass",
   "override-unusable" in _router, True)
# THE LABEL MUST NOT CLAIM A CAUSE IT CANNOT KNOW (backend#2324). Two different
# failures reach this holding state now -- a `.kanban.yml` that cannot be read, and
# one that reads fine and names a Status the mapping does not declare. A label
# saying "unreadable" sends the operator looking for a fetch error that never
# happened, in the file whose contents are the actual bug.
# CODE LINES ONLY. The comments above the rename explain it and necessarily quote
# the old name -- a raw-text scan would flag the very sentence saying it is gone,
# which is the trap e2e#176 hit and this repo keeps re-hitting.
_router_code = [ln for ln in _router.splitlines() if not ln.strip().startswith("#")]
eq("the router no longer calls the holding state 'unreadable' in its CODE",
   [ln.strip() for ln in _router_code
    if "override-unreadable" in ln or "UNREADABLE_OVERRIDE" in ln], [])
# ... and the scan can see the shape it is looking for, or it is decoration.
eq("that scan would catch a reinstated 'unreadable' identifier",
   bool([ln for ln in ['          UNREADABLE_OVERRIDE="true"']
         if "UNREADABLE_OVERRIDE" in ln]), True)

# RECONCILE: skips the item. It fixes MISSES, so a guessed column would overrule a
# router that already got it right -- and nothing else acts on its silence.
eq("reconcile skips rather than writing anything",
   "leaving it alone rather than guessing a column" in _recon, True)

# ADVANCE-DEPLOY-ENV: no fallback at all. A push has no competing automation, so a
# failed run is a failed run and the card keeps whatever it had.
eq("advance-deploy-env has no fallback path", "--no-override" in _adv, False)

# NO CALL SITE MAY DISCARD THE MAPPER'S STDERR (backend#2324). All three used to,
# which cost nothing while the only refusal was "could not fetch" -- the router's
# flag and reconcile's skip line said that much on their own. The unknown-Status
# refusal is different: it names the branch, the bad value and the accepted
# vocabulary, and that message is the ONLY thing that tells an operator which
# `.kanban.yml` line to fix. Swallowed, the card is parked with no reason anywhere.
for _f, _txt in (("kanban-closure-router.yml", _router),
                 ("kanban-reconcile.yml", _recon),
                 ("advance-deploy-env.yml", _adv)):
    _lines = _txt.splitlines()
    _sites = [" ".join(_lines[_i:_i + 3]) for _i, _ln in enumerate(_lines)
              if "branch_status_map.py" in _ln and "python3" in _ln]
    # Assert the sites were FOUND before asserting anything about them: zero sites
    # scanned compares equal to zero sites offending.
    eq(f"{_f}: mapper call sites located", len(_sites) > 0, True)
    for _site in _sites:
        eq(f"{_f}: the mapper's stderr is not discarded",
           "2>/dev/null" in _site, False)

# --- read_override: absent vs unfetchable are different answers ------------
# The whole point of this module is that an override gets applied. A fetch failure
# that returns {} is indistinguishable from "no .kanban.yml", so a present override
# behind a 403/5xx/rate-limit was silently ignored -- the defect this module exists to
# close, reached by a different door (Bugbot, .github#295).
import subprocess as _sp  # noqa: E402


class _Res:
    def __init__(self, out=""):
        self.stdout = out


def _stub(err, rc=1):
    def run(args, **kw):
        raise _sp.CalledProcessError(rc, args, output="", stderr=err)
    return run


import branch_status_map as _m  # noqa: E402

_real = _sp.run
try:
    # 404 is the ONLY failure meaning "no override".
    _m.subprocess.run = _stub("gh: Not Found (HTTP 404)")
    eq("a 404 is an empty override, not an error", _m.read_override("o/r"), {})

    # Everything else REFUSES. `SystemExit` is the specific failure asserted, not a
    # bare "it raised" -- a different exception would mean a different path.
    for err in ("gh: Forbidden (HTTP 403)", "gh: Bad gateway (HTTP 502)",
                "API rate limit exceeded"):
        # Set the stub BEFORE the call. Setting it after left the first iteration
        # running against the 404 stub from the case above, so the 403 case reported
        # a false failure -- the test's own off-by-one, caught by the test.
        _m.subprocess.run = _stub(err)
        try:
            _m.read_override("o/r")
            bad(f"an unfetchable override was accepted as empty: {err}")
        except SystemExit:
            ok(f"refuses on {err.split('(')[0].strip()}")
        except Exception as exc:                                  # noqa: BLE001
            bad(f"wrong failure for {err}: {type(exc).__name__}")
    # A `branch_status_map` THAT PARSES BUT IS NOT A MAP refuses too. This was the
    # one arm that returned {} silently while the docstring claimed it announced
    # itself -- so the FIVE outcomes are now asserted as a set, not one at a time,
    # because the defect was an inconsistency BETWEEN them (saadqbal on .github#295).
    class _Ok:
        def __init__(self, out):
            self.stdout = out

    for body, kind in (("[1, 2]", "list"), ('"a string"', "str"), ("7", "int")):
        _m.subprocess.run = lambda *a, **k: _Ok(body)
        try:
            got = _m.read_override("o/r")
            bad(f"a {kind} branch_status_map was accepted as empty: {got!r}")
        except SystemExit:
            ok(f"refuses a branch_status_map that is a {kind}")
        except Exception as exc:                                  # noqa: BLE001
            bad(f"wrong failure for a {kind}: {type(exc).__name__}")

    # ... and a real mapping still comes back, so the guard is not "refuse anything".
    _m.subprocess.run = lambda *a, **k: _Ok('{"develop": "Done"}')
    eq("a real mapping is returned unchanged",
       _m.read_override("o/r"), {"develop": "Done"})
finally:
    _m.subprocess.run = _real

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
