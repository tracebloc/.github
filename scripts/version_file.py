#!/usr/bin/env python3
"""Read and WRITE the version out of a repo's declared `version_file`.

WHY THIS EXISTS (backend#2758)

Shipping a tag-published repo creates the exact condition that blocks its next
promotion: the version it just released is now taken, so the next PR touching a
published file is refused by `version-bump-gate`, and the next hop is refused by
the train's own preflight. Nothing bumps the file afterwards, so the repo goes
quietly unpromotable until a human reads a refusal mid-hop -- the most expensive
moment to find out. Measured: `cli` needed the identical one-line bump on two
consecutive days (2026-08-24, 08-25) and `design-system` twice (v1.3.0, v1.4.0).

`post-release-bump.yml` closes that loop by opening the bump PR at the moment the
version is known -- immediately after the release that consumed it.

WHY A SCRIPT RATHER THAN INLINE SHELL

`version-bump-gate.yml` already knows how to READ these five formats, inline. A
second inline copy in the bump workflow would be a restatement free to drift from
it, which is the defect class this repo keeps closing. This file is the one place
the format rules live; the gate can be moved onto it later without changing
behaviour, and `scripts/tests/version-file-selftest.py` pins the two against each
other by round-tripping every format the gate declares.

THE FORMATS, and the trap each one carries -- all five taken from the gate's own
header so the rules cannot silently disagree:

  VERSION        the bare file. A LEADING COMMENT must not win (backend#1427),
                 so the first non-comment, non-blank line is the version.
  *.json         `.version`, parsed as JSON -- never a dependency pin that
                 happens to look like a version.
  *.yaml|*.yml   the first LINE-ANCHORED `^version:`. The anchor is what keeps
                 `appVersion:` out of it (Chart.yaml carries both).
  *.toml         the first `^version *= *"`.
  *.py           the first `^__version__ *= *["']`.

FAIL CLOSED, everywhere. An unknown extension, an unreadable file, a file whose
version cannot be found, or a write that did not land all exit non-zero. A bump
that cannot prove it happened is not a bump.
"""
import argparse
import json
import re
import sys

# (matcher, description) per suffix. Anchored, and the anchor is the rule.
_YAML = re.compile(r'^version:\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))\s*$', re.M)
_TOML = re.compile(r'^version\s*=\s*"([^"]*)"', re.M)
_PY = re.compile(r'^__version__\s*=\s*["\']([^"\']*)["\']', re.M)


def _kind(path: str) -> str:
    low = path.lower()
    if low.endswith(".json"):
        return "json"
    if low.endswith((".yaml", ".yml")):
        return "yaml"
    if low.endswith(".toml"):
        return "toml"
    if low.endswith(".py"):
        return "py"
    # A bare VERSION file has no extension. Anything else with no extension is
    # NOT assumed to be one -- guessing here would silently write a version into
    # a file that holds something else.
    if low.rsplit("/", 1)[-1] == "version":
        return "bare"
    raise SystemExit(
        f"version_file: no parse rule for '{path}'. Known: *.json, *.yaml/*.yml, "
        f"*.toml, *.py, and a bare VERSION file."
    )


def _first_group(m):
    return next(g for g in m.groups() if g is not None)


def read(path: str, text: str) -> str:
    k = _kind(path)
    if k == "json":
        try:
            v = json.loads(text).get("version")
        except json.JSONDecodeError as e:
            raise SystemExit(f"version_file: {path} is not valid JSON ({e})")
        if not isinstance(v, str) or not v:
            raise SystemExit(f"version_file: {path} has no string .version")
        return v
    if k == "bare":
        for line in text.splitlines():
            s = line.strip()
            # The leading-comment trap: a commented version is documentation, not
            # the value (backend#1427).
            if s and not s.startswith("#"):
                return s
        raise SystemExit(f"version_file: {path} has no non-comment version line")
    rx = {"yaml": _YAML, "toml": _TOML, "py": _PY}[k]
    m = rx.search(text)
    if not m:
        raise SystemExit(f"version_file: no version found in {path}")
    return _first_group(m)


