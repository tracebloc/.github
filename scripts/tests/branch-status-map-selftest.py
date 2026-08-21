#!/usr/bin/env python3
"""Cases for the one branch -> Status mapping (backend#2243)."""
import pathlib
import re
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from branch_status_map import DEFAULT_MAP, ENV_FOR_STATUS, resolve  # noqa: E402

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

# An override naming a Status this file does not know keeps the DEFAULT env rather
# than inventing one: the Status is the operator's call, the env is derived, and
# deriving it from an unknown is a guess.
eq("an unknown Status keeps the default env",
   resolve("develop", {"develop": "Some New Column"}), ("Some New Column", "dev"))
eq("a non-string override is ignored", resolve("develop", {"develop": 7}), ("On dev", "dev"))
eq("an empty override value is ignored", resolve("develop", {"develop": ""}), ("On dev", "dev"))
eq("no override behaves as the default", resolve("main", None), ("Prod", "prod"))

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

# AND THE FALLBACK CALLS EXIST WHERE THEY MUST. The router is the caller that cannot
# afford to publish nothing, so both of its arms need one.
_router = (_WF / "kanban-closure-router.yml").read_text()
eq("the router has a --no-override fallback in both arms",
   _router.count("--no-override"), 2)
eq("reconcile has NO fallback -- it skips the item instead",
   "--no-override" in (_WF / "kanban-reconcile.yml").read_text(), False)

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
