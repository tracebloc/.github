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

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
