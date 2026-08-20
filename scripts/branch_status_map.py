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

    FETCHED, NOT READ OFF DISK, because the router never checks the repo out -- and
    a closed PR's branch may be gone by the time this runs, so the default ref is
    the repo's own default branch rather than the merged head.

    An unreadable or absent file is an EMPTY override, not an error: the file is
    optional by design and no repo has one. A malformed one is also empty, and says
    so on stderr -- silently applying half a parsed map would be worse than
    ignoring it.
    """
    args = ["gh", "api", f"repos/{repo}/contents/.kanban.yml",
            "-H", "Accept: application/vnd.github.raw"]
    if ref and ref != "HEAD":
        args[2] += f"?ref={ref}"
    try:
        raw = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return {}
    if not raw.strip():
        return {}
    try:
        import yaml  # type: ignore
        doc = yaml.safe_load(raw)
    except Exception as exc:                                  # noqa: BLE001
        sys.stderr.write(f"::warning::.kanban.yml in {repo} did not parse ({exc}); "
                         "ignoring the override rather than applying part of it\n")
        return {}
    if not isinstance(doc, dict):
        return {}
    got = doc.get("branch_status_map")
    return got if isinstance(got, dict) else {}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: branch_status_map.py <branch> [owner/repo] [ref]\n")
        return 2
    branch = argv[1]
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
