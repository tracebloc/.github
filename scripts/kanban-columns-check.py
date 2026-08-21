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
# `advance-deploy-env.yml` IS BACK (saadqbal on .github#295), and its removal was the
# SECOND wrong one on this PR -- the router was the first. The reasoning was that
# `WRITERS` is a write-side name and this file's thirteen Status literals live in
# `rank()`, which CONSUMES a Status rather than emitting one. Technically defensible,
# and wrong about what the check is for: an unknown Status returns "" from `rank()`,
# the guard falls through to strict equality, and the card then BLOCKS every prod
# promotion carrying it. A read-side name that does not resolve breaks at least as
# loudly as a write-side one.
#
# Two hand-removals, two errors, is why `unlisted_namers()` below exists: the tuple
# is now checked rather than trusted.
WRITERS = (
    "advance-deploy-env.yml",
    "kanban-closure-router.yml",
    "set-pr-status.yml",
    "fr-pass-comment.yml",
    # FOUND BY `unlisted_namers()` ON ITS FIRST RUN, which is the argument for the
    # guard existing. Neither writes a Status -- `kanban-archive.yml:104` SELECTS the
    # three terminal columns (`Prod`/`Cancelled`/`Done`) to archive, and
    # `wip-limit-check.yml:47` defaults its column input to `Code review` -- and
    # neither name was being checked against the board. A rename would have left the
    # archiver silently archiving nothing and the WIP check counting an empty column,
    # both of which look exactly like a quiet board.
    #
    # `WRITERS` is now really "workflows that NAME a column"; the name is kept
    # because the paths-filter assertion and the idiom cross-check both key on it.
    "kanban-archive.yml",
    "wip-limit-check.yml",
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


def written_names(options: "set[str]") -> "dict[str, set[str]]":
    found: "dict[str, set[str]]" = {}
    idiom_hits = 0
    for name in WRITERS:
        path = WORKFLOWS / name
        if not path.is_file():
            sys.exit(f"error: {path} not found — WRITERS is stale")
        text = path.read_text()
        for quoted, bare in LITERAL.findall(text):
            value = (quoted or bare).strip()
            if value:
                found.setdefault(value, set()).add(name)
                idiom_hits += 1
    # KEYED ON THE IDIOM PASS, NOT ON `found` (Bugbot, .github#295 -- caught while
    # fixing that finding). This guard exists to say "the LITERAL pattern is stale".
    # `found` now has THREE contributors -- the idiom pass, `declared_names()` and
    # `names_in()` over WRITERS -- so testing `found` would let case-arm names alone
    # satisfy it while LITERAL matched nothing: the guard disarmed by the very change
    # that broadened the collection, which is exactly the vacuity it was written to
    # prevent. Fixing one finding nearly created its twin.
    if not idiom_hits:
        sys.exit("error: no Status literals found at all — the pattern is stale, "
                 "which would make this check pass vacuously")

    # THE SHARED MAPPING'S OWN VOCABULARY (backend#2243), folded in HERE rather than
    # in main() so that a caller substituting `written_names` -- which the selftest
    # does, to control the input against a fake board -- still controls the whole
    # input. Folding it into main() silently widened what the selftest could not see.
    for name in declared_names():
        found.setdefault(name, set()).add("branch_status_map.py")

    # EVERY COLUMN NAME A WRITERS FILE MENTIONS, not only the assignment idioms
    # (Bugbot, .github#295). Restoring advance-deploy-env.yml to WRITERS did NOT put
    # `rank()`'s twelve names under this check: `LITERAL` matches `STATUS="..."` and a
    # `case` arm has no `=`, and WRITERS membership also makes `unlisted_namers` skip
    # the file. So all twelve stayed invisible -- and `Backlog`, `North Stars` and
    # `Ready` were collected from NOWHERE AT ALL -- while the comment I had just
    # written claimed they were covered. A claim no check enforces, one commit old.
    for name in WRITERS:
        for got in names_in(WORKFLOWS / name, options):
            found.setdefault(got, set()).add(name)
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


# Exemptions for `unlisted_namers`, at module level so `stale_exemptions` reads the
# SAME copy -- two lists that must agree is the drift this file keeps finding.
EXEMPT_NAMERS = {
    "fr-gate.yml": "its rank table is covered by the fr-gate selftest",
    "kanban-reconcile.yml": "its DEST names come from branch_status_map.py, whose "
                           "vocabulary is imported above",
}


def names_in(path: "Path", options: "set[str]") -> "set[str]":
    """Board column names quoted on a CODE line of `path`. One definition.

    `written_names` and `unlisted_namers` both need "does this file name a column",
    and holding it twice is how they would drift. Code lines only: comments discuss
    column names constantly -- including the ones explaining why this exists.
    """
    found: "set[str]" = set()
    for line in path.read_text().splitlines():
        bare = line.strip()
        if not bare or bare.startswith("#"):
            continue
        for name in options:
            if f'"{name}"' in line:
                found.add(name)
    return found


def unlisted_namers(options: "set[str]", where: "Path | None" = None) -> "list[str]":
    """Workflows that NAME a board column but are not in `WRITERS`.

    TWO HAND-REMOVALS FROM `WRITERS` ON ONE PR WERE BOTH WRONG (saadqbal,
    .github#295): the closure router (six literals) and advance-deploy-env
    (thirteen, in `rank()`). Each removal was argued from "this file does not
    WRITE a Status", and each took real names outside a check whose stated purpose
    is that every Status name a workflow uses exists on the board. A curated tuple
    that has been wrong twice in one PR is not a tuple to trust -- so it is derived
    against now, and a file naming a column while absent from `WRITERS` is a
    finding rather than a judgement call.

    CODE LINES ONLY. Comments discuss column names constantly -- including the two
    comments explaining the removals this function exists because of -- and a
    raw-text scan would flag every one of them. That is the same trap e2e#176 hit.

    EXEMPT names its exceptions with reasons, and a stale exemption is reported by
    the caller for the same reason mint-scope.py reports its own.
    """
    out: "list[str]" = []
    for path in sorted((where or WORKFLOWS).glob("*.yml")):
        if path.name in WRITERS or path.name in EXEMPT_NAMERS:
            continue
        named = names_in(path, options)
        if named:
            out.append(f"{path.name} names {', '.join(sorted(named))} "
                       "but is not in WRITERS")
    return sorted(set(out))


def stale_exemptions(options: "set[str]") -> "list[str]":
    """Exemptions whose reason has expired. The docstring promised this; nothing did.

    `unlisted_namers` said stale entries "are reported by the caller for the same
    reason mint-scope.py reports its own", and `main()` never diffed them (Bugbot,
    .github#295). An exemption can outlive its reason while the check stays green --
    the same shape as the migration anchor backend#1979 had to delete, and the same
    shape as this file's own two wrong WRITERS removals.

    An exemption is stale when the file is gone, when it names no column any more, or
    when it has since joined WRITERS -- in each case the licence is unused.
    """
    out: "list[str]" = []
    for name, why in sorted(EXEMPT_NAMERS.items()):
        path = WORKFLOWS / name
        if not path.is_file():
            out.append(f"{name}: exempt ({why}) but the file no longer exists")
        elif name in WRITERS:
            out.append(f"{name}: exempt ({why}) but it is now in WRITERS, so the "
                       "exemption grants nothing")
        elif not names_in(path, options):
            out.append(f"{name}: exempt ({why}) but it names no board column any "
                       "more, so the exemption is unused")
    return out


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
    options = board_options()
    written = written_names(options)

    stale_ex = stale_exemptions(options)
    if stale_ex:
        print("ERROR: an exemption has outlived its reason, so it silently permits "
              "whatever takes that file's place:")
        for row in stale_ex:
            print(f"  {row}")
        return 1

    unlisted = unlisted_namers(options)
    if unlisted:
        print("ERROR: a workflow names a board column but is not in WRITERS, so "
              "its names are not checked against the board — the shape that took "
              "six literals (the router) and thirteen (advance-deploy-env) outside "
              "this check on .github#295:")
        for row in unlisted:
            print(f"  {row}")
        return 1

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
