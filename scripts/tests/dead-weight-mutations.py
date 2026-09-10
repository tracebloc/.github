#!/usr/bin/env python3
"""Mutation harness for scripts/dead-weight.py (RFC-0087 D3, backend#3523).

`dead-weight-selftest.py` asserts the checker's behaviour; this asserts the
SELFTEST. Each mutation removes one token's teeth in the real script -- the
dotted-prefix match, the three-word reason, the same-line justification, the
GPU exclusion, the command-position test -- re-runs the real suite against the
mutated file, and expects at least one case to redden. A mutation the suite
survives means the token is not load-bearing in any assertion, which is the
vacuous-guard shape RFC-0087 exists to end (workspace rule 5).

THE MUTATION CALLS THE CODE UNDER TEST (rule 9): it edits scripts/dead-weight.py
on disk and the suite loads that file by path. No rule is re-implemented here.

EVERY ANCHOR MUST MATCH EXACTLY ONCE -- twice mutates an arbitrary one, zero is
stale, and both fail the run exactly like an uncaught mutation. That is the
assertion that the mutation ACTUALLY APPLIED.

  dead-weight-mutations.py          run them all
  dead-weight-mutations.py --dry    resolve anchors only
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "dead-weight.py"
SUITE = ROOT / "scripts" / "tests" / "dead-weight-selftest.py"

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_baseline  # noqa: E402


# (label, old, new)
MUTATIONS = [
    ("imports stop being consulted -- every pin reads as imported",
     '        if module in reached or module.replace("_", "-") in reached:\n            continue',
     '        if True:\n            continue'),

    ("an `indirect-use` line no longer needs a reason",
     "                if not dist or len(reason.split()) < 3:",
     "                if not dist:"),

    ("a `# dead-weight:` pragma no longer needs a reason",
     "            if len(d.pragma.split()) < 3:",
     "            if False:"),

    ("a stale `indirect-use` entry is no longer reported",
     "        if n not in declared:",
     "        if False:"),

    ("the dotted-prefix match collapses to the root: `import azure.identity` vouches for azure-servicebus",
     "                    reached |= _dotted_prefixes(alias.name)",
     '                    reached |= _dotted_prefixes(alias.name.split(".")[0])'),

    ("a relative import vouches for a same-named pin",
     "                if node.level == 0 and node.module:",
     "                if node.module:"),

    ("any bare identifier string counts as a dynamic import (dict keys become imports)",
     '                if 1 < len(v) <= 160 and DOTTED.match(v) and ("." in v or v in registry_strings):',
     "                if 1 < len(v) <= 160 and DOTTED.match(v):"),

    ("a tool name inside a path segment counts as an invocation",
     '        if re.search(r"(?<![\\w@/.-])%s(?![\\w/.-])" % re.escape(n), text):',
     "        if n in text:"),

    ("a tool name anywhere in a string literal counts as a command",
     '            if re.search(r"(?:^|[\\s;&|(`\'\\"])%s(?=$|[\\s;&|)\'\\"])" % re.escape(n), cmd):',
     "            if n in cmd:"),

    ("a pytest plugin passes without the option it adds ever being set",
     "    return any(rx.search(repo.text(rel)) for rel in repo.glob(*globs))",
     "    return True"),

    ("a peer-only package passes without its requirer",
     "        if any(node_used(p) and p in declared_node for p in PEER_OF.get(d.dist, ())):",
     "        if d.dist in PEER_OF:"),

    ("an implicit tool passes without the files that imply it",
     "    return bool(globs) and bool(repo.glob(*globs))",
     "    return bool(globs)"),

    ("an eslint config passes without being extended",
     '    text = "\\n".join(chunks)',
     '    return True\n    text = "\\n".join(chunks)'),

    ("@types/* stop being checked at all",
     '        if d.dist.startswith("@types/"):',
     "        if False:"),

    ("the full-base rule accepts every tag",
     "            if SMALL_BASE_TAG.search(tag):",
     "            if True:"),

    ("a two-word comment justifies a full base",
     "            if len(comment.split()) >= 3:",
     "            if comment:"),

    ("an unresolved ARG tag is silently accepted",
     "            if unresolved:\n                # Cannot tell slim from full",
     "            if False:\n                # Cannot tell slim from full"),

    ("torch pins are never judged",
     '        if "+cpu" in raw:\n            continue',
     "        if True:\n            continue"),

    ("a file-level CPU or GPU index is ignored",
     '        if _file_names_index(repo.text(rel)) in ("cpu", "gpu"):\n            continue',
     "        if False:\n            continue"),

    ("a GPU installer no longer clears the pin",
     "            if gpu:\n                continue\n            if CPU_INDEX.search(cmd)",
     "            if False:\n                continue\n            if CPU_INDEX.search(cmd)"),

    ("a *_cuda requirements file is judged like a CPU one",
     "        if GPU_HINT.search(os.path.basename(rel)):\n            continue\n        pins = _torch_pin_lines(repo, rel)",
     "        if False:\n            continue\n        pins = _torch_pin_lines(repo, rel)"),

    ("a CPU index on the install line no longer clears the pin",
     '            if CPU_INDEX.search(cmd) or re.search(r"PIP_(?:EXTRA_)?INDEX_URL[^\\n]*whl/cpu", itext):',
     "            if False:"),

    ("`-r` include chains are no longer followed",
     "            for parent in includers.get(cur, []):",
     "            for parent in []:"),

    ("`dead-weight-disable:` accepts any name",
     "                if val not in CHECKS:",
     "                if False:"),

    ("findings no longer fail the run",
     "    if findings and not args.soft_fail:\n        return 1",
     "    if False:\n        return 1"),

    ("--soft-fail annotations are emitted as errors",
     '        level = "error" if (not args.soft_fail or f.check in INTEGRITY) else "warning"',
     '        level = "error"'),

    # --- the .github#454 review round: each finding became a token ----------
    ("a pragma on the previous PIN line leaks onto the next pin",
     '    if index > 0 and lines[index - 1].lstrip().startswith("#"):',
     "    if index > 0:"),

    ("setup.py's INSTALL_REQUIRES vouches for its own pins",
     "                if is_declaration_file:\n                    continue",
     "                if False:\n                    continue"),

    ("a *REQUIRE*/*PACKAGES* constant vouches for its pins",
     "                if any(n.isupper() and not DECLARATION_NAME.search(n) for n in names) and isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):",
     "                if any(n.isupper() for n in names) and isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):"),

    ("the distribution table is looked up un-normalised (ruamel.yaml never matches)",
     "_IMPORT_NAME_OF_NORMALISED = {normalise(k): v for k, v in IMPORT_NAME_OF.items()}",
     "_IMPORT_NAME_OF_NORMALISED = dict(IMPORT_NAME_OF)"),

    ("@types/node is accepted on its name alone",
     "            if node_used(base) or any(node_used(p) for p in PEER_OF.get(base, ())):",
     '            if base == "node" or node_used(base) or any(node_used(p) for p in PEER_OF.get(base, ())):'),

    ("GPU / index context is read from the whole workflow, not the job",
     "        for job_start, job_text, context in _workflow_jobs(repo.text(rel)):",
     "        for job_start, job_text, context in [(1, repo.text(rel), repo.text(rel))]:"),

    ("YAML comments count as workflow context",
     '    lines = YAML_COMMENT.sub("", text).splitlines()',
     "    lines = text.splitlines()"),

    # --- second review round on .github#454 ----------------------------------
    ("a trailing YAML comment hides the `jobs:` line and every job with it",
     'JOBS_LINE = re.compile(r"^jobs:\\s*$")',
     'JOBS_LINE = re.compile(r"^jobs: # the jobs$")'),

    ("trailing YAML comments stop being blanked (a `# gpu` remark makes a GPU job)",
     'YAML_COMMENT = re.compile(r"^\\s*#.*$|\\s#[^\\"\'\\n]*$", re.M)',
     'YAML_COMMENT = re.compile(r"^\\s*#.*$", re.M)'),

    ("a `+cpu` in a requirements comment clears the pin",
     '        raw = _strip_req_comment(d.raw)\n        if "+cpu" in raw:',
     '        raw = d.raw\n        if "+cpu" in raw:'),

    ("an index URL in a requirements comment names the file's index",
     '    options = "\\n".join(_strip_req_comment(ln) for ln in text.splitlines() if REQ_OPTION.match(_strip_req_comment(ln)))',
     '    options = text'),

    ("an unreadable file reads as empty",
     '                self.unreadable[rel] = "%s: %s" % (type(exc).__name__, exc.strerror or exc)',
     '                pass'),

    # --- third review round on .github#454 -----------------------------------
    ("--soft-fail swallows scan-integrity findings",
     "    integrity = [f for f in findings if f.check in INTEGRITY]\n    if integrity:",
     "    integrity = []\n    if integrity:"),

    ("a GPU stage anywhere in a Dockerfile exempts every stage",
     "                    gpu = file_gpu or stage_gpu(no)",
     "                    gpu = file_gpu or any(stage_gpu(n) for n in range(1, no + 1))"),

    ("eslint evidence reads the raw package.json again (dependency keys count as extends)",
     '    chunks = [repo.text(rel) for rel in repo.glob(*ESLINT_CONFIG_GLOBS)]',
     '    chunks = [repo.text(rel) for rel in repo.glob(*ESLINT_CONFIG_GLOBS, "package.json")]'),

    ("an inline ${VAR:-default} in FROM is no longer honoured",
     "        if default is not None:\n            return default",
     "        if False:\n            return default"),

    ("a trailing comment on an ARG line becomes part of the default",
     '            am = ARG_LINE.match(raw.split(" #", 1)[0])  # a trailing comment is not the default',
     "            am = ARG_LINE.match(raw)"),

    ("a pyproject pragma attaches to any line that contains the name as a substring",
     r'''        rx = re.compile(r"""["']%s(?=[^A-Za-z0-9._-])""" % re.escape(needle))''',
     r'''        rx = re.compile(r"""%s""" % re.escape(needle))'''),

    ("exec-form RUN arrays are no longer read as installers",
     "    m = EXEC_FORM.match(line)\n    if not m:\n        return line",
     "    m = None\n    if not m:\n        return line"),

    ("a quoted -r path keeps its quotes and never matches the requirements basename",
     r'''REQ_FLAG = re.compile(r"""(?:^|\s)(?:-r|--requirement)[\s=]+["']?([^\s"']+)["']?""")''',
     r'''REQ_FLAG = re.compile(r"""(?:^|\s)(?:-r|--requirement)[\s=]+(\S+)""")'''),

    ("a setup.py helper call named like a requirements read is silently skipped",
     r'''                        if READS_REQUIREMENTS_FILE.search(src):''',
     r'''                        if "requirements" in src:'''),

    ("poetry/pdm/uv dependency tables count as invocations",
     r'''            keep = table.startswith("tool") and not DEP_TABLE.search(table)''',
     r'''            keep = table.startswith("tool")'''),

    ("an install command erases the whole line, chained tools included",
     r'''INSTALL_LINE = re.compile(r"\b(?:pip3?\s+(?:[^\n&;|]*?\s)?install|python3?\s+-m\s+pip\s+(?:[^\n&;|]*?\s)?install|uv\s+pip\s+install|npm\s+(?:i|install|add)|yarn\s+add|pnpm\s+(?:add|install))\b[^\n&;|]*")''',
     r'''INSTALL_LINE = re.compile(r"^.*\b(?:pip3?\s+(?:[^\n]*?\s)?install|python3?\s+-m\s+pip\s+(?:[^\n]*?\s)?install|uv\s+pip\s+install|npm\s+(?:i|install|add)|yarn\s+add|pnpm\s+(?:add|install))\b.*$", re.M)'''),

    ("workflow-level env is no longer inherited by the jobs",
     r'''        yield a + 1, job_text, header + "\n" + job_text''',
     r'''        yield a + 1, job_text, job_text'''),

    ("README fences count as JS imports again",
     r'''JS_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".mdx", ".vue", ".svelte", ".astro"}''',
     r'''JS_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".mdx", ".md", ".vue", ".svelte", ".astro"}'''),

    ("versioned alpine/slim tags read as full Debian again",
     r'''SMALL_BASE_TAG = re.compile(r"(?:^|-)(?:slim|alpine)[0-9.]*(?:-|$)")''',
     r'''SMALL_BASE_TAG = re.compile(r"(?:^|-)(?:slim|alpine)(?:-|$)")'''),

    ("a stage-local ARG leaks into a later FROM",
     r'''                if not seen_from:
                    args[am.group(1)] = (am.group(2) or "").strip().strip('"').strip("'")''',
     r'''                if True:
                    args[am.group(1)] = (am.group(2) or "").strip().strip('"').strip("'")'''),

    ("an unresolved FROM ARG is advisory again",
     r'''                findings.append(Finding("cannot-parse", rel, no,
                                        "FROM %s: the python tag comes from an ARG with no default in this file''',
     r'''                findings.append(Finding("full-python-base", rel, no,
                                        "FROM %s: the python tag comes from an ARG with no default in this file'''),

    ("autoprefixer vouches for itself through package.json",
     r'''    "autoprefixer": (r"autoprefixer", ("postcss.config.*",)),''',
     r'''    "autoprefixer": (r"autoprefixer", ("postcss.config.*", "package.json")),'''),

    ("a commented-out RUN counts as an installer",
     r'''        text = DOCKER_COMMENT.sub("", repo.text(rel))  # a commented-out RUN installs nothing (Bugbot, .github#454)''',
     r'''        text = repo.text(rel)'''),

    ("a GPU base behind a FROM ARG reads as CPU",
     r'''        ref, unresolved = _expand_args(m.group(1), args)  # `FROM ${CUDA_IMAGE}` is judged by what it expands to (Bugbot, .github#454)''',
     r'''        ref, unresolved = m.group(1), False'''),

    ("an unresolved FROM ARG's own name marks the stage GPU (`${CUDA_IMAGE}` reads GPU)",
     r'''            resolved = ARG_REF.sub("", ref)''',
     r'''            resolved = ref'''),

    ("an install in an unresolved-base stage is silently cleared, not cannot-parse",
     "            if unresolved:\n                # The stage's base image comes from an ARG with no value in this",
     "            if False:\n                # The stage's base image comes from an ARG with no value in this"),

    ("a CPU index anywhere in the Dockerfile clears every stage's install",
     r'''                    hits.append((rel, no, cmd, gpu, _stage_text(text, stage_gpu.stages, no), unresolved))''',
     r'''                    hits.append((rel, no, cmd, gpu, text, unresolved))'''),

    ("trailing Dockerfile comments are kept (hide exec-form installs, name indexes)",
     r'''DOCKER_COMMENT = re.compile(r"^\s*#.*$|\s#[^\"'\n]*$", re.M)''',
     r'''DOCKER_COMMENT = re.compile(r"^\s*#.*$", re.M)'''),

    ("a blank (blanked comment) inside a continued RUN ends the join",
     r'''        if buf and not stripped.strip():''',
     r'''        if False:'''),

    ("a parent requirements file's index no longer covers the pins it includes",
     r'''        if any(_file_names_index(repo.text(p)) in ("cpu", "gpu") for p in parents):
            continue''',
     r'''        if False:
            continue'''),

    ("a stale indirect-use is skipped when nothing is declared",
     "        for n, (reason, line) in cfg.indirect.items():\n            findings.append(Finding(\"stale-allowlist\", cfg_rel(cfg), line,",
     "        for n, (reason, line) in {}.items():\n            findings.append(Finding(\"stale-allowlist\", cfg_rel(cfg), line,"),
]