def write(path: str, text: str, new: str) -> str:
    """Return `text` with the version replaced by `new`. Never writes to disk."""
    k = _kind(path)
    old = read(path, text)
    if k == "json":
        # Re-serialising would reformat the whole file and produce a diff nobody
        # asked for, so the value is replaced in place on the ORIGINAL bytes.
        out, n = re.subn(
            r'("version"\s*:\s*)"' + re.escape(old) + r'"',
            lambda m: m.group(1) + '"' + new + '"', text, count=1)
    elif k == "bare":
        out, n = re.subn(r'(?m)^\s*' + re.escape(old) + r'\s*$', new, text, count=1)
    elif k == "yaml":
        out, n = re.subn(r'(?m)^(version:\s*)\S.*$',
                         lambda m: m.group(1) + new, text, count=1)
    elif k == "toml":
        out, n = re.subn(r'(?m)^(version\s*=\s*)"[^"]*"',
                         lambda m: m.group(1) + '"' + new + '"', text, count=1)
    else:  # py
        out, n = re.subn(r'(?m)^(__version__\s*=\s*)(["\'])[^"\']*(["\'])',
                         lambda m: m.group(1) + m.group(2) + new + m.group(3),
                         text, count=1)
    if n != 1:
        raise SystemExit(
            f"version_file: refused to write {path} - the replacement matched "
            f"{n} times, not exactly once")
    # PROVE THE WRITE, with the same reader the gate uses. A substitution that
    # ran but landed somewhere unexpected is the failure this catches, and it is
    # why write() re-reads instead of trusting subn's count alone.
    back = read(path, out)
    if back != new:
        raise SystemExit(
            f"version_file: wrote {path} but reading it back gave '{back}', "
            f"not '{new}'")
    return out


def bump(version: str, part: str = "patch") -> str:
    """Next version. Refuses anything that is not a plain X.Y.Z."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not m:
        # A prerelease or a non-semver string is not something to guess at: an rc
        # is not what a release consumed, and a two-part version has no patch.
        raise SystemExit(
            f"version_file: '{version}' is not a plain X.Y.Z, so the next "
            f"version cannot be derived. Bump it by hand.")
    x, y, z = (int(g) for g in m.groups())
    if part == "patch":
        return f"{x}.{y}.{z + 1}"
    if part == "minor":
        return f"{x}.{y + 1}.0"
    if part == "major":
        return f"{x + 1}.0.0"
    raise SystemExit(f"version_file: unknown bump part '{part}'")


def _parts(v: str, what: str):
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", v)
    if not m:
        raise SystemExit(f"version_file: {what} '{v}' is not a plain X.Y.Z")
    return tuple(int(g) for g in m.groups())


def compare(left: str, right: str) -> str:
    """`lt` / `eq` / `gt`, ordered NUMERICALLY (backend#2758).

    String inequality cannot tell "already bumped" from "behind": both are
    `!=`, and treating the second as a green no-op reports success on a state
    nobody expected. 1.10.0 vs 1.9.0 is the other half - lexically 1.10.0 sorts
    FIRST, so a string compare calls a newer version older.
    """
    a, b = _parts(left, "left"), _parts(right, "right")
    return "lt" if a < b else ("gt" if a > b else "eq")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("read", "write", "bump", "cmp"))
    ap.add_argument("--file", required=False, help="path, for its extension")
    ap.add_argument("--part", default="patch")
    ap.add_argument("--to", help="write: the new version")
    ap.add_argument("--value", help="bump: the version to bump")
    ap.add_argument("--in-place", action="store_true",
                    help="use --file as the source: read it from disk (read), or "
                         "read and rewrite it (write), instead of stdin->stdout")
    a = ap.parse_args()
    # Validated BEFORE EVERY early-return: an ignored flag is worse than a refused
    # one, because the caller believes it took effect.
    #
    # IT USED TO SIT BETWEEN THE TWO RETURNS (Bugbot), which covered `bump` and
    # let `cmp --in-place` sail past the arm that names it -- so the `"cmp"` in
    # this very tuple was unreachable, and the refusal text said only "bump". The
    # same ignored-flag defect this change closed for bump, reintroduced one line
    # above the fix.
    #
    # The message names `a.mode` rather than restating a mode list, so it cannot
    # go stale the way the hardcoded "bump" did.
    if a.in_place and a.mode in ("bump", "cmp"):
        raise SystemExit(f"version_file: --in-place is not valid for {a.mode}")
    if a.mode == "cmp":
        print(compare(a.value, a.to))
        return 0
    if a.mode == "bump":
        print(bump(a.value, a.part))
        return 0
    # --in-place keeps the read and the write inside ONE process. The shell form
    # (`< file > tmp`) is the SC2094 shape and a reader cannot prove the two paths
    # differ, so the workflow that depends on this uses --in-place instead.
    if a.in_place:
        with open(a.file) as fh:
            text = fh.read()
        if a.mode == "read":
            print(read(a.file, text))
            return 0
        out = write(a.file, text, a.to)
        with open(a.file, "w") as fh:
            fh.write(out)
        return 0
    text = sys.stdin.read()
    if a.mode == "read":
        print(read(a.file, text))
    else:
        sys.stdout.write(write(a.file, text, a.to))
    return 0


if __name__ == "__main__":
    sys.exit(main())
