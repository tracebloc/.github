#!/usr/bin/env python3
"""Explode the `run:` blocks of workflow / composite-action YAML into shell
fragments, so the early-close scanner can read them (backend#2967).

WHY THIS EXISTS
---------------
`pipefail-early-close.sh` classifies a file as shell by extension, else by
shebang -- deliberately the same rule the `shellcheck` job applies. Workflow
YAML has neither. So every `run:` block in the fleet was OUT OF SCOPE, and the
gate reported success on `tracebloc/e2e-test-agent@f4d6fec`, whose
`.github/workflows/journey-tier-a.yml:2014` carried

    CM=$(printf '%s\\n' "$CM_RAW" | head -1)

in a step declaring `shell: bash`. Measured: handed that file explicitly, the
scanner flags line 2014 correctly. The LINE GRAMMAR was never the hole -- the
awk was simply never handed the file. That is worth stating plainly, because
the ticket suspected a narrower matcher (`head -1` missed where `head -n1` is
caught) and that suspicion is FALSE; all four `head` spellings were already
matched, and are pinned as cases in the selftest so the claim stays measured.

THIS FILE MATCHES NO HAZARDS. It resolves scope and nothing else. The rule for
what counts as an offending LINE lives in `pipefail-early-close.awk` and is
called, never copied (backend#1729 rule 9): a second matcher here would drift
from the awk and then go on proving that a regex nothing runs would have caught
the bug -- twice in one day in this org already.

THE OPTIONS ARE NOT COPIED EITHER, and that is the load-bearing trick. A run
block's errexit/pipefail state does not come from a `set` line in the body; it
comes from the shell GitHub invokes. So this file SYNTHESISES the equivalent
`set` line and prepends it to the fragment, and the awk's own `apply_set` --
already mutation-proofed for the long forms, the split forms, the signs and the
trailing comments -- parses it. There is no second option parser to drift.

    shell: bash        ->  bash --noprofile --norc -eo pipefail {0}  ->  set -eo pipefail
    shell: sh          ->  sh -e {0}                                 ->  set -e
    (no shell:)        ->  bash -e {0}   (falls back to sh -e {0})   ->  set -e
    shell: bash -x {0} ->  used VERBATIM by GitHub, so -e is NOT added -> set -x

That table is GitHub's documented contract, and the last row is why the custom
form must have its flags PARSED rather than assumed: a custom command line gets
no implicit `-e`, so treating every `bash …` as errexit would invent hazards.
The distinction is asserted in the selftest in both directions.

`shell: python` / `pwsh` / `powershell` / `cmd` are not POSIX shells and carry
no pipefail; they are skipped, exactly as `.ps1` is skipped by the wrapper's
classifier.

LINE NUMBERS COME FROM THE RAW SOURCE, not from the parsed scalar. A folded
(`>`) scalar joins lines, and a parsed literal block is dedented by an amount
the parser does not report -- either way the offsets stop matching the file a
reviewer opens. So the block's raw source lines are taken from the node's
start/end marks and dedented here, which keeps the mapping exactly 1:1 and
makes the annotation land on the real line.

FAILS CLOSED (backend#1729 rule 3). Unparseable YAML, an unreadable file and a
missing PyYAML all exit 2 -- "cannot tell" is a finding, never a pass. A file
that parses and simply holds no run blocks contributes nothing, which is a
legitimate silence: nothing in scope is not the same as nothing checked.

Usage:  pipefail-early-close-yaml.py --out DIR FILE...
Writes: DIR/<n>.frag           one synthetic `set` line + the dedented body
        DIR/manifest.tsv       frag <TAB> real path <TAB> body first line (1-based)
Exit:   0 wrote a manifest (possibly empty) · 2 cannot tell
"""
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - exercised by the wrapper's rc-2 path
    sys.stderr.write("pipefail-early-close-yaml: PyYAML is not installed — "
                     "refusing to report clean\n")
    raise SystemExit(2)

# GitHub's documented keyword shells, mapped to the flags it actually invokes.
# DERIVED FROM THE DECLARATION, never from a guess about the step's intent: the
# whole point of the f4d6fec case is that exactly one step in that file said
# `shell: bash`, and that is the step whose `| head -1` could abort.
KEYWORD_FLAGS = {
    "bash": "-eo pipefail",
    "sh": "-e",
}
# Not POSIX shells: no pipefail, so no member of this class.
NON_SHELL = {"python", "pwsh", "powershell", "cmd"}
# The default when a step declares no shell at all: `bash -e {0}`, falling back
# to `sh -e {0}`. Both are errexit-only -- NO pipefail. That asymmetry is the
# reason the f4d6fec step had to opt in with `shell: bash` to become hazardous.
DEFAULT_FLAGS = "-e"

POSIX_SHELL_PROGRAMS = {"bash", "sh", "dash", "ksh", "zsh"}


