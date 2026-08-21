#!/usr/bin/env python3
"""Every Status column name the kanban WRITERS emit must exist on the board.

WHY THIS EXISTS (backend#1592, .github#243/#245)

The board's column names and the workflows that write them are two systems that
must agree, and nothing checked that they did. The rename window for
`FR on staging` was carried by hand: step 1 taught the resolvers both names,
step 3 removed the shim from the writers. In between, the writers emitted a
column the board did not have and only a fallback made it work.

The failure mode is the reason this is a script and not a comment. A written
name that does not resolve means the card is not moved -- and until .github#246
that was a `::warning::` on a GREEN run, so the board silently stopped tracking
the pipeline. The board freezing and the board working looked identical.

SCOPE, deliberately narrow:

  * WRITERS are checked. A name that is written must exist, or the write cannot
    land. This is the assertion.
  * RESOLVERS are reported, not enforced. They ask for names on both sides of a
    rename on purpose -- `opt_either("Staging (human review)", "FR on staging")`
    is the #1592 shim and its first argument is SUPPOSED to be absent today.
    Failing on that would make the tolerance it provides impossible to express.

So this answers one question exactly: can every write this repo performs
actually land on the board as it is configured right now?
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# The workflows that WRITE a Status value. Listed rather than globbed: `STATUS=`
# is a common shell variable name and other workflows use it for unrelated
# values ("ok", "absent", "unreadable"), which are not column names.
#
# THIS LIST WAS WRONG ON ITS FIRST DAY and the check stayed green anyway, which
# is the whole reason for the cross-check below. It named two files and one
# idiom, and missed `set-pr-status.yml` (writes `status_name=In progress`
# UNQUOTED) and `fr-pass-comment.yml` (writes `NEXT="Ready for prod"`) -- so
# three column names were invisible to a guard built to make exactly that
# impossible (Bugbot, .github#243).
# `advance-deploy-env.yml` and `kanban-closure-router.yml` are NO LONGER HERE, and
# that is not an omission: backend#2243 moved the branch -> Status mapping out of
# both and into `branch_status_map.py`, so their literals are gone from the YAML.
# Those names are now checked by IMPORTING the mapping (see `declared_names`), which
# is strictly stronger than scraping a shell `case` with a regex -- it reads the
# actual data structure instead of a rendering of it.
#
# Any workflow still writing a Status literal of its own belongs here.
# `kanban-closure-router.yml` IS STILL HERE (Bugbot, .github#295). Only its BRANCH
# MAPPING moved to `branch_status_map.py`; it still writes `Cancelled`, `Done` and
# `On dev` directly, and `Cancelled`/`Done` are not in the imported mapping -- so
# dropping it would have let those two writes name columns the board no longer has
# while this check went green. `advance-deploy-env.yml` is out because it now writes
# NO literal at all, which is checkable: `grep -oE 'STATUS(_NAME)?="[A-Za-z ()]+"'`
# over it returns nothing.
WRITERS = (
    "kanban-closure-router.yml",
    "set-pr-status.yml",
    "fr-pass-comment.yml",
)


def declared_names() -> "set[str]":
    """Every Status the shared mapping can produce, read from the mapping itself.

    Imported rather than pattern-matched: after backend#2243 this is where the
    branch mapping lives, and a regex over the module's source would be the same
    scrape-a-rendering mistake one layer along.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from branch_status_map import DEFAULT_MAP, ENV_FOR_STATUS  # noqa: PLC0415
    return {s for s, _ in DEFAULT_MAP.values()} | set(ENV_FOR_STATUS)

# The write idioms actually used. Quoted OR bare, because `echo "status_name=In
# progress" >> "$GITHUB_OUTPUT"` has no inner quotes.
#   STATUS="Prod"  STATUS_NAME="On dev"  status_name=In progress  NEXT="Ready for prod"
# `$`-containing values are skipped: `STATUS_NAME="$OVERRIDE"` is a per-repo
# .kanban.yml value that cannot be known here.
LITERAL = re.compile(
    r'\b(?:STATUS(?:_NAME)?|status_name|NEXT)='          # the write idioms
    r'(?:"([^"$\n]+)"'                                    # quoted:  STATUS="Prod"
    r'|([^"$\n;)&|]+?)(?=\s*(?:;|\)|&|\||"|$)))',        # bare:    status_name=In progress
    re.MULTILINE,
)