def _drop_bytecode_cache():
    try:
        cached = importlib.util.cache_from_source(str(GUARD))
    except (ValueError, NotImplementedError):
        return
    try:
        os.unlink(cached)
    except OSError:
        pass


def apply_one(src, old, new):
    n = src.count(old)
    if n != 1:
        raise LookupError("anchor matched %d times, expected exactly 1: %r" % (n, old[:80]))
    out = src.replace(old, new, 1)
    return None if out == src else out


def main():
    dry = "--dry" in sys.argv

    if not dry:
        rc = mutation_baseline.guard(ROOT, [GUARD])
        if rc:
            return rc

    pristine = GUARD.read_text(encoding="utf-8")
    stale, uncaught = [], []

    for label, old, new in MUTATIONS:
        try:
            mutated = apply_one(pristine, old, new)
        except LookupError as exc:
            stale.append((label, str(exc)))
            continue
        if mutated is None:
            stale.append((label, "NO-OP: the mutation changed nothing"))
            continue
        try:
            compile(mutated, str(GUARD), "exec")
        except SyntaxError as exc:
            stale.append((label, "MUTANT DOES NOT PARSE (%s) -- fix the mutation" % exc))
            continue
        if dry:
            print("  anchor ok  %s" % label)
            continue
        GUARD.write_text(mutated, encoding="utf-8")
        _drop_bytecode_cache()
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            run = subprocess.run([sys.executable, "-B", str(SUITE)], capture_output=True, text=True, cwd=str(ROOT), env=env)
        finally:
            GUARD.write_text(pristine, encoding="utf-8")
            _drop_bytecode_cache()
        caught = [line.strip()[5:].strip() for line in run.stdout.splitlines() if line.strip().startswith("FAIL:")]
        reported = "dead-weight-selftest:" in run.stdout
        if reported and run.returncode != 0:
            print("  caught     %s\n             by: %s" % (label, "; ".join(caught)[:160]))
        elif not reported:
            uncaught.append((label, "the suite did not report -- mutation broke the harness"))
            print("  UNCAUGHT   %s (harness broke, not detected)" % label)
        else:
            uncaught.append((label, "the suite passed with this broken"))
            print("  UNCAUGHT   %s" % label)

    if GUARD.read_text(encoding="utf-8") != pristine:
        GUARD.write_text(pristine, encoding="utf-8")
        print("::error::restored scripts/dead-weight.py after a mutation left it modified")
        return 2

    if stale:
        print("\n%d STALE anchor(s) -- the mutation no longer matches the checker:" % len(stale))
        for label, why in stale:
            print("  %s\n    %s" % (label, why))
    if uncaught:
        print("\n%d UNCAUGHT mutation(s) -- the selftest is vacuous for:" % len(uncaught))
        for label, why in uncaught:
            print("  %s\n    %s" % (label, why))
    if stale or uncaught:
        return 1
    print("\ndead-weight-mutations: %d/%d mutation(s) %s" % (len(MUTATIONS), len(MUTATIONS), "resolve" if dry else "caught"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
