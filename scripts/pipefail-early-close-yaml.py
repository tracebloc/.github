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
import re
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

    # AN UNRESOLVED EXPRESSION IS CHECKED FIRST, and before the command-line
    # branch, because `shell: ${{ matrix.shell }} {0}` would otherwise be read
    # as a command whose program is `${{` -- not a POSIX shell -- and skipped.
    #
    # GitHub evaluates `${{ }}` BEFORE choosing the shell, so an expression
    # resolving to `bash` runs with errexit AND pipefail. The scanner only ever
    # sees the literal text, so what runs cannot be determined here and the one
    # safe answer is the one that still reports (Bugbot, .github#404). Before
    # this, such a step fell through to the default `-e` -- errexit only, no
    # pipefail -- and a live `| head -1` in it was reported CLEAN: fail-open, in
    # the scan this file exists to add.
    #
    # Arming both is the same trade the wrapper makes for basename-matched
    # `source` targets: the worst case is a spurious finding someone silences
    # with `# pipefail-guard: allow`, where the other direction is silent.
    if "${{" in spec:
        return "-eo pipefail"

    tokens = spec.split()
    if not tokens:
        return None

    # PEEL WRAPPER PROGRAMS BEFORE ASKING WHAT THE PROGRAM IS (Bugbot,
    # .github#404). `shell: /usr/bin/env bash -eo pipefail {0}` runs bash with
    # pipefail, but `basename(tokens[0])` is `env` -- not a POSIX shell -- so it
    # fell through to the custom-interpreter branch below and was SKIPPED, while
    # the byte-identical `bash -eo pipefail {0}` and `/bin/bash -eo pipefail {0}`
    # both flag. A live `| head -1` in such a step was reported clean: fail-open,
    # in the scan this file exists to add.
    #
    # `env` is the only wrapper worth peeling, and it is the one people actually
    # write. Its own flags and any `VAR=value` assignments sit between it and the
    # real program, so they are stepped over too -- `env` applies them itself,
    # which is what makes this shape reachable at all. (A bare `FOO=bar bash …`
    # is NOT reachable: GitHub execs the spec directly, so there is no shell to
    # apply the assignment, and GitHub refuses the workflow.)
    # `-S` IS NOT A VALUE FLAG, and treating it as one loses the program. `-u
    # NAME` and `-C DIR` take a separate argument; `-S`'s argument is the WHOLE
    # remaining command, so after `.split()` the very next token IS the program.
    # Consuming it skipped `env -S bash -eo pipefail {0}` entirely -- the peel
    # landed on `pipefail` as the program -- which is the same fail-open shape
    # the peel was added to close.
    _VALUE_FLAGS = ("-u", "--unset", "-C", "--chdir")
    i = 0
    while i < len(tokens) and os.path.basename(tokens[i]) == "env":
        i += 1
        while i < len(tokens) and (
            tokens[i].startswith("-")
            or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i])
        ):
            i += 2 if tokens[i] in _VALUE_FLAGS else 1
    # Peeling everything leaves no program to judge. Fall through armed rather
    # than skipped -- the same direction every other "cannot tell" takes here.
    if i >= len(tokens):
        return "-eo pipefail"
    tokens = tokens[i:]

    program = os.path.basename(tokens[0])

    # A COMMAND LINE, whose flags are whatever is written -- GitHub uses it
    # verbatim and implies no `-e`. The `{0}` is NOT required to reach this
    # branch: `shell: bash -eo pipefail` with the template omitted is the same
    # invocation as `bash -eo pipefail {0}`, and treating it as "unrecognised"
    # dropped its pipefail.
    if program in POSIX_SHELL_PROGRAMS:
        kept = []
        for tok in tokens[1:]:
            if tok == "{0}":
                break
            kept.append(tok)
        return " ".join(kept)

    # A CUSTOM INTERPRETER (`perl {0}`, `node {0}`) is not a shell and has no
    # pipefail, exactly like the `python`/`pwsh` keywords above. The `{0}` is
    # what marks it as a real command rather than a typo.
    if "{0}" in spec:
        return None

    # A bare word that is neither a keyword nor a program: a workflow GitHub
    # itself refuses to run. Armed rather than skipped, so the fail-open
    # direction is never the default, and it costs nothing real.
    return "-eo pipefail"


def _scalar(node):
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _mapping_get(node, key):
    """The value node for `key`, or None. Mapping nodes only.

    Honours YAML `<<:` merge keys. `yaml.compose` already resolves ALIASES to
    the anchored node -- an aliased step (`- *anchor`) arrives here as a real
    MappingNode and is scanned normally -- but it does NOT expand merges, so a
    step whose `run:`/`shell:` lives only on a `<<: *anchor` merge would be read
    as if it had neither, and the gate would report a verdict without scanning
    that block (Bugbot #404, the vacuous-success hole this file closes).

    A directly-present key wins over any merged one, and within a sequence merge
    (`<<: [*a, *b]`) earlier entries win -- both per the YAML merge-key spec.
    """
    if not isinstance(node, yaml.MappingNode):
        return None
    # EVERY `<<:`, NOT THE LAST ONE. This kept a single `merge`, overwriting it
    # on each merge key, so with two of them the FIRST was never searched -- and
    # a `run:`/`shell:`/`defaults:`/`steps:` arriving only through it read as
    # absent, letting the gate reach a clean verdict without scanning the block
    # (Bugbot Medium, .github#404). `_mapping_items` already collected all of
    # them, so the two helpers disagreed about the same mapping: the shape this
    # file keeps finding, here between two functions that must not differ.
    merges = []
    for k, v in node.value:
        if _scalar(k) == key:
            return v  # a direct key always wins over a merged one
        if _scalar(k) == "<<":
            merges.append(v)
    # Document order, matching `_mapping_items`: within a sequence merge
    # (`<<: [*a, *b]`) earlier entries win, and the same holds across repeated
    # merge keys. compose has resolved each alias to its anchored MappingNode.
    for merge in merges:
        sources = merge.value if isinstance(merge, yaml.SequenceNode) else [merge]
        for src in sources:
            found = _mapping_get(src, key)
            if found is not None:
                return found
    return None


def _mapping_items(node):
    """Every (key, value) of a mapping, INCLUDING entries merged in via `<<:`.

    `_mapping_get` was made merge-aware for a step's `run:`/`shell:` (Bugbot
    #404), which fixed the instance. THIS is the rest of the class: `jobs:` is
    ITERATED rather than looked up, so a job arriving via `jobs: <<: *tpl` was
    still skipped entirely -- measured, rc 0 with no finding, where the same job
    written directly is flagged. One level up, same fail-open, and the reason to
    route every mapping read through one of these two functions.

    Precedence is `_mapping_get`'s, because it is the same rule: a direct key
    shadows a merged one, and within a sequence merge earlier entries win.
    """
    if not isinstance(node, yaml.MappingNode):
        return []
    out, seen, merges = [], set(), []
    for k, v in node.value:
        name = _scalar(k)
        if name == "<<":
            merges.append(v)
            continue
        if name is not None and name not in seen:
            seen.add(name)
            out.append((name, v))
    for merge in merges:
        sources = merge.value if isinstance(merge, yaml.SequenceNode) else [merge]
        for src in sources:
            for name, v in _mapping_items(src):
                if name not in seen:
                    seen.add(name)
                    out.append((name, v))
    return out


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
        # `_mapping_items`, not `jobs.value` -- a job merged in via
        # `jobs: <<: *tpl` is otherwise never visited at all.
        for _name, job in _mapping_items(jobs):
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