PROJECT_ID = "PVT_kwDOCSsgos4BTDWN"  # engineer kanban (org project #2)


def written_names() -> "dict[str, set[str]]":
    found: "dict[str, set[str]]" = {}
    for name in WRITERS:
        path = WORKFLOWS / name
        if not path.is_file():
            sys.exit(f"error: {path} not found — WRITERS is stale")
        text = path.read_text()
        for quoted, bare in LITERAL.findall(text):
            value = (quoted or bare).strip()
            if value:
                found.setdefault(value, set()).add(name)
    if not found:
        sys.exit("error: no Status literals found at all — the pattern is stale, "
                 "which would make this check pass vacuously")

    # THE SHARED MAPPING'S OWN VOCABULARY (backend#2243), folded in HERE rather than
    # in main() so that a caller substituting `written_names` -- which the selftest
    # does, to control the input against a fake board -- still controls the whole
    # input. Folding it into main() silently widened what the selftest could not see.
    for name in declared_names():
        found.setdefault(name, set()).add("branch_status_map.py")
    return found


def cross_check(found: "dict[str, set[str]]", options: "set[str]") -> "list[str]":
    """Catch the extractor UNDER-COLLECTING, which is how this check failed first.

    A precise extractor that silently misses an idiom reports a clean sweep of a
    SUBSET -- indistinguishable from a clean sweep of everything. So the names are
    derived a second, independent way and the two are compared.

    The second pass looks at ASSIGNMENT SITES only: a non-comment line with an
    `=`, from which quoted and bare right-hand values are pulled and matched
    WHOLE against board names. Comments, the rank `case` arms and prose mentions
    are therefore ignored -- they name columns without writing them. Whole-value
    matching also stops `Ready` matching inside `Ready for prod`, which a
    substring scan does.

    One-directional on purpose: the precise pass may legitimately find MORE (a
    name written but absent from the board is the primary finding), so only
    crude-minus-precise indicates a stale idiom list.
    """
    rhs = re.compile(r'=\s*(?:"([^"\n]*)"|([^"\n;)&|]*))')
    stale: "list[str]" = []
    for name in WRITERS:
        for line in (WORKFLOWS / name).read_text().splitlines():
            bare_line = line.strip()
            if not bare_line or bare_line.startswith("#") or "=" not in bare_line:
                continue
            for quoted, unquoted in rhs.findall(bare_line):
                value = (quoted or unquoted).strip().strip('"')
                if value in options and value not in found:
                    stale.append(f"{value!r} is assigned in {name} but no idiom matched it")
    return sorted(set(stale))


def board_options() -> "set[str]":
    query = (
        'query{node(id:"%s"){... on ProjectV2{field(name:"Status")'
        '{... on ProjectV2SingleSelectField{options{name}}}}}}' % PROJECT_ID
    )
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"error: could not read the board: {proc.stderr.strip()[:300]}")
    try:
        node = json.loads(proc.stdout)["data"]["node"]
        options = {o["name"] for o in node["field"]["options"]}
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        sys.exit(f"error: unexpected board response ({exc}) — refusing to report "
                 "conformance from a read this script did not understand")
    if not options:
        sys.exit("error: the board reported zero Status options — treating as a "
                 "failed read, not as 'nothing to check'")
    return options


def main() -> int:
    written, options = written_names(), board_options()

    stale = cross_check(written, options)
    if stale:
        print("ERROR: the idiom list is stale — a board column name appears in a "
              "writer that no pattern matched, so this check would report a clean "
              "sweep of a subset:")
        for row in stale:
            print(f"  - {row}")
        return 1

    missing = {n: s for n, s in written.items() if n not in options}

    for name in sorted(written):
        mark = "✗" if name in missing else "ok"
        print(f"  {mark:2} {name:20} <- {', '.join(sorted(written[name]))}")

    if missing:
        print("\nERROR: these Status names are WRITTEN but do not exist on the board:")
        for name in sorted(missing):
            print(f"  - {name!r}  (written by {', '.join(sorted(missing[name]))})")
        print("\nA write to a name the board does not have cannot land, so the card "
              "does not move. Either rename the column back, or update the writers "
              "in the same change. Board has: " + ", ".join(sorted(options)))
        return 1

    print(f"\nAll {len(written)} written Status name(s) exist on the board.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
