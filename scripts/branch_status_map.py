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
}


def resolve(branch: str, override: dict | None = None) -> tuple[str, str]:
    """(Status, Deploy environment) for `branch`, honouring a `.kanban.yml` override.

    An override naming a Status this file does not know keeps the DEFAULT
    environment rather than inventing one -- the Status is the operator's call, the
    environment is a derived fact, and deriving it from an unknown is a guess.
    """
    status, env = DEFAULT_MAP.get(branch, ("", ""))
    if not override:
        return status, env
    want = override.get(branch)
    if not want or not isinstance(want, str):
        return status, env
    return want, ENV_FOR_STATUS.get(want, env)


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
      kanban-closure-router  writes the DEFAULT. Publishing nothing leaves the
                        project's built-in "Item closed" automation to set
                        `Cancelled` and archive shipped-via-parent work
                        (.github#157) -- strictly worse than ignoring an override
                        for one run.

    So `read_override` still refuses, and `--no-override` is how a caller asks for
    the answer it can safely fall back to. Neither caller gets a silent default.
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