def flags_for_shell(shell):
    """The `set` flags equivalent to the shell GitHub will invoke.

    Returns None when the step does not run a POSIX shell at all.
    """
    if shell is None:
        return DEFAULT_FLAGS
    if not isinstance(shell, str):
        return None
    spec = shell.strip()
    if spec in KEYWORD_FLAGS:
        return KEYWORD_FLAGS[spec]
    if spec in NON_SHELL:
        return None
    if "{0}" not in spec:
        # An unrecognised keyword. Fail CLOSED on scope: treat it as the
        # default shell rather than silently dropping the block. The worst case
        # is a finding someone can silence with the marker; the other direction
        # is the hole this file exists to close.
        return DEFAULT_FLAGS
    # A custom command line, used VERBATIM by GitHub -- so its flags are
    # whatever is written, and no `-e` is implied.
    tokens = spec.split()
    if not tokens:
        return None
    program = os.path.basename(tokens[0])
    if program not in POSIX_SHELL_PROGRAMS:
        return None
    kept = []
    for tok in tokens[1:]:
        if tok == "{0}":
            break
        kept.append(tok)
    return " ".join(kept)


def _scalar(node):
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _mapping_get(node, key):
    """The value node for `key`, or None. Mapping nodes only."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if _scalar(k) == key:
            return v
    return None


def _defaults_shell(node):
    """`defaults: run: shell:` on a workflow or job node, as a string or None."""
    run = _mapping_get(_mapping_get(node, "defaults"), "run")
    return _scalar(_mapping_get(run, "shell"))


def collect_steps(root):
    """Every step node, paired with the shell defaulting that applies to it.

    Precedence is GitHub's: step `shell:`, else the job's `defaults.run.shell`,
    else the workflow's. A composite action (`runs.using: composite`) keeps its
    steps under `runs.steps` and has no defaults layer -- there, `shell:` is
    required per step, so the fallback simply never applies.
    """
    out = []
    workflow_default = _defaults_shell(root)

    jobs = _mapping_get(root, "jobs")
    if isinstance(jobs, yaml.MappingNode):
        for _name, job in jobs.value:
            job_default = _defaults_shell(job) or workflow_default
            steps = _mapping_get(job, "steps")
            if isinstance(steps, yaml.SequenceNode):
                for step in steps.value:
                    out.append((step, job_default))

    runs = _mapping_get(root, "runs")
    if isinstance(runs, yaml.MappingNode):
        steps = _mapping_get(runs, "steps")
        if isinstance(steps, yaml.SequenceNode):
            for step in steps.value:
                out.append((step, workflow_default))
    return out


def block_body(src_lines, node):
    """The block's RAW source lines, dedented, plus its first line (1-based).

    Taken from the marks rather than from `node.value` so the offsets keep
    pointing at the file a reviewer opens -- see the module docstring.
    """
    first = node.start_mark.line          # 0-based; the `run: |` line itself
    last = node.end_mark.line             # 0-based, exclusive-ish
    raw = src_lines[first:last + 1]
    if not raw:
        return [], first + 1
    # Drop the `run:` line when the body starts underneath it (a block scalar).
    # A single-line `run: echo hi` has its content ON that line, and the scalar
    # value is what to scan there.
    style = node.style
    if style in ("|", ">"):
        body = raw[1:]
        body_first = first + 2            # 1-based line of the body's line 1
    else:
        body = [_scalar(node) or ""]
        body_first = first + 1
    # Dedent by the common leading whitespace of the non-blank lines, so the
    # awk's `^[[:space:]]*set` and function-opener anchors behave as they do in
    # a real script.
    indents = [len(ln) - len(ln.lstrip()) for ln in body if ln.strip()]
    cut = min(indents) if indents else 0
    return [ln[cut:] if len(ln) >= cut else ln for ln in body], body_first


def main(argv):
    if "--out" not in argv:
        sys.stderr.write("pipefail-early-close-yaml: --out DIR is required\n")
        return 2
    i = argv.index("--out")
    out_dir = argv[i + 1]
    files = argv[:i] + argv[i + 2:]

    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    n = 0

    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except OSError as exc:
            sys.stderr.write(f"pipefail-early-close-yaml: cannot read {path}: {exc} — "
                             "refusing to report clean\n")
            return 2
        try:
            root = yaml.compose(src)
        except yaml.YAMLError as exc:
            first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            sys.stderr.write(f"pipefail-early-close-yaml: cannot parse {path}: {first} — "
                             "refusing to report clean\n")
            return 2
        if root is None:
            continue
        src_lines = src.splitlines()

        for step, inherited in collect_steps(root):
            run = _mapping_get(step, "run")
            if run is None:
                continue
            shell = _scalar(_mapping_get(step, "shell"))
            flags = flags_for_shell(shell if shell is not None else inherited)
            if flags is None:
                continue
            body, body_first = block_body(src_lines, run)
            if not body:
                continue
            n += 1
            frag = os.path.join(out_dir, f"{n}.frag")
            with open(frag, "w", encoding="utf-8") as fh:
                # Line 1 of the fragment: the options GitHub's own invocation
                # establishes. The awk's apply_set parses it -- this file does
                # not decide what `-eo pipefail` means.
                fh.write(f"set {flags}\n" if flags.strip() else "set --\n")
                fh.write("\n".join(body) + "\n")
            manifest.append(f"{frag}\t{path}\t{body_first}")

    with open(os.path.join(out_dir, "manifest.tsv"), "w", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
