#!/usr/bin/env python3
"""dead-weight.py -- the wave-2 guard of RFC-0087: what a repo declares, ships and
installs must be reachable from what it runs.

WHY THIS EXISTS
  A 2026-09-09 audit of 14 repositories found ~5 GB of installed-but-unused
  functionality per fleet build (RFC-0087, backend#3453): TensorFlow pinned for a
  year after the engine retired it, CUDA torch downloaded into CPU-only images and
  every CI job, dev tools declared and never invoked, full Debian Python bases
  where slim was the intent. Every one of those grew back from a pin nobody
  re-examined, because nothing compared the declaration to the code. This script
  is that comparison, run in the org's REQUIRED `quality / house-rules` gate.

THREE CHECKS
  declared-unused      Every top-level dependency (requirements*.txt, pyproject
                       [project] deps + extras, setup.py install_requires,
                       package.json dependencies/devDependencies/...) must be
                       IMPORTED somewhere in the tree, INVOKED BY NAME in a
                       Makefile / workflow / pre-commit / package.json script /
                       tool config, or declared `indirect-use` with a reason.
  full-python-base     A Dockerfile `FROM python:<tag>` whose tag is neither
                       -slim nor -alpine needs a same-line justification comment.
  cuda-torch-on-cpu    A `torch` pin naming neither `+cpu` nor a CPU wheel index,
                       installed on Linux without a GPU -- by a CPU Dockerfile or
                       a hosted-runner workflow step whose install line adds no
                       CPU index either. File-scoped: the pin, its file, and every
                       installer of that file are read together.

DERIVED, NEVER RESTATED (workspace rule 1)
  Declarations come from the real dependency files; usage from the AST of every
  Python file (static, lazy and string-based imports), from import/require
  specifiers in every JS/TS/CSS file, and from the names in the files that
  invoke tools. The only hand-written lists are the distribution -> import-name
  table below (mismatches Python cannot derive without installing the package)
  and the per-repo `indirect-use` entries, each of which must carry the path that
  reaches the package. A name alone is the audit's "unused" hypothesis in disguise
  and is refused as a config error.

FAIL CLOSED (rule 3)
  A Python file that does not parse, a setup.py whose install_requires is not a
  literal, a Dockerfile FROM whose tag is an unresolved ARG, an `indirect-use`
  entry for a package nothing declares any more, an entry without a reason --
  each is a finding, never silent agreement.

KNOWN BLIND SPOTS (precision choices, documented rather than hidden)
  * "Invoked by name" is a word match of the DISTRIBUTION name (and the console
    commands of a small tool table) inside Makefiles, workflows, pre-commit,
    package.json scripts, tool config and shell scripts. A tool named only in a
    comment there counts as invoked. The audit's class was "appears nowhere".
  * A string literal in Python source that looks like a dotted module path
    counts as a dynamic import (Django INSTALLED_APPS, Celery config, gunicorn
    worker classes, importlib strings). Prose strings that happen to equal a
    module name are rare and the cost of the false negative is one pin.
  * Requirement files are matched to their Dockerfile/workflow installers by
    BASENAME, because the path inside a container is not the path in the repo.
  * `cuda-torch-on-cpu` reads `torch` pins. torchvision/torchaudio ride the
    torch pin's wheel; a file pinning them without torch is not judged.
  * Python 3.11+ (tomllib). ubuntu-latest carries 3.12; the gate never sees older.

CONFIG (the repo's `.house-rules.conf`, shared with house-rules.sh)
  indirect-use: <dist> | <how the package is reached>   # required reason
  import-name:  <dist> | <module>                       # dist -> import mismatch
  exclude:      <glob>                                  # skip paths (shared)
  dead-weight-disable: <check>                          # turn one check off

USAGE
  dead-weight.py [--root DIR] [--config FILE] [--github] [--summary FILE]
                 [--soft-fail] [--check NAME]... [--verbose]
  Exit: 0 clean (or --soft-fail), 1 findings, 2 usage/internal error.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover -- the gate runs 3.12
    tomllib = None

CHECKS = ("declared-unused", "full-python-base", "cuda-torch-on-cpu")

#: Findings about the SCAN, not the tree: a file that could not be read or
#: parsed, a config line that could not be understood. `--soft-fail` governs
#: how loudly a real pin finding is reported; it never turns "we could not
#: look" into green (Bugbot, .github#454). These exit 1 in every mode.
INTEGRITY = frozenset({"cannot-read", "cannot-parse", "config-error"})

# ── distribution name -> module(s) it provides ─────────────────────────────────
# Only for names Python cannot derive by normalisation (lower-case, `-`/`.` -> `_`).
# Dotted values are matched against every dotted prefix an import produces, so
# `azure-servicebus` -> `azure.servicebus` is not satisfied by `import azure.identity`.
IMPORT_NAME_OF = {
    "pillow": "PIL",
    "scikit-learn": "sklearn",
    "scikit-survival": "sksurv",
    "scikit-image": "skimage",
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "opencv-contrib-python": "cv2",
    "opencv-contrib-python-headless": "cv2",
    "pyyaml": "yaml",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
    "python-dotenv": "dotenv",
    "msgpack-python": "msgpack",
    "protobuf": "google.protobuf",
    "google-api-core": "google.api_core",
    "google-auth": "google.auth",
    "azure-core": "azure.core",
    "azure-identity": "azure.identity",
    "azure-servicebus": "azure.servicebus",
    "azure-mgmt-servicebus": "azure.mgmt.servicebus",
    "azure-storage-blob": "azure.storage.blob",
    "azure-storage-file-share": "azure.storage.fileshare",
    "azure-keyvault-secrets": "azure.keyvault.secrets",
    "azure-monitor-opentelemetry": "azure.monitor.opentelemetry",
    "opencensus-ext-azure": "opencensus.ext.azure",
    "psycopg2-binary": "psycopg2",
    "psycopg": "psycopg",
    "mysql-connector-python": "mysql.connector",
    "pymysql": "pymysql",
    "nvidia-ml-py": "pynvml",
    "djangorestframework": "rest_framework",
    "djangorestframework-simplejwt": "rest_framework_simplejwt",
    "django-cors-headers": "corsheaders",
    "django-filter": "django_filters",
    "django-storages": "storages",
    "django-request-logging": "request_logging",
    "django-debug-toolbar": "debug_toolbar",
    "drf-yasg": "drf_yasg",
    "drf-spectacular": "drf_spectacular",
    "python-json-logger": "pythonjsonlogger",
    "python-multipart": "multipart",
    "attrs": "attr",
    "pyjwt": "jwt",
    "python-jose": "jose",
    "pyopenssl": "OpenSSL",
    "gitpython": "git",
    "pygithub": "github",
    "ruamel.yaml": "ruamel.yaml",
    "pynacl": "nacl",
    "python-gnupg": "gnupg",
    "python-slugify": "slugify",
    "unidecode": "unidecode",
    "py-cpuinfo": "cpuinfo",
    "faker": "faker",
    "factory-boy": "factory",
    "mssql-django": "mssql",
    "interpret-core": "interpret",
    "pytorch-lightning": "pytorch_lightning",
    "lightning": "lightning",
    "huggingface-hub": "huggingface_hub",
    "importlib-metadata": "importlib_metadata",
    "typing-extensions": "typing_extensions",
    "charset-normalizer": "charset_normalizer",
    "requests-oauthlib": "requests_oauthlib",
    "tracebloc-telemetry": "tracebloc_telemetry",
    "tracebloc-package": "tracebloc_package",
    "tracebloc": "tracebloc_package",
    "setuptools-scm": "setuptools_scm",
    "pre-commit": "pre_commit",
    "pytest-django": "pytest_django",
    "pytest-cov": "pytest_cov",
    "pytest-xdist": "xdist",
    "pytest-mock": "pytest_mock",
    "pytest-asyncio": "pytest_asyncio",
    "pytest-timeout": "pytest_timeout",
    "pytest-env": "pytest_env",
    "pytest-subtests": "pytest_subtests",
    "flake8": "flake8",
    "sqlalchemy": "sqlalchemy",
    "ipython": "IPython",
    "django-ckeditor": "ckeditor",
    "django-codemirror2": "codemirror2",
    "django-templated-mail": "templated_mail",
    "django-proxy": "proxy",
    "pillow-avif-plugin": "pillow_avif",
    "websocket-client": "websocket",
    "opencensus-context": "opencensus.common.runtime_context",
    "opencensus": "opencensus",
    "markdown": "markdown",
    "pygments": "pygments",
}

# Packages that are loaded by a HOST, not imported: pytest plugins via entry
# points, eslint configs/plugins via `extends`, vitest providers via config.
# Each value is (regex, tuple of file globs to search). A hit is the same
# evidence an `indirect-use` line would give, derived from the option the
# plugin actually adds -- so a pin whose option nobody sets is still a finding.
PLUGIN_EVIDENCE = {
    "pytest-cov": (r"--cov\b|--no-cov\b", ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "Makefile", ".github/workflows/*.yml")),
    "pytest-env": (r"^\s*env\s*=|\benv\s*=\s*\[", ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")),
    "pytest-xdist": (r"(?:^|\s)-n\s*(?:\d+|auto)|--numprocesses|--dist\b", ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "Makefile", ".github/workflows/*.yml")),
    "pytest-django": (r"DJANGO_SETTINGS_MODULE|--ds[\s=]|django_find_project", ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")),
    "pytest-timeout": (r"--timeout[\s=]|^\s*timeout\s*=", ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "Makefile", ".github/workflows/*.yml")),
    "pytest-asyncio": (r"asyncio_mode|pytest\.mark\.asyncio", ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "*.py")),
    "pytest-mock": (r"\bmocker\b", ("*.py",)),
    "pytest-subtests": (r"\bsubtests\b", ("*.py",)),
    "pytest-rerunfailures": (r"--reruns\b", ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "Makefile", ".github/workflows/*.yml")),
    "pytest-randomly": (r"-p\s+no:randomly|--randomly-seed", ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "Makefile", ".github/workflows/*.yml")),
    "@vitest/coverage-v8": (r"--coverage\b|coverage\s*:\s*\{|provider\s*:\s*['\"]v8['\"]", ("package.json", "vitest.config.*", "vite.config.*")),
    "@vitest/coverage-istanbul": (r"provider\s*:\s*['\"]istanbul['\"]", ("vitest.config.*", "vite.config.*")),
    "autoprefixer": (r"autoprefixer", ("postcss.config.*", "package.json")),
    "@tailwindcss/postcss": (r"@tailwindcss/postcss", ("postcss.config.*",)),
}

# A package that only exists to satisfy another package's peer requirement.
# It is used exactly when the package that requires it is used. Hand-written
# because the gate has no node_modules to read peerDependencies from; each
# row names the requiring package's documented peer list.
PEER_OF = {
    "react-dom": ("react", "next"),
    "@emotion/react": ("@mui/material", "@mui/system", "@mui/styled-engine"),
    "@emotion/styled": ("@mui/material", "@mui/system", "@mui/styled-engine"),
    "@emotion/cache": ("@mui/material-nextjs",),
    "@testing-library/dom": ("@testing-library/react", "@testing-library/user-event"),
    "@types/react-dom": ("react-dom",),
}

# Tools a build reaches for when a file kind exists, without any import or
# script naming them: Next/Vite compile .scss through `sass`; a tsconfig.json
# makes `typescript` (and `@types/node`) load-bearing for every type-check.
NODE_IMPLICIT = {
    "sass": ("*.scss", "*.sass"),
    "typescript": ("tsconfig.json", "tsconfig.*.json", "*.ts", "*.tsx"),
    "@types/node": ("tsconfig.json", "tsconfig.*.json"),
    "postcss": ("postcss.config.*",),
}
ESLINT_CONFIG_GLOBS = ("eslint.config.*", ".eslintrc*")

# Tools that are run, not imported: the console command(s) each distribution
# installs. Anything not here is matched by its distribution name only.
TOOL_COMMANDS = {
    "pytest": ("pytest", "py.test"),
    "black": ("black",),
    "ruff": ("ruff",),
    "mypy": ("mypy",),
    "isort": ("isort",),
    "flake8": ("flake8",),
    "pylint": ("pylint",),
    "twine": ("twine",),
    "bandit": ("bandit",),
    "coverage": ("coverage",),
    "pre-commit": ("pre-commit",),
    "gunicorn": ("gunicorn",),
    "uvicorn": ("uvicorn",),
    "celery": ("celery",),
    "pip-tools": ("pip-compile", "pip-sync"),
    "ipython": ("ipython",),
    "build": ("python -m build", "python3 -m build", "pyproject-build"),
    "wheel": ("bdist_wheel", "pip wheel"),
    "setuptools": ("setup.py", "setuptools"),
    "typescript": ("tsc",),
    "vitest": ("vitest",),
    "eslint": ("eslint",),
    "prettier": ("prettier",),
    "next": ("next",),
    "tsup": ("tsup",),
    "vite": ("vite",),
    "storybook": ("storybook",),
    "chromatic": ("chromatic",),
    "cypress": ("cypress",),
    "husky": ("husky",),
    "lint-staged": ("lint-staged",),
    "rimraf": ("rimraf",),
    "concurrently": ("concurrently",),
    "cross-env": ("cross-env",),
    "sharp": ("sharp",),
}

# Files whose text counts as "invoking" a tool by name.
INVOCATION_GLOBS = (
    "Makefile", "*.mk", "makefile", "GNUmakefile",
    ".github/workflows/*.yml", ".github/workflows/*.yaml",
    ".pre-commit-config.yaml", ".pre-commit-config.yml",
    "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", ".flake8", ".isort.cfg",
    "mypy.ini", ".coveragerc", ".bandit", "bandit.yaml", "bandit.yml",
    "Dockerfile*", "*.Dockerfile", "docker-compose*.yml", "docker-compose*.yaml",
    "Procfile", "*.sh", "*.bash", "scripts/*", "bin/*",
    "package.json", "*.config.js", "*.config.mjs", "*.config.cjs", "*.config.ts",
    "*.config.mts", ".eslintrc*", "eslint.config.*", "postcss.config.*",
    "tailwind.config.*", ".storybook/*", "tsconfig*.json", ".babelrc*", "babel.config.*",
    "vitest.config.*", "vite.config.*", "next.config.*", "cypress.config.*",
    ".prettierrc*", ".lintstagedrc*", ".husky/*", "content-collections.ts",
    "tsup.config.*", "components.json", ".releaserc*", "release.config.*",
)

JS_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".mdx", ".md", ".vue", ".svelte", ".astro"}
CSS_SUFFIXES = {".css", ".scss", ".sass", ".less", ".pcss"}

DEFAULT_EXCLUDES = (
    ".git/*", ".quality-tools/*", "node_modules/*", "*/node_modules/*",
    ".venv/*", "venv/*", "*/.venv/*", "*/venv/*", ".tox/*", ".next/*", "dist/*",
    "build/*", "storybook-static/*", "coverage/*", "*/__pycache__/*",
    ".mypy_cache/*", ".ruff_cache/*", "*/site-packages/*", ".yarn/*",
)

CPU_INDEX = re.compile(r"download\.pytorch\.org/whl/cpu\b")
GPU_INDEX = re.compile(r"download\.pytorch\.org/whl/(?:nightly/)?(?:cu|rocm)\d")
GPU_HINT = re.compile(r"(?:^|[^a-z])(?:gpu|cuda|nvidia|rocm)(?:[^a-z]|$)", re.I)
REQ_OPTION = re.compile(r"^\s*(?:-[a-zA-Z]|--[a-z-]+)")
REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
JS_SPEC = re.compile(
    r"""(?:\bimport\s*(?:[\w*\s{},$]*?\bfrom\s*)?|\bexport\s+(?:[\w*\s{},$]*?)\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*|\brequire\.resolve\s*\(\s*)["']([^"'\n]+)["']"""
)
CSS_IMPORT = re.compile(r"""@(?:import|use|forward)\s+(?:url\(\s*)?["']?([^"')\s;]+)""")
FROM_LINE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+(?:AS|as)\s+\S+)?\s*(#(.*))?$")
ARG_LINE = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?\s*$")
PIP_INSTALL = re.compile(r"\bpip3?\s+(?:[^\n]*?\s)?install\b|\bpython3?\s+-m\s+pip\s+(?:[^\n]*?\s)?install\b|\buv\s+pip\s+install\b")
REQ_FLAG = re.compile(r"""(?:^|\s)(?:-r|--requirement)[\s=]+["']?([^\s"']+)["']?""")
EXEC_FORM = re.compile(r"^(\s*(?:RUN|CMD|ENTRYPOINT)\s+)(\[.*\])\s*$")
RUNS_ON = re.compile(r"runs-on:\s*(.+)")


class Finding:
    __slots__ = ("check", "path", "line", "message")

    def __init__(self, check, path, line, message):
        self.check, self.path, self.line, self.message = check, path, line, message

    def __repr__(self):
        return "%s:%s: [%s] %s" % (self.path, self.line or 1, self.check, self.message)


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


#: IMPORT_NAME_OF keyed the way `normalise` spells names, so `ruamel.yaml` (dot
#: -> hyphen) and `PyYAML` (case) both resolve (Bugbot, .github#454).
_IMPORT_NAME_OF_NORMALISED = {normalise(k): v for k, v in IMPORT_NAME_OF.items()}


def module_of(dist: str) -> str:
    n = normalise(dist)
    if n in _IMPORT_NAME_OF_NORMALISED:
        return _IMPORT_NAME_OF_NORMALISED[n]
    return n.replace("-", "_")


# ── repository walk ────────────────────────────────────────────────────────────

class Repo:
    def __init__(self, root: Path, excludes):
        self.root = root
        self.excludes = list(DEFAULT_EXCLUDES) + list(excludes)
        self.files = self._tracked()
        self._text = {}
        self.unreadable = {}  # rel -> why; reported, never silently empty

    def _tracked(self):
        try:
            out = subprocess.run(
                ["git", "-C", str(self.root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                capture_output=True, check=True,
            ).stdout
            rel = [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
        except (subprocess.CalledProcessError, FileNotFoundError):
            rel = []
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".venv", "venv", "__pycache__"}]
                for f in filenames:
                    rel.append(os.path.relpath(os.path.join(dirpath, f), self.root))
        keep = []
        for r in sorted(set(rel)):
            if any(fnmatch.fnmatch(r, g) or fnmatch.fnmatch("./" + r, g) for g in self.excludes):
                continue
            if (self.root / r).is_file():
                keep.append(r)
        return keep

    def glob(self, *patterns):
        out = []
        for r in self.files:
            base = os.path.basename(r)
            for p in patterns:
                if "/" in p:
                    if fnmatch.fnmatch(r, p) or fnmatch.fnmatch(r, "*/" + p):
                        out.append(r)
                        break
                elif fnmatch.fnmatch(base, p):
                    out.append(r)
                    break
        return out

    def text(self, rel: str) -> str:
        """The file's text, or "" -- and the path recorded in `unreadable`, which
        main() turns into a `cannot-read` finding. An unreadable requirements
        file must not read as a file with no pins (Bugbot, .github#454)."""
        if rel not in self._text:
            try:
                self._text[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                self._text[rel] = ""
                self.unreadable[rel] = "%s: %s" % (type(exc).__name__, exc.strerror or exc)
        return self._text[rel]


# ── config ─────────────────────────────────────────────────────────────────────

class Config:
    def __init__(self):
        self.indirect = {}      # normalised dist -> (reason, line)
        self.import_names = {}  # normalised dist -> module
        self.excludes = []
        self.disabled = set()
        self.errors = []        # Finding

    @classmethod
    def load(cls, root: Path, rel: str):
        cfg = cls()
        path = root / rel
        if not path.is_file():
            return cfg
        for no, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if key == "exclude":
                cfg.excludes.append(val)
            elif key == "indirect-use":
                dist, _, reason = val.partition("|")
                dist, reason = dist.strip(), reason.strip()
                if not dist or len(reason.split()) < 3:
                    cfg.errors.append(Finding("config-error", rel, no,
                                              "`indirect-use: <dist> | <how it is reached>` needs a reason of at least "
                                              "three words -- a name alone is the 'unused' hypothesis restated"))
                    continue
                cfg.indirect[normalise(dist)] = (reason, no)
            elif key == "import-name":
                dist, _, mod = val.partition("|")
                dist, mod = dist.strip(), mod.strip()
                if not dist or not DOTTED.match(mod or ""):
                    cfg.errors.append(Finding("config-error", rel, no,
                                              "`import-name: <dist> | <module>` needs a dotted module name"))
                    continue
                cfg.import_names[normalise(dist)] = mod
            elif key == "dead-weight-disable":
                if val not in CHECKS:
                    cfg.errors.append(Finding("config-error", rel, no, "unknown check %r; known: %s" % (val, ", ".join(CHECKS))))
                cfg.disabled.add(val)
            # every other directive belongs to house-rules.sh
        return cfg


# ── declarations ───────────────────────────────────────────────────────────────

class Declaration:
    __slots__ = ("dist", "path", "line", "kind", "raw", "pragma")

    def __init__(self, dist, path, line, kind, raw="", pragma=None):
        self.dist, self.path, self.line, self.kind, self.raw = dist, path, line, kind, raw
        self.pragma = pragma  # `# dead-weight: <reason>` on the pin's line or the line above


PRAGMA = re.compile(r"#\s*dead-weight:\s*(.+?)\s*$")


def pragma_for(lines, index):
    """The `# dead-weight: <reason>` justification for line `index` (0-based):
    on the line itself (after the pin), or on the line directly above WHEN that
    line is a comment and nothing else. A justified pin's own trailing pragma
    must not leak onto the pin below it (Bugbot, .github#454). A reason under
    three words is not one, and is reported as such by the caller."""
    m = PRAGMA.search(lines[index])
    if m:
        return m.group(1)
    if index > 0 and lines[index - 1].lstrip().startswith("#"):
        m = PRAGMA.search(lines[index - 1])
        if m:
            return m.group(1)
    return None


def parse_requirements(repo: Repo, rel: str):
    """Yield Declarations for every pin line; options/includes are skipped here."""
    lines = repo.text(rel).splitlines()
    for idx, raw in enumerate(lines):
        line = raw.split(" #", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line or line.startswith("#") or REQ_OPTION.match(line):
            continue
        m = REQ_NAME.match(line)
        if not m:
            continue
        yield Declaration(m.group(1), rel, idx + 1, "python", raw, pragma_for(lines, idx))


def requirement_includes(repo: Repo, rel: str):
    """Files this requirements file pulls in with -r / --requirement."""
    out = []
    for raw in repo.text(rel).splitlines():
        m = REQ_FLAG.search(" " + raw.split("#", 1)[0])
        if m:
            target = os.path.normpath(os.path.join(os.path.dirname(rel), m.group(1)))
            out.append(target)
    return out


def _pyproject_deps(repo: Repo, rel: str, findings):
    decls = []
    if tomllib is None:
        findings.append(Finding("cannot-parse", rel, 1, "python < 3.11 has no tomllib; cannot read pyproject dependencies"))
        return decls
    try:
        data = tomllib.loads(repo.text(rel))
    except tomllib.TOMLDecodeError as exc:
        findings.append(Finding("cannot-parse", rel, 1, "pyproject.toml does not parse: %s" % exc))
        return decls
    project = data.get("project", {})
    lines = repo.text(rel).splitlines()

    def line_of(spec):
        """The line holding THIS spec: the quoted name followed by a non-name
        character, so `requests` never matches the `requests-oauthlib` line and
        inherits its pragma (Bugbot, .github#454)."""
        name = REQ_NAME.match(spec)
        needle = name.group(1) if name else spec
        rx = re.compile(r"""["']%s(?=[^A-Za-z0-9._-])""" % re.escape(needle))
        for i, ln in enumerate(lines, 1):
            if rx.search(ln):
                return i
        return 1

    for spec in project.get("dependencies", []) or []:
        m = REQ_NAME.match(spec)
        if m:
            ln = line_of(spec)
            decls.append(Declaration(m.group(1), rel, ln, "python", spec, pragma_for(lines, ln - 1)))
    for extra, specs in (project.get("optional-dependencies", {}) or {}).items():
        for spec in specs:
            m = REQ_NAME.match(spec)
            if m:
                ln = line_of(spec)
                decls.append(Declaration(m.group(1), rel, ln, "python", spec, pragma_for(lines, ln - 1)))
    return decls


def _setup_py_deps(repo: Repo, rel: str, findings):
    decls = []
    try:
        tree = _parse(repo.text(rel))
    except SyntaxError as exc:
        findings.append(Finding("cannot-parse", rel, exc.lineno or 1, "setup.py does not parse: %s" % exc.msg))
        return decls
    assigns = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigns[node.targets[0].id] = node.value

    def literal_specs(value):
        if isinstance(value, ast.Name) and value.id in assigns:
            value = assigns[value.id]
        if isinstance(value, (ast.List, ast.Tuple)):
            out = []
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append((elt.value, elt.lineno))
                else:
                    return None
            return out
        if isinstance(value, ast.Dict):
            out = []
            for v in value.values:
                inner = literal_specs(v)
                if inner is None:
                    return None
                out.extend(inner)
            return out
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in ("install_requires", "extras_require", "setup_requires"):
                    specs = literal_specs(kw.value)
                    if specs is None:
                        src = ast.get_source_segment(repo.text(rel), kw.value) or ""
                        if "requirements" in src:
                            continue  # delegates to a requirements file this script reads directly
                        findings.append(Finding("cannot-parse", rel, kw.value.lineno,
                                                "%s is not a literal list; declare it literally or via a requirements file" % kw.arg))
                        continue
                    for spec, line in specs:
                        m = REQ_NAME.match(spec)
                        if m:
                            decls.append(Declaration(m.group(1), rel, line, "python", spec))
    return decls


def _package_json_deps(repo: Repo, rel: str, findings):
    decls = []
    try:
        data = json.loads(repo.text(rel))
    except json.JSONDecodeError as exc:
        findings.append(Finding("cannot-parse", rel, exc.lineno, "package.json does not parse: %s" % exc.msg))
        return decls
    lines = repo.text(rel).splitlines()
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name in (data.get(section) or {}):
            line = next((i for i, ln in enumerate(lines, 1) if ('"%s"' % name) in ln), 1)
            decls.append(Declaration(name, rel, line, "node", section))
    return decls


def collect_declarations(repo: Repo, findings):
    py, node = [], []
    for rel in repo.glob("requirements*.txt", "requirements/*.txt", "*requirements*.txt"):
        py.extend(parse_requirements(repo, rel))
    for rel in repo.glob("pyproject.toml"):
        py.extend(_pyproject_deps(repo, rel, findings))
    for rel in repo.glob("setup.py"):
        py.extend(_setup_py_deps(repo, rel, findings))
    for rel in repo.glob("package.json"):
        node.extend(_package_json_deps(repo, rel, findings))
    return py, node


# ── usage evidence ─────────────────────────────────────────────────────────────

def _parse(src: str):
    """ast.parse without the scanned repo's own SyntaxWarnings (invalid escapes
    in ITS regexes) leaking into this checker's output."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(src)


def _dotted_prefixes(name: str):
    parts = name.split(".")
    return {".".join(parts[: i + 1]) for i in range(len(parts))}


#: An ALL-CAPS list that is itself a dependency declaration (`INSTALL_REQUIRES`,
#: `EXTRAS_REQUIRE`, `DEPENDENCIES`, `PINNED_PACKAGES`) vouches for nothing: its
#: strings are the pins under test, not modules the code reaches.
DECLARATION_NAME = re.compile(r"REQUIRE|DEPEND|EXTRAS|PACKAGES|PINS?\b|WHEELS?\b")
IMPORTISH_CALL = re.compile(r"(?:^|\.)(?:import_module|__import__|import_string|import_by_path|load_backend|get_module|load_plugin|entry_point|load_entry_point|resolve_name)$")


def _call_name(node):
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def python_usage(repo: Repo, findings):
    """Every module path (and dotted prefix) Python code can reach, plus the
    shell-command strings it runs.

    Imports: static, in-function, `from x import y` (y may be a submodule).
    Strings, three narrow ways only -- a bare identifier string anywhere would
    make any dict key a phantom import:
      * a DOTTED string literal anywhere (`"corsheaders.middleware.CorsMiddleware"`,
        a gunicorn worker class, an importlib target);
      * a single-segment string that is the argument of an import-like call
        (`importlib.import_module("sksurv")`, `__import__("x")`);
      * a single-segment string inside a list/tuple/set bound to an ALL-CAPS
        name -- the shape of INSTALLED_APPS, AUTHENTICATION_BACKENDS, PLUGINS.
    Returns (reached, command_strings): the second is every string literal that
    could be a shell command, so `subprocess.run("gunicorn --bind ...")` counts
    as invoking gunicorn by name."""
    reached, commands = set(), []
    for rel in repo.glob("*.py"):
        src = repo.text(rel)
        try:
            tree = _parse(src)
        except SyntaxError as exc:
            findings.append(Finding("cannot-parse", rel, exc.lineno or 1,
                                    "does not parse as Python 3, so its imports are unknown (%s); fix it or `exclude:` it" % exc.msg))
            continue
        registry_strings = set()
        is_declaration_file = os.path.basename(rel) in ("setup.py", "setup_py.py") or os.path.basename(rel).startswith("setup_")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                if is_declaration_file:
                    continue  # setup.py's INSTALL_REQUIRES is a declaration, not a registry (Bugbot, .github#454)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                if any(n.isupper() and not DECLARATION_NAME.search(n) for n in names) and isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            registry_strings.add(elt.value)
            elif isinstance(node, ast.Call) and IMPORTISH_CALL.search(_call_name(node)):
                for arg in node.args[:1]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        registry_strings.add(arg.value)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    reached |= _dotted_prefixes(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    reached |= _dotted_prefixes(node.module)
                    for alias in node.names:
                        reached |= _dotted_prefixes(node.module + "." + alias.name)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value
                if 1 < len(v) <= 160 and DOTTED.match(v) and ("." in v or v in registry_strings):
                    reached |= _dotted_prefixes(v)
                elif " " in v and len(v) <= 400:
                    commands.append(v)
    return reached, commands


def node_usage(repo: Repo):
    """Every package name a JS/TS/CSS/MDX specifier points at."""
    packages = set()
    for rel in repo.files:
        suffix = os.path.splitext(rel)[1].lower()
        if suffix in JS_SOURCE_SUFFIXES:
            for spec in JS_SPEC.findall(repo.text(rel)):
                pkg = _package_of_specifier(spec)
                if pkg:
                    packages.add(pkg)
        elif suffix in CSS_SUFFIXES:
            for spec in CSS_IMPORT.findall(repo.text(rel)):
                pkg = _package_of_specifier(spec.lstrip("~"))
                if pkg:
                    packages.add(pkg)
    return packages


def _package_of_specifier(spec: str):
    if not spec or spec.startswith((".", "/", "node:", "http:", "https:", "data:", "#")):
        return None
    if spec.startswith("@"):
        parts = spec.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    return spec.split("/")[0]


INSTALL_LINE = re.compile(r"^.*\b(?:pip3?\s+(?:[^\n]*?\s)?install|python3?\s+-m\s+pip\s+(?:[^\n]*?\s)?install|uv\s+pip\s+install|npm\s+(?:i|install|add)|yarn\s+add|pnpm\s+(?:add|install))\b.*$", re.M)


def _tool_tables_only(toml_text: str) -> str:
    """pyproject.toml with its [project*] / [build-system] tables removed: a
    dependency list names every pin, and a dependency file must never vouch for
    its own entries. What stays is [tool.*] -- ruff/black/pytest/coverage
    config, the part that RUNS tools."""
    out, keep = [], True
    for line in toml_text.splitlines():
        m = re.match(r"^\s*\[+\s*([A-Za-z0-9_.\"-]+)", line)
        if m:
            keep = m.group(1).startswith("tool")
        if keep:
            out.append(line)
    return "\n".join(out)


def invocation_text(repo: Repo):
    """Concatenated text of every file whose contents can RUN a tool by name.
    Two exclusions keep a declaration from vouching for itself: package.json
    contributes only its `scripts`, pyproject.toml only its [tool.*] tables;
    and `pip install X` / `npm add X` lines are dropped everywhere, because
    installing a package is not running it."""
    chunks = []
    for rel in repo.glob(*INVOCATION_GLOBS):
        if rel.endswith("package.json"):
            try:
                scripts = json.loads(repo.text(rel)).get("scripts") or {}
            except json.JSONDecodeError:
                scripts = {}
            chunks.append("\n".join(str(v) for v in scripts.values()))
        elif rel.endswith("pyproject.toml"):
            chunks.append(_tool_tables_only(repo.text(rel)))
        else:
            chunks.append(repo.text(rel))
    return INSTALL_LINE.sub("", "\n".join(chunks))


def invoked(dist: str, text: str) -> bool:
    """The distribution (or a console command it installs) named as a word in the
    files that run tools. `/`-adjacent hits are paths, not invocations."""
    names = {dist, normalise(dist), normalise(dist).replace("-", "_")}
    names |= set(TOOL_COMMANDS.get(normalise(dist), ()))
    for n in names:
        if not n:
            continue
        if re.search(r"(?<![\w@/.-])%s(?![\w/.-])" % re.escape(n), text):
            return True
    return False


def invoked_as_command(dist: str, commands) -> bool:
    """A console command of the tool in COMMAND POSITION inside a string literal
    the code runs (subprocess, os.system, a CMD written by hand)."""
    names = set(TOOL_COMMANDS.get(normalise(dist), ())) | {normalise(dist)}
    for cmd in commands:
        for n in names:
            if re.search(r"(?:^|[\s;&|(`'\"])%s(?=$|[\s;&|)'\"])" % re.escape(n), cmd):
                return True
    return False


def plugin_evidence(repo: Repo, dist: str) -> bool:
    n = normalise(dist)
    key = next((k for k in PLUGIN_EVIDENCE if normalise(k) == n), None)
    if key is None:
        return False
    pattern, globs = PLUGIN_EVIDENCE[key]
    rx = re.compile(pattern, re.M)
    return any(rx.search(repo.text(rel)) for rel in repo.glob(*globs))


def eslint_extends(repo: Repo, dist: str) -> bool:
    """`eslint-config-x` / `@scope/eslint-config` / `eslint-plugin-x` are reached
    by the SHORT name in `extends:` / `plugins:` (`"next/core-web-vitals"`,
    `"plugin:react/recommended"`, `"@scope"`), which no import ever spells."""
    m = re.match(r"^(?:@([\w.-]+)/)?eslint-(config|plugin)(?:-([\w.-]+))?$", dist)
    if not m:
        return False
    scope, kind, short = m.groups()
    needles = []
    if short:
        needles.append(("@%s/%s" % (scope, short)) if scope else short)
    elif scope:
        needles.append("@" + scope)
    # Config files, plus ONLY the `eslintConfig` object of package.json: the raw
    # file names every dependency as a key, so scanning it would make an unused
    # eslint-config-next read as extended (Bugbot, .github#454).
    chunks = [repo.text(rel) for rel in repo.glob(*ESLINT_CONFIG_GLOBS)]
    for rel in repo.glob("package.json"):
        try:
            cfg = json.loads(repo.text(rel)).get("eslintConfig")
        except json.JSONDecodeError:
            cfg = None
        if cfg:
            chunks.append(json.dumps(cfg))
    text = "\n".join(chunks)
    for n in needles:
        e = re.escape(n)
        if kind == "config" and re.search(r"""["']%s(?:/[\w./-]*)?["']""" % e, text):
            return True
        if kind == "plugin" and re.search(r"""["'](?:plugin:)?%s(?:/[\w.-]+)?["']""" % e, text):
            return True
    return False


def implicit_by_files(repo: Repo, dist: str) -> bool:
    globs = NODE_IMPLICIT.get(dist)
    return bool(globs) and bool(repo.glob(*globs))


# ── check 1: declared-unused ───────────────────────────────────────────────────

def check_declared_unused(repo: Repo, cfg: Config, findings):
    py_decls, node_decls, = collect_declarations(repo, findings)
    if not py_decls and not node_decls:
        # Nothing declared -- but an `indirect-use` left behind after the last
        # pin went is still a stale entry, not silent agreement (Bugbot, .github#454).
        for n, (reason, line) in cfg.indirect.items():
            findings.append(Finding("stale-allowlist", cfg_rel(cfg), line,
                                    "`indirect-use: %s` names a package no dependency file declares any more; delete the entry" % n))
        return
    reached, commands = python_usage(repo, findings) if py_decls else (set(), [])
    node_pkgs = node_usage(repo) if node_decls else set()
    invocations = invocation_text(repo)

    def justified(d):
        """A pin's own `# dead-weight:` pragma, or a config `indirect-use` line."""
        if d.pragma is not None:
            if len(d.pragma.split()) < 3:
                findings.append(Finding("config-error", d.path, d.line,
                                        "`# dead-weight: <reason>` needs at least three words saying how `%s` is reached" % d.dist))
            return True
        return normalise(d.dist) in cfg.indirect

    for d in py_decls:
        n = normalise(d.dist)
        module = cfg.import_names.get(n) or module_of(d.dist)
        if module in reached or module.replace("_", "-") in reached:
            continue
        if justified(d) or plugin_evidence(repo, d.dist) or invoked(d.dist, invocations) or invoked_as_command(d.dist, commands):
            continue
        findings.append(Finding("declared-unused", d.path, d.line,
                                "`%s` is declared but nothing imports `%s`, nothing invokes it by name, and neither a "
                                "`# dead-weight: <how it is reached>` pragma nor an `indirect-use: %s | ...` line in .house-rules.conf "
                                "justifies it; remove the pin or name the path" % (d.dist, module, d.dist)))

    used_node = {normalise(p): p for p in node_pkgs}
    declared_node = {d.dist for d in node_decls}

    def node_used(pkg):
        return pkg in node_pkgs or normalise(pkg) in used_node

    for d in node_decls:
        if node_used(d.dist):
            continue
        if d.dist.startswith("@types/"):
            typed = d.dist[len("@types/"):]
            base = ("@" + typed.replace("__", "/", 1)) if "__" in typed else typed  # @types/scope__name -> @scope/name
            if node_used(base) or any(node_used(p) for p in PEER_OF.get(base, ())):
                continue  # `@types/node` is NOT special-cased: NODE_IMPLICIT ties it to a tsconfig (Bugbot, .github#454)
        if any(node_used(p) and p in declared_node for p in PEER_OF.get(d.dist, ())):
            continue
        if justified(d) or implicit_by_files(repo, d.dist) or plugin_evidence(repo, d.dist) or eslint_extends(repo, d.dist) or invoked(d.dist, invocations):
            continue
        findings.append(Finding("declared-unused", d.path, d.line,
                                "`%s` (%s) is declared but no import/require/@import names it, no script or config "
                                "invokes it, no declared package lists it as a peer, and .house-rules.conf has no "
                                "`indirect-use: %s | <how it is reached>`" % (d.dist, d.kind, d.dist)))

    declared = {normalise(d.dist) for d in py_decls} | {normalise(d.dist) for d in node_decls}
    for n, (reason, line) in cfg.indirect.items():
        if n not in declared:
            findings.append(Finding("stale-allowlist", cfg_rel(cfg), line,
                                    "`indirect-use: %s` names a package no dependency file declares any more; delete the entry" % n))


def cfg_rel(cfg):
    return getattr(cfg, "rel", ".house-rules.conf")


# ── check 2: full-python-base ──────────────────────────────────────────────────

def _image_name_tag(ref: str):
    ref = ref.split("@", 1)[0]
    path = ref.rsplit("/", 1)[-1]
    name, _, tag = path.partition(":")
    return name, tag


ARG_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")


def _expand_args(ref: str, args: dict) -> tuple:
    """Dockerfile `$VAR`, `${VAR}`, `${VAR:-default}`, `${VAR-default}` inside an
    image reference: (expanded, unresolved). An ARG with a value wins; else the
    inline default; else the reference stays and is reported as unresolved
    (Bugbot, .github#454: `${PY:-3.11-slim}` used to keep its `:-3.11-slim}` tail)."""
    unresolved = False

    def sub(m):
        nonlocal unresolved
        var = m.group(1) or m.group(3)
        default = m.group(2)
        if var in args and args[var]:
            return args[var]
        if default is not None:
            return default
        unresolved = True
        return m.group(0)

    return ARG_REF.sub(sub, ref), unresolved


def check_full_python_base(repo: Repo, cfg: Config, findings):
    for rel in repo.glob("Dockerfile*", "*.Dockerfile", "*.dockerfile"):
        args = {}
        for no, raw in enumerate(repo.text(rel).splitlines(), 1):
            am = ARG_LINE.match(raw.split(" #", 1)[0])  # a trailing comment is not the default
            if am:
                args[am.group(1)] = (am.group(2) or "").strip().strip('"').strip("'")
                continue
            fm = FROM_LINE.match(raw)
            if not fm:
                continue
            ref, unresolved = _expand_args(fm.group(1), args)
            name, tag = _image_name_tag(ref)
            if name != "python":
                continue
            if unresolved:
                findings.append(Finding("full-python-base", rel, no,
                                        "FROM %s: the python tag comes from an ARG with no default in this file, so slim-or-full cannot be told; give the ARG a default" % fm.group(1)))
                continue
            if re.search(r"(?:^|-)(?:slim|alpine)(?:-|$)", tag):
                continue
            comment = (fm.group(3) or "").strip()
            if len(comment.split()) >= 3:
                continue
            findings.append(Finding("full-python-base", rel, no,
                                    "FROM python:%s is the full Debian image (~360 MB more than -slim); use python:%s-slim or justify the full base in a comment on this line"
                                    % (tag or "latest", tag or "<ver>")))


# ── check 3: cuda-torch-on-cpu ─────────────────────────────────────────────────

def _shell_form(line: str) -> str:
    """A Dockerfile exec-form `RUN ["pip", "install", "-r", "requirements.txt"]`
    read as the shell words it means, so the installer scan sees it the way it
    sees shell form (Bugbot, .github#454). Anything that is not a JSON array of
    strings is returned unchanged."""
    m = EXEC_FORM.match(line)
    if not m:
        return line
    try:
        words = json.loads(m.group(2))
    except json.JSONDecodeError:
        return line
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        return line
    return m.group(1) + " ".join(words)


def _joined_commands(text: str):
    """Yield (first_line_no, logical_line) with backslash continuations joined
    and exec-form arrays rendered as shell words."""
    buf, start = [], None
    for no, raw in enumerate(text.splitlines(), 1):
        if start is None:
            start = no
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(stripped)
        yield start, _shell_form(" ".join(buf))
        buf, start = [], None
    if buf:
        yield start, _shell_form(" ".join(buf))


def _strip_req_comment(raw: str) -> str:
    """A requirements line without its trailing comment. pip treats ` #` (and a
    leading `#`) as a comment; a `+cpu` or an index URL inside one is prose."""
    if raw.lstrip().startswith("#"):
        return ""
    return raw.split(" #", 1)[0].split("\t#", 1)[0]


def _torch_pin_lines(repo: Repo, rel: str):
    out = []
    for d in parse_requirements(repo, rel):
        if normalise(d.dist) != "torch":
            continue
        raw = _strip_req_comment(d.raw)
        if "+cpu" in raw:
            continue
        marker = raw.split(";", 1)[1] if ";" in raw else ""
        if re.search(r"""sys_platform\s*==\s*["'](?:darwin|win32)["']""", marker) or \
           re.search(r"""sys_platform\s*!=\s*["']linux["']""", marker) or \
           re.search(r"""platform_system\s*==\s*["'](?:Darwin|Windows)["']""", marker):
            continue
        out.append(d)
    return out


def _file_names_index(text: str):
    """The wheel index a requirements file names on an OPTION line (comments
    stripped): "gpu", "cpu" or None. A URL in a comment names nothing."""
    options = "\n".join(_strip_req_comment(ln) for ln in text.splitlines() if REQ_OPTION.match(_strip_req_comment(ln)))
    if GPU_INDEX.search(options):
        return "gpu"
    if CPU_INDEX.search(options):
        return "cpu"
    return None


def _stage_gpu_map(text: str):
    """A function line_no -> is this line inside a GPU build stage?

    A Dockerfile is judged per STAGE, not per file: a multi-stage image whose
    builder is `FROM nvidia/cuda` and whose runtime is `FROM python:3.11-slim`
    installs into the CPU runtime, and that install is exactly the finding
    (Bugbot, .github#454). A stage inherits GPU-ness from an earlier stage it
    is `FROM <name>` of; anything before the first FROM (ARGs) is no stage."""
    stages = []  # (start_line, gpu)
    named = {}
    for no, raw in enumerate(text.splitlines(), 1):
        m = FROM_LINE.match(raw)
        if not m:
            continue
        ref = m.group(1)
        alias = re.search(r"\s(?:AS|as)\s+(\S+)\s*(?:#.*)?$", raw)
        if ref in named:
            gpu = named[ref]
        else:
            gpu = bool(GPU_HINT.search(_image_name_tag(ref)[0] + " " + ref))
        if alias:
            named[alias.group(1)] = gpu
        stages.append((no, gpu))

    def lookup(line_no: int) -> bool:
        current = False
        for start, gpu in stages:
            if start <= line_no:
                current = gpu
            else:
                break
        return current

    return lookup


def _installers_of(repo: Repo, req_basename: str, want_file_rel: str):
    """(rel, line, command, gpu_context) for every Dockerfile RUN / workflow step
    that pip-installs a requirements file with this basename."""
    hits = []
    for rel in repo.glob("Dockerfile*", "*.Dockerfile", "*.dockerfile"):
        text = repo.text(rel)
        file_gpu = bool(GPU_HINT.search(os.path.basename(rel)))
        stage_gpu = _stage_gpu_map(text)
        for no, cmd in _joined_commands(text):
            if not PIP_INSTALL.search(cmd):
                continue
            for target in REQ_FLAG.findall(cmd):
                if os.path.basename(target) == req_basename:
                    hits.append((rel, no, cmd, file_gpu or stage_gpu(no), text))
    for rel in repo.glob(".github/workflows/*.yml", ".github/workflows/*.yaml"):
        for job_start, job_text in _workflow_jobs(repo.text(rel)):
            gpu = any(GPU_HINT.search(m.group(1)) for m in RUNS_ON.finditer(job_text))
            for offset, cmd in _joined_commands(job_text):
                if not PIP_INSTALL.search(cmd):
                    continue
                for target in REQ_FLAG.findall(cmd):
                    if os.path.basename(target) == req_basename:
                        hits.append((rel, job_start + offset - 1, cmd, gpu, job_text))
    return hits


#: Full-line comments, and trailing ` #...` comments that contain no quote
#: (a `#` inside a quoted string or a `${{ }}` expression is not a comment; a
#: trailing comment with a quote in it is left alone rather than guessed at).
YAML_COMMENT = re.compile(r"^\s*#.*$|\s#[^\"'\n]*$", re.M)
JOB_HEADER = re.compile(r"^  ([A-Za-z_][\w-]*):\s*$")
JOBS_LINE = re.compile(r"^jobs:\s*$")


def _workflow_jobs(text: str):
    """Yield (first_line_no, job_text) per job of a workflow, comments blanked.

    Context that clears a CUDA-torch finding -- a GPU `runs-on`, a CPU index in
    `PIP_EXTRA_INDEX_URL` -- must come from the JOB that runs the install, not
    from anywhere in the file: one GPU job (or a comment mentioning one) must not
    exempt every CPU job beside it (Bugbot, .github#454). A workflow with no
    `jobs:` block is one block, so nothing is skipped."""
    lines = YAML_COMMENT.sub("", text).splitlines()
    in_jobs, starts = False, []
    for i, line in enumerate(lines):
        if JOBS_LINE.match(line):
            in_jobs = True
            continue
        if in_jobs and line and not line.startswith(" "):
            in_jobs = False  # a later top-level key ends the jobs map
        if in_jobs and JOB_HEADER.match(line):
            starts.append(i)
    if not starts:
        yield 1, "\n".join(lines)
        return
    bounds = starts + [len(lines)]
    for a, b in zip(bounds, bounds[1:]):
        yield a + 1, "\n".join(lines[a:b])


def check_cuda_torch_on_cpu(repo: Repo, cfg: Config, findings):
    req_files = repo.glob("requirements*.txt", "requirements/*.txt", "*requirements*.txt")
    includers = {}
    for rel in req_files:
        for inc in requirement_includes(repo, rel):
            includers.setdefault(inc, []).append(rel)

    for rel in req_files:
        if GPU_HINT.search(os.path.basename(rel)):
            continue
        pins = _torch_pin_lines(repo, rel)
        if not pins:
            continue
        if _file_names_index(repo.text(rel)) in ("cpu", "gpu"):
            continue
        # walk up include chains: the installer of an including file installs this one too
        basenames = {os.path.basename(rel)}
        frontier = [rel]
        while frontier:
            cur = frontier.pop()
            for parent in includers.get(cur, []):
                if os.path.basename(parent) not in basenames:
                    basenames.add(os.path.basename(parent))
                    frontier.append(parent)
        installers = []
        for b in basenames:
            installers.extend(_installers_of(repo, b, rel))
        for irel, ino, cmd, gpu, itext in installers:
            if gpu:
                continue
            if CPU_INDEX.search(cmd) or re.search(r"PIP_(?:EXTRA_)?INDEX_URL[^\n]*whl/cpu", itext):
                continue
            for pin in pins:
                findings.append(Finding(
                    "cuda-torch-on-cpu", pin.path, pin.line,
                    "`%s` names no `+cpu` build and this file names no CPU wheel index, yet %s:%d installs it on Linux without a GPU "
                    "(the PyPI wheel drags ~2 GB of CUDA libraries). Add `--extra-index-url https://download.pytorch.org/whl/cpu` "
                    "to this file, pin `torch==<ver>+cpu; sys_platform == \"linux\"`, or put the index on the install line"
                    % (pin.raw.strip(), irel, ino)))


# ── reporting ──────────────────────────────────────────────────────────────────

def report(findings, args):
    findings = sorted(findings, key=lambda f: (f.check, f.path, f.line or 0))
    for f in findings:
        level = "error" if (not args.soft_fail or f.check in INTEGRITY) else "warning"
        if args.github:
            print("::%s file=%s,line=%d,title=dead-weight %s::%s" % (level, f.path, f.line or 1, f.check, f.message))
        print(repr(f))
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write("\n### dead-weight (RFC-0087 D3)%s\n\n" % (" -- advisory" if args.soft_fail else ""))
            if not findings:
                fh.write("No findings.\n")
            else:
                fh.write("| check | file | line | finding |\n|---|---|---|---|\n")
                for f in findings:
                    fh.write("| %s | `%s` | %d | %s |\n" % (f.check, f.path, f.line or 1, f.message.replace("|", "\\|")))
    by = {}
    for f in findings:
        by[f.check] = by.get(f.check, 0) + 1
    print("dead-weight: %d finding(s)%s" % (len(findings), (" -- " + ", ".join("%s=%d" % kv for kv in sorted(by.items()))) if by else ""))
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], add_help=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--config", default=".house-rules.conf")
    ap.add_argument("--github", action="store_true")
    ap.add_argument("--summary")
    ap.add_argument("--soft-fail", action="store_true")
    ap.add_argument("--check", action="append", choices=CHECKS)
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print("dead-weight: --root %s is not a directory" % root, file=sys.stderr)
        return 2
    cfg = Config.load(root, args.config)
    cfg.rel = args.config
    repo = Repo(root, cfg.excludes + args.exclude)
    findings = list(cfg.errors)
    wanted = [c for c in (args.check or CHECKS) if c not in cfg.disabled]
    if args.verbose:
        print("dead-weight: %d files, checks: %s" % (len(repo.files), ", ".join(wanted)))
    for check in wanted:
        {"declared-unused": check_declared_unused,
         "full-python-base": check_full_python_base,
         "cuda-torch-on-cpu": check_cuda_torch_on_cpu}[check](repo, cfg, findings)
    for rel, why in sorted(repo.unreadable.items()):
        findings.append(Finding("cannot-read", rel, 1,
                                "could not be read (%s), so whatever it declares is unknown; fix the permissions or `exclude:` it" % why))
    findings = report(findings, args)
    integrity = [f for f in findings if f.check in INTEGRITY]
    if integrity:
        print("dead-weight: %d scan-integrity finding(s) -- these fail the run even under --soft-fail" % len(integrity))
        return 1
    if findings and not args.soft_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
