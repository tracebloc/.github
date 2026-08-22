#!/usr/bin/env python3
"""Suite for scripts/mint-scope.py.

The guard says "a mint with no `permission-*` is a finding". Everything below
drives it against FIXTURE workflow directories, because a suite that named real
workflows would redden every time one of them is fixed -- and the burn-down is the
point.

Each case pins a behaviour a mutation would break. `mint-scope-mutations` in
mutation-check breaks each one and asserts this suite reddens; a case here that
survives its own mutation is vacuous and worse than absent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE.parent / "mint-scope.py"

RESULTS = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n        {detail}")


MINT = "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1"


def wf(*, scoped: bool, mint: bool = True, action: str = MINT,
       with_block: bool = True) -> str:
    """One workflow. `scoped` decides whether it names any permission-* input.

    `with_block=False` omits `with:` entirely -- a shape no earlier fixture had,
    and the mutation run is what noticed: "a missing `with:` counts as scoped"
    survived the whole suite because every fixture supplied one.
    """
    step = ""
    if mint and not with_block:
        step = f"      - uses: {action}\n"
    elif mint:
        step = f"""      - uses: {action}
        with:
          app-id: ${{{{ secrets.A }}}}
          private-key: ${{{{ secrets.B }}}}
          owner: ${{{{ github.repository_owner }}}}
"""
        if scoped:
            step += "          permission-issues: read\n"
    return f"""name: w
on: [push]
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
{step}      - run: echo done
"""


def run(files: "dict[str, str]", exempt: "str | None" = None):
    """Write a fixture dir, run the guard over it, return (rc, stdout, stderr).

    PASS `exempt` ON EVERY CASE, including `""`. Leaving it unset lets the LIVE
    exemption map apply, and against a fixture directory all twelve real rows read
    as stale -- so the suite reddens on production state rather than on the case it
    is testing. Two cases were written that way and failed for exactly that reason;
    the failure was the stale-exemption check working, on the wrong input.
    """
    d = tempfile.mkdtemp()
    for name, body in files.items():
        Path(d, name).write_text(body, encoding="utf-8")
    env = dict(os.environ, MINT_SCOPE_DIR=d)
    if exempt is not None:
        env["MINT_SCOPE_EXEMPT"] = exempt
    p = subprocess.run([sys.executable, str(GUARD)], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


# --- the finding this guard exists for ------------------------------------
rc, out, err = run({"bad.yml": wf(scoped=False)}, exempt="")
record(rc == 1 and "bad.yml" in err,
       "an unscoped mint is a finding",
       f"rc={rc} err={err.strip()[:120]!r}")

rc, out, err = run({"good.yml": wf(scoped=True)}, exempt="")
record(rc == 0 and not err.strip(),
       "a scoped mint is clean",
       f"rc={rc} out={out.strip()[:110]!r}")

# EXEMPTED means not a finding -- this is what lets the guard land green over 12
# pre-existing offenders instead of as a red gate nobody can merge past.
rc, out, err = run({"bad.yml": wf(scoped=False)}, exempt="bad.yml")
record(rc == 0,
       "an exempted unscoped mint is not a finding",
       f"rc={rc} err={err.strip()[:110]!r}")

# ...and the other half, which is what stops the list becoming permanent cover.
rc, out, err = run({"good.yml": wf(scoped=True)}, exempt="good.yml")
record(rc == 1 and "no longer mints an unscoped" in err,
       "a STALE exemption is a finding too",
       f"rc={rc} err={err.strip()[:130]!r}")

# --- fail closed ----------------------------------------------------------
# The premise is that mints exist. Finding none means the matcher broke.
rc, out, err = run({"none.yml": wf(scoped=False, mint=False)}, exempt="")
record(rc == 2 and "no `actions/create-github-app-token` step" in err,
       "ZERO mints found is a hard error, not a clean run",
       f"rc={rc} err={err.strip()[:130]!r}")

rc, out, err = run({"broken.yml": "jobs: [this is: not: valid\n"}, exempt="")
record(rc == 2 and "could not be parsed" in err,
       "an unparseable workflow is a hard error, not a skip",
       f"rc={rc} err={err.strip()[:130]!r}")

d = tempfile.mkdtemp()
p = subprocess.run([sys.executable, str(GUARD)], capture_output=True, text=True,
                   env=dict(os.environ, MINT_SCOPE_DIR=d, MINT_SCOPE_EXEMPT=""))
record(p.returncode == 2 and "no workflow files" in p.stderr,
       "an empty workflow directory is a hard error",
       f"rc={p.returncode} err={p.stderr.strip()[:120]!r}")

p = subprocess.run([sys.executable, str(GUARD)], capture_output=True, text=True,
                   env=dict(os.environ, MINT_SCOPE_DIR=d + "/nope", MINT_SCOPE_EXEMPT=""))
record(p.returncode == 2 and "not a directory" in p.stderr,
       "a missing workflow directory is a hard error",
       f"rc={p.returncode} err={p.stderr.strip()[:120]!r}")

# --- the matcher itself ---------------------------------------------------
# A VERSION BUMP MUST NOT SILENTLY STOP THE CHECK. The `uses:` value is split on
# `@`, so a new pin is still matched -- the failure mode a literal full-string
# match would have, and the reason MINT_ACTION holds no version.
rc, out, err = run({"bumped.yml": wf(scoped=False, action="actions/create-github-app-token@v9")}, exempt="")
record(rc == 1 and "bumped.yml" in err,
       "a DIFFERENT version pin of the mint action is still matched",
       f"rc={rc} err={err.strip()[:120]!r}")

# A different action that merely contains the name is NOT the mint action.
rc, out, err = run({"other.yml": wf(scoped=False, action="evil/actions-create-github-app-token@v1")}, exempt="")
record(rc == 2 and "no `actions/create-github-app-token` step" in err,
       "a look-alike action name is not treated as the mint",
       f"rc={rc} err={err.strip()[:130]!r}")

# NO `with:` AT ALL is the same finding as a `with:` that names no scopes, and
# nothing pinned that until a mutation ("a missing `with:` counts as scoped")
# survived the entire suite. Every fixture happened to supply one.
rc, out, err = run({"nowith.yml": wf(scoped=False, with_block=False)}, exempt="")
record(rc == 1 and "nowith.yml" in err,
       "a mint with NO `with:` block at all is a finding",
       f"rc={rc} err={err.strip()[:120]!r}")

# A look-alike that a SUBSTRING test would wrongly match. The earlier look-alike
# (`evil/actions-create-github-app-token`) does not contain the real name -- the
# hyphen breaks it -- so it could not distinguish `in` from `split("@")[0] ==`.
# Found the same way: the substring mutation survived.
rc, out, err = run({"nested.yml": wf(scoped=False,
                                     action="myorg/actions/create-github-app-token@v1")},
                   exempt="")
record(rc == 2 and "no `actions/create-github-app-token` step" in err,
       "an action whose path CONTAINS the mint name is not the mint",
       f"rc={rc} err={err.strip()[:130]!r}")

# Several files, one bad: the report must name the bad one and only it.
rc, out, err = run({"a.yml": wf(scoped=True), "b.yml": wf(scoped=False), "c.yml": wf(scoped=True)}, exempt="")
record(rc == 1 and "b.yml" in err and "a.yml" not in err and "c.yml" not in err,
       "only the offending file is named",
       f"rc={rc} err={err.strip()[:130]!r}")

failed = [r for r in RESULTS if not r[0]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
