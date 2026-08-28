#!/usr/bin/env python3
"""Exercise scripts/version_file.py against every format the GATE declares.

WHY THIS FILE EXISTS (backend#2758)

`version_file.py` writes the version; `version-bump-gate.yml` reads it. They are
two implementations of the same five format rules, and the day they disagree the
bump lands somewhere the gate does not look -- a green PR that did not bump, or a
refusal on a file that did. So the last block here does not test the script at
all: it reads the SUFFIX LIST OUT OF THE GATE and asserts the script accepts
exactly that set, so a format added to one and not the other is a failure rather
than a silent divergence.

Everything else is the writer's own contract: the five formats round-trip, each
one's decoy value is not mistaken for the version, and every unanswerable case
exits non-zero rather than guessing.
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCRIPT = REPO / "scripts" / "version_file.py"
GATE = REPO / ".github" / "workflows" / "version-bump-gate.yml"

RESULTS = []


def record(ok, name, detail=""):
    RESULTS.append((ok, name, detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"\n         {detail}" if not ok else ""))


def run(args, stdin=""):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          input=stdin, capture_output=True, text=True)


# --- the five formats round-trip, decoys and all -----------------------------
# Every fixture carries the trap its format actually has, taken from the gate's
# own header: a leading comment, a dependency pin, appVersion, a schema constant.
CASES = [
    ("VERSION", "# bumped from 9.9.9 by hand\n0.10.3\n", "0.10.3", "0.10.4",
     "a leading comment is documentation, not the version (backend#1427)"),
    ("package.json", '{"name":"x","dependencies":{"react":"18.3.1"},"version":"1.4.0"}',
     "1.4.0", "1.4.1", "a dependency pin is not the version"),
    ("client/Chart.yaml", 'apiVersion: v2\nappVersion: "1.16.0"\nversion: 0.4.2\n',
     "0.4.2", "0.4.3", "appVersion is not the version"),
    ("pyproject.toml", '[project]\nname = "x"\nversion = "1.0.0"\n',
     "1.0.0", "1.0.1", "the [project] version"),
    ("tracebloc_ingestor/__init__.py", "SCHEMA = '9.9.9'\n__version__ = '0.8.13'\n",
     "0.8.13", "0.8.14", "a schema constant is not the version"),
]

print("round-trip: read -> bump -> write -> read, for every declared format")
for path, text, want, nxt, why in CASES:
    r = run(["read", "--file", path], text)
    got = r.stdout.strip()
    record(got == want, f"{path}: reads {want} ({why})", f"got {got!r} rc={r.returncode} {r.stderr.strip()[:80]}")
    w = run(["write", "--file", path, "--to", nxt], text)
    back = run(["read", "--file", path], w.stdout).stdout.strip()
    record(w.returncode == 0 and back == nxt,
           f"{path}: writes {nxt} and reads it back",
           f"rc={w.returncode} back={back!r} {w.stderr.strip()[:80]}")
    # The decoy must survive untouched: a writer that rewrote appVersion or a
    # dependency pin would still "read back" correctly and be badly wrong.
    for decoy in ("9.9.9", "18.3.1", "1.16.0"):
        if decoy in text:
            record(decoy in w.stdout,
                   f"{path}: leaves the decoy {decoy} alone",
                   "the decoy was rewritten")

print()
print("fail closed - every unanswerable case exits non-zero")
for name, args, stdin in [
    ("an unknown extension is refused", ["read", "--file", "notes.md"], "1.0.0\n"),
    ("a file with no version is refused", ["read", "--file", "pyproject.toml"], "[project]\nname='x'\n"),
    ("invalid JSON is refused", ["read", "--file", "package.json"], "{not json"),
    ("a JSON file with no .version is refused", ["read", "--file", "package.json"], '{"name":"x"}'),
    ("an extensionless non-VERSION file is refused", ["read", "--file", "Makefile"], "1.0.0\n"),
]:
    r = run(args, stdin)
    record(r.returncode != 0, name, f"rc={r.returncode} (wanted non-zero)")

# THE READ-BACK IS LOAD-BEARING, and this is the case that proves it. A NESTED
# `version` occurring before the top-level one makes the count=1 substitution
# replace the wrong occurrence: `subn` reports 1 match and is happy, while the
# manifest's real version is untouched. Only re-reading catches it. Without this
# case the verification is an inert guard -- a mutation removing it left the
# suite green until this fixture existed.
r = run(["write", "--file", "package.json", "--to", "1.0.1"],
        '{"packages":{"a":{"version":"1.0.0"}},"version":"1.0.0"}')
record(r.returncode != 0 and "reading it back" in r.stderr,
       "a nested version ahead of the real one is REFUSED, not silently mis-written",
       f"rc={r.returncode} stderr={r.stderr.strip()[:110]!r}")

for name, val in [("a prerelease cannot be bumped", "1.4.0-rc.1"),
                  ("a two-part version cannot be bumped", "1.4"),
                  ("a non-semver string cannot be bumped", "latest")]:
    r = run(["bump", "--value", val])
    record(r.returncode != 0, name, f"rc={r.returncode} out={r.stdout.strip()!r}")

for part, want in (("patch", "1.4.1"), ("minor", "1.5.0"), ("major", "2.0.0")):
    r = run(["bump", "--value", "1.4.0", "--part", part])
    record(r.stdout.strip() == want, f"bump --part {part} -> {want}", f"got {r.stdout.strip()!r}")

print()
print("--in-place - the ONLY form post-release-bump.yml uses")
# The workflow never uses the stdin form, so testing only that would leave the
# release path uncovered. Every format is round-tripped on a real file on disk.
for path, text, want, nxt, _why in CASES:
    d = tempfile.mkdtemp()
    f = os.path.join(d, os.path.basename(path))
    with open(f, "w") as fh:
        fh.write(text)
    r = subprocess.run([sys.executable, str(SCRIPT), "read", "--file", f, "--in-place"],
                       capture_output=True, text=True)
    record(r.stdout.strip() == want, f"--in-place read: {os.path.basename(path)} -> {want}",
           f"got {r.stdout.strip()!r} {r.stderr.strip()[:70]}")
    w = subprocess.run([sys.executable, str(SCRIPT), "write", "--file", f, "--to", nxt, "--in-place"],
                       capture_output=True, text=True)
    on_disk = open(f).read()
    back = subprocess.run([sys.executable, str(SCRIPT), "read", "--file", f, "--in-place"],
                          capture_output=True, text=True).stdout.strip()
    record(w.returncode == 0 and back == nxt and text != on_disk,
           f"--in-place write: {os.path.basename(path)} -> {nxt} on disk",
           f"rc={w.returncode} back={back!r} changed={text != on_disk}")

r = subprocess.run([sys.executable, str(SCRIPT), "bump", "--value", "1.0.0", "--in-place"],
                   capture_output=True, text=True)
record(r.returncode != 0, "--in-place is refused for bump", f"rc={r.returncode}")

print()
print("agreement with the gate - the format list is DERIVED, not restated")
# The gate declares its parses in its own header, one line per suffix. Reading
# that list here is what makes a format added to one and not the other fail.
gate = GATE.read_text()
declared = set()
for suf in re.findall(r"^#\s+(\*\.\w+(?:\|\*\.\w+)*|VERSION)\s{2,}", gate, re.M):
    for part in suf.split("|"):
        declared.add(part.replace("*.", "").lower())
record(bool(declared), "the gate's declared suffix list was located",
       f"parsed {declared!r} - if empty, this check is vacuous and must be fixed")
if declared:
    probe = {"json": "package.json", "yaml": "a.yaml", "yml": "a.yml",
             "toml": "a.toml", "py": "a.py", "version": "VERSION"}
    unknown = []
    for suf in sorted(declared):
        f = probe.get(suf)
        if not f:
            unknown.append(suf)
            continue
        # `read` on an empty body fails for a content reason, never "no parse
        # rule" -- that message is the one that means the format is unsupported.
        r = run(["read", "--file", f], "")
        if "no parse rule" in (r.stderr or ""):
            unknown.append(suf)
    record(not unknown,
           f"the script handles every format the gate declares ({len(declared)})",
           f"unsupported by version_file.py: {unknown}")

failed = [r for r in RESULTS if not r[0]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
