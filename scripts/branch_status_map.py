#!/usr/bin/env python3
"""The branch -> (Status, Deploy environment) mapping. ONE definition of it.

WHY THIS FILE EXISTS
--------------------
Two workflows decide a card's Status from the branch a merge landed on, and until
backend#2243 they each held their own copy of the rule:

  advance-deploy-env.yml   1 site, and it READ the per-repo `.kanban.yml` override
  kanban-closure-router.yml  2 sites, and both IGNORED it

Both write `Status`, and a PR merged to `develop` fires both -- the router on
`pull_request: closed`, advance on the `push`. So with a `.kanban.yml` in place they
would write DIFFERENT statuses for the same merge, and which one stuck depended on
run ordering. No repo has a `.kanban.yml` today, which is the only reason this has
never fired: the documented feature has never executed, and the first repo to adopt
it inherits the bug.

The router could not have honoured the override even in principle -- it never checks
out the repo. That is why the fix is one shared mapping rather than a second copy of
the yq read: the override has to be fetched, not assumed to be on disk.

FAIL CLOSED ON AN UNKNOWN BRANCH. An unrecognised branch yields ("", "") and the
callers skip, which is what both did before. A guessed Status on a branch nobody
declared is a board write nobody asked for.

AND FAIL CLOSED ON AN UNKNOWN STATUS (backend#2324). An override naming a column
that is not in `ENV_FOR_STATUS` used to be passed through verbatim -- an unknown
BRANCH was refused while an unknown STATUS was not, which is the asymmetry that
made the first adopter's typo destructive rather than merely wrong. See `resolve`.

THE ACCEPT LIST IS NOT THE DEPLOY PIPELINE (backend#2242). It was five deploy
columns, and the first repo that needed an override needed the sixth: `rfcs` ships
no artifact, so a merge there is `Done` -- completed, nothing deployed
(RFC-BACKEND-1405 D8). Declaring only deploy columns meant the semantically correct
override was the one the #2324 guard refused, so the accept list is the set of
Statuses a MERGE can legitimately mean, not the set of stages a deploy passes
through. See the `Done` row for the three consumers that had to be checked first.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

# THE DEFAULT MAPPING, and the only copy of it. RFC-BACKEND-0008 D6: there is no
# dev-side functional review, so `develop` goes straight to "On dev".
DEFAULT_MAP = {
    "develop": ("On dev", "dev"),
    "staging": ("FR on staging", "staging"),
    "main": ("Prod", "prod"),
    "master": ("Prod", "prod"),
}

# A Status the override may name -> the Deploy environment that has to agree with it.
# Keeping this beside the default map rather than inside a caller is the point: an
# override that sets Status without moving Deploy environment leaves the two board
# fields contradicting each other, which is the shape backend#1277 was about.
#
# AND MEMBERSHIP HERE IS THE ACCEPT LIST (backend#2324). `resolve` used to return an
# override's Status verbatim and fall back to the default env for a name it did not
# know -- so a typo or a retired column name in the first adopter's `.kanban.yml`
# travelled all the way to the board write, where it cannot resolve to an option id
# and the write is abandoned. A closure that writes nothing is not a no-op: the
# project's built-in "Item closed" automation decides instead, sets `Cancelled`, and
# `kanban-archive.yml` archives it off the board within a day. The operator sees the
# card vanish, with no error anywhere near the file they got wrong.
#
# One dict, two answers, and that is deliberate: the Status is accepted BECAUSE it
# has a declared environment, so acceptance and environment cannot drift apart the
# way two lists would. `kanban-columns-check.py` imports these keys and asserts every
# one of them exists on the board, which is what keeps this from being a hand-written
# restatement of the board's vocabulary.
ENV_FOR_STATUS = {
    "On dev": "dev",
    "FR on staging": "staging",
    "Staging (agent review)": "staging",
    # `Staging (human review)` is deliberately ABSENT. `advance-deploy-env.yml`
    # accepted it as an override value, and it is not a column on the board --
    # measured against project #2, whose Status options are Backlog, North Stars,
    # Ready, In progress, Code review, On dev, Staging (agent review), FR on
    # staging, Ready for prod, Prod, Done, Cancelled. A repo that had used it would
    # have had a write rejected for naming a column that does not exist. Nothing
    # caught it because the vocabulary lived in a shell `case` that
    # kanban-columns-check.py did not read; moving it here is what surfaced it.
    "Ready for prod": "staging",
    "Prod": "prod",
    # `Done` -> `none`, AND IT IS THE ONLY NON-DEPLOY ROW HERE (backend#2242).
    #
    # RFC-BACKEND-1405 D8 says RFCs, epics and spikes belong in `Done` -- "completed,
    # but nothing deployed". `rfcs` is a repo of exactly that: it builds no artifact
    # and has no environment, so a merge there is completed work and not a deploy.
    # Without this row the only mappings a `.kanban.yml` could name were the five
    # deploy columns, so the one repo the override exists for could say `On dev` or
    # `Prod` -- a deploy state for a repo that deploys nothing -- or say nothing and
    # keep its cards in `Code review` forever, which is what it did.
    #
    # backend#2243 is cited as having unblocked that, and it unblocked half of it:
    # ONE mapping both writers read. backend#2324 then made an undeclared Status a
    # REFUSAL, which is right, and `Done` was undeclared -- so the semantically
    # correct override was the one shape the new guard rejected. This row is the
    # other half, and the asymmetry is worth naming: the accept list was derived from
    # the DEPLOY pipeline while the board's vocabulary is wider than that.
    #
    # `none` is the environment, not a placeholder: it is a real option on the board's
    # `Deploy environment` field (measured on project #2, 2026-08-22 -- none, Active,
    # dev, staging, prod, Cancelled), and it is the one that AGREES with `Done`. The
    # dict's contract is that acceptance carries its own environment, so leaving this
    # at a deploy env would put the two fields in the contradiction backend#1277 was
    # about -- a card in `Done` stamped `dev`.
    #
    # THREE CONSUMERS WERE CHECKED BEFORE ADDING IT, because `Done` is TERMINAL and
    # every other value here is not:
    #
    #   advance-deploy-env.yml   `rank()` already scores Done 11, above Prod's 10, so
    #                            the monotonic guard advances INTO it and nothing
    #                            demotes out. A push re-carrying an old commit is a
    #                            no-op rather than an un-shipping.
    #   kanban-closure-router.yml  its `STATUS_NAME = "Done"` guard refuses to write
    #                            Done OVER a deploy state. A merged PR's card sits in
    #                            `Code review`, which `classify_column` places BELOW
    #                            `On dev` and therefore returns `no` for -- so the
    #                            write lands. The guard protects the case it was built
    #                            for (a hand-closed card in a deploy column) and does
    #                            not block this one.
    #   kanban-reconcile.yml     its sweep pulls NON-TERMINAL columns only, and Done
    #                            is not in that list -- so the weekly backstop never
    #                            sees these cards and cannot undo the override. That
    #                            mattered: its `drift-to-prod` arm writes `Prod` for
    #                            any merged PR whose sha reached the prod branch and
    #                            consults no `.kanban.yml` at all (deliberately --
    #                            `resolve_prod_branch` refuses to trust a
    #                            repo-controlled file, D27-L4). The sweep filter is
    #                            the only thing standing between that arm and every
    #                            overridden card, so it is a MACHINE CHECK rather
    #                            than this sentence: `branch-status-map-selftest.py`
    #                            parses that filter out of the workflow and asserts
    #                            no Status declaring `none` appears in it.
    #
    # AND RECONCILE'S ROUTER-MISS BACKSTOP NOW HAS A `Done` ARM. The first version of
    # this comment claimed that arm had "no option id for Done and skips", which was
    # simply false -- `DONE_OPT` is resolved in that step and already written by the
    # arm beside it (Bugbot, .github#304). So the skip was an omission, and it fell on
    # exactly the repos this row exists for: a router miss left their cards in an
    # active column with no weekly backstop, the invariant .github#127 fixed for every
    # other mapping. Adding `Done` to the accept list without that arm would have
    # opened a gap rather than found one.
    #
    # `branch-status-map-selftest.py` asserts the general form: a Status this mapping
    # can produce, AND for which reconcile resolves an option id, must have an arm.
    # The two with no arm have no id either -- `Staging (agent review)` is read-only
    # until backend#1578 (RFC-BACKEND-1552 D5) and `Ready for prod` is a human
    # `/fr-pass` act (D6) -- so the check keys on the id rather than demanding an arm
    # for a column nothing may write.
    "Done": "none",
}


# The refusal's stable half, so a caller -- and the selftest -- can tell THIS refusal
# apart from the ones `read_override` raises (see its table). A test that only
# asserts "it raised SystemExit" passes on any of them (CLAUDE.md rule 10).
UNKNOWN_STATUS_MARKER = "names a Status this mapping does not declare"


def resolve(branch: str, override: dict | None = None) -> tuple[str, str]:
    """(Status, Deploy environment) for `branch`, honouring a `.kanban.yml` override.

    AN OVERRIDE MAY ONLY NAME A STATUS `ENV_FOR_STATUS` DECLARES (backend#2324).
    Anything else REFUSES here, at the source, rather than travelling to the board
    write that cannot land it.

    The earlier version returned the name verbatim and kept the default environment
    for one it did not know, reasoning -- carefully, and about the wrong half -- that
    the environment is derived while the Status is the operator's call. The
    environment was never the exposure. An unknown Status resolves to no option id,
    so `kanban-closure-router.yml` aborts its update WITHOUT WRITING, and a closure
    that writes nothing is handed to the project's built-in "Item closed" automation,
    which sets `Cancelled` -- terminal, and archived off the board inside a day.

    So this is the same case as an unreadable `.kanban.yml`, and it now gets the same
    treatment: refuse, loudly, and let each caller apply its own no-write policy
    (see `main`). It fell through that holding state into the destructive branch.
    """
    status, env = DEFAULT_MAP.get(branch, ("", ""))
    if not override:
        return status, env
    want = override.get(branch)
    if not want or not isinstance(want, str):
        return status, env
    if want not in ENV_FOR_STATUS:
        sys.stderr.write(
            f"::error::.kanban.yml maps `{branch}` to {want!r}, which "
            f"{UNKNOWN_STATUS_MARKER}. Refusing rather than passing it through: it "
            "resolves to no Status option, the board write is then abandoned, and "
            "the project's built-in Item-closed automation sets `Cancelled` and "
            "archives the card (backend#2324). Declared Statuses are: "
            + ", ".join(sorted(ENV_FOR_STATUS))
            + ". Fix the name in .kanban.yml, or -- if the column exists on the "
              "board and a merge to this branch really means it -- add it to "
              "ENV_FOR_STATUS in scripts/branch_status_map.py with the Deploy "
              "environment it implies.\n")
        raise SystemExit(1)
    # NOT `.get(want, env)`. The membership test above and this lookup read the SAME
    # dict, so an accepted Status always carries its own environment and the two can
    # never disagree -- which is what a second, looser fallback would have allowed.
    return want, ENV_FOR_STATUS[want]


def read_override(repo: str, ref: str = "HEAD") -> dict:
    """`branch_status_map` from the repo's `.kanban.yml`, via the API.

    FETCHED, NOT READ OFF DISK, because the router never checks the repo out.

    THE REF MATTERS, AND EVERY CALLER PASSES IT (Bugbot, .github#295). Defaulting to
    the API's `HEAD` reads the repo's DEFAULT branch, so an override present on
    `develop` but not yet on `main` was silently ignored by both writers until it
    reached the default -- and `advance-deploy-env` previously read `.kanban.yml` off
    the checked-out PUSHED branch, so this was a regression, not a new limitation.
    Every branch these callers map is a BASE branch (`develop`/`staging`/`main`, or a
    closing PR's base), which persists after a merge -- so passing it is safe even
    though a merged head may be gone.

    EXACTLY ONE OUTCOME IS AN EMPTY OVERRIDE: a 404. The file is optional by design
    and no repo has one, so "absent" has to be ordinary. Every other way this can go
    wrong REFUSES, loudly, with a reason:

        404 ................................ empty override, no message
        any other fetch failure ............ refuse (403, 5xx, rate limit)
        `yq` not installed ................. refuse
        the file does not parse ............ refuse
        `branch_status_map` is not a map ... refuse

    That last row was the odd one out, and saadqbal made the argument better than
    Bugbot did (.github#295): this docstring used to claim "a malformed one is also
    empty, and SAYS SO on stderr", which was true of a parse failure and FALSE of a
    map that parses fine and is a list -- no message, no exit code, `{}` returned. A
    documented contract that one path quietly breaks is worse than an undocumented
    one, because the next reader trusts it. Now the table above is the code.
    """
    args = ["gh", "api", f"repos/{repo}/contents/.kanban.yml",
            "-H", "Accept: application/vnd.github.raw"]
    if ref and ref != "HEAD":
        args[2] += f"?ref={ref}"
    # ABSENT AND UNFETCHABLE ARE DIFFERENT ANSWERS (Bugbot, .github#295). The first
    # version caught every `gh api` failure and returned an empty map -- identical to
    # "no `.kanban.yml`". So a present override behind a 403, a 5xx or a rate limit was
    # silently ignored and both writers applied the defaults: the exact override-ignore
    # defect this change exists to close, reached by a different door. The parse and
    # missing-`yq` paths already refused; this one did not.
    #
    # 404 is the ONLY failure that means "no override". Matched on the message the way
    # promote-repo.sh does it, because `gh` exits non-zero for both.
    try:
        raw = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or "")
        if "Not Found" in err or "404" in err:
            return {}
        sys.stderr.write(f"::error::.kanban.yml in {repo} could not be fetched "
                         f"({err.strip()[:200]}). Refusing rather than applying the "
                         "default mapping to a repo that may have an override.\n")
        raise SystemExit(1) from None
    except OSError as exc:
        sys.stderr.write(f"::error::could not run `gh` to read .kanban.yml in {repo} "
                         f"({exc}). Refusing rather than guessing.\n")
        raise SystemExit(1) from None
    if not raw.strip():
        return {}
    # PARSED BY `yq`, NOT PyYAML (Bugbot, .github#295). The first version imported
    # `yaml` lazily and treated ImportError exactly like a parse miss -- a warning and
    # an empty override. Neither rewired workflow installs PyYAML, so on the runner a
    # real `.kanban.yml` would have been silently ignored and both writers would have
    # kept the defaults: the override this whole change exists to honour, failing open
    # in the one environment that matters.
    #
    # `yq` is what `advance-deploy-env.yml` used before this refactor, so it is a
    # dependency this path already had rather than a new one.
    try:
        proc = subprocess.run(["yq", "-o=json", ".branch_status_map // {}"],
                              input=raw, capture_output=True, text=True, check=True)
        doc = json.loads(proc.stdout or "{}")
    except FileNotFoundError:
        # CANNOT TELL, LOUDLY. A present `.kanban.yml` and no parser is not "no
        # override" -- it is an unread override, and quietly defaulting is how the
        # first version failed. Refuse so somebody fixes the runner.
        sys.stderr.write("::error::.kanban.yml is present but `yq` is not installed, "
                         "so the override cannot be read. Refusing rather than "
                         "silently applying the default mapping.\n")
        raise SystemExit(1) from None
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"::error::.kanban.yml in {repo} did not parse ({exc}); "
                         "refusing rather than applying part of it\n")
        raise SystemExit(1) from None
    # REFUSE, DO NOT IGNORE. A `branch_status_map` that parses and is a list, a
    # string or a number is a MISTYPED override in a file somebody wrote on purpose --
    # the one shape where "treat it as absent" is certainly wrong.
    if not isinstance(doc, dict):
        sys.stderr.write(
            f"::error::.kanban.yml in {repo} has a `branch_status_map` that is "
            f"{type(doc).__name__}, not a mapping; refusing rather than ignoring "
            "a file that was fetched and parsed\n")
        raise SystemExit(1)
    return doc


def main(argv: list[str]) -> int:
    """CLI. `--no-override` answers from `DEFAULT_MAP` alone, consulting nothing.

    THE POLICY ON AN UNREADABLE OVERRIDE BELONGS TO THE CALLER, because the cost of
    not writing differs per caller and this module cannot know it (Bugbot,
    .github#295):

      kanban-reconcile  SKIPS the item. It is a backstop that fixes MISSES, so
                        writing a guessed column would overrule a router that
                        already got it right.
      kanban-closure-router  writes the non-terminal HOLDING STATE and labels the
                        card. Publishing nothing leaves the project's built-in
                        "Item closed" automation to set `Cancelled` and archive
                        shipped-via-parent work (.github#157) -- strictly worse
                        than parking the card for the weekly pass.
      advance-deploy-env  nothing. A push has no competing automation, so a failed
                        run is a failed run and the card keeps what it had.

    THE SAME THREE POLICIES NOW COVER `resolve`'s REFUSAL TOO (backend#2324), and
    that is the fix: an override whose Status does not exist reaches each caller as a
    non-zero exit, which is the one thing all three already handle. It used to reach
    them as a successful answer, so none of them handled it.

    So `read_override` and `resolve` both refuse, and `--no-override` is how a caller
    asks for the answer it can safely fall back to. Neither caller gets a silent
    default.
    """
    if len(argv) < 2:
        sys.stderr.write("usage: branch_status_map.py <branch> [owner/repo] [ref]\n"
                         "       branch_status_map.py <branch> --no-override\n")
        return 2
    branch = argv[1]
    if "--no-override" in argv[2:]:
        override: dict = {}
    else:
        repo = argv[2] if len(argv) > 2 else os.environ.get("GITHUB_REPOSITORY", "")
        ref = argv[3] if len(argv) > 3 else "HEAD"
        override = read_override(repo, ref) if repo else {}
    status, env = resolve(branch, override)
    if override.get(branch):
        sys.stderr.write(f"::notice::.kanban.yml overrides {branch} -> {status}\n")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"status_name={status}\nenv={env}\n")
    print(json.dumps({"status": status, "env": env}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
