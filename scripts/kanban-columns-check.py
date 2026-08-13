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
WRITERS = ("advance-deploy-env.yml", "kanban-closure-router.yml")

# `STATUS_NAME="On dev"` / `STATUS="Prod"`. Only literals: `STATUS_NAME="$OVERRIDE"`
# is a per-repo .kanban.yml value that cannot be known here.
LITERAL = re.compile(r'\bSTATUS(?:_NAME)?="([^"$]+)"')

PROJECT_ID = "PVT_kwDOCSsgos4BTDWN"  # engineer kanban (org project #2)


def written_names() -> "dict[str, set[str]]":
    found: "dict[str, set[str]]" = {}
    for name in WRITERS:
        path = WORKFLOWS / name
        if not path.is_file():
            sys.exit(f"error: {path} not found — WRITERS is stale")
        for value in LITERAL.findall(path.read_text()):
            found.setdefault(value, set()).add(name)
    if not found:
        sys.exit("error: no Status literals found at all — the pattern is stale, "
                 "which would make this check pass vacuously")
    return found


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
