#!/usr/bin/env python3
"""Selftest for scripts/dead-weight.py (RFC-0087 D3, backend#3523).

WHY IT EXISTS. The checker decides, for every pin in every repo, whether the
code reaches it. Each decision rests on one token doing one job -- the dotted
prefix match, the three-word reason, the same-line justification, the GPU
exclusion, the command-position test. A token that stops doing its job does not
make the checker crash; it makes the checker agree with everything, which is
the failure mode RFC-0087 exists to end. Every token below has an ACCEPT case
and a REFUSE case, so a guard that starts refusing correct pins (the way a
maintainer learns to delete it) is caught as surely as one that stops refusing
dead ones.

HERMETIC. Each case writes a small repository into a tempdir (no git, so the
os.walk fallback is what runs), points the real module at it and calls the
real check functions. No network, no installs.

INPUTS ARE WRITTEN DOWN INDEPENDENTLY OF THE CHECKER (workspace rule 9's
corollary). The pins, the import lines and the config directives below are
literals typed here, not iterated out of the module's tables -- a case that
reads IMPORT_NAME_OF to test IMPORT_NAME_OF would be blind to a typo in it.

CONTRACT WITH dead-weight-mutations.py: a failing case prints a line starting
`FAIL:`; the run ends with a line starting `dead-weight-selftest:`; exit 1 on
any failure.
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "dead-weight.py"

sys.dont_write_bytecode = True


def _load():
    spec = importlib.util.spec_from_file_location("dead_weight", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DW = _load()

RESULTS = []


def case(name):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


class Fixture:
    """A throwaway repo: `files` maps relative path -> text."""

    def __init__(self, files, config=None):
        self.dir = tempfile.mkdtemp(prefix="dead-weight-")
        for rel, text in files.items():
            path = pathlib.Path(self.dir, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if config is not None:
            pathlib.Path(self.dir, ".house-rules.conf").write_text(config, encoding="utf-8")

    def findings(self, checks=None):
        root = pathlib.Path(self.dir)
        cfg = DW.Config.load(root, ".house-rules.conf")
        cfg.rel = ".house-rules.conf"
        repo = DW.Repo(root, cfg.excludes)
        out = list(cfg.errors)
        for check in (checks or DW.CHECKS):
            if check in cfg.disabled:
                continue
            {"declared-unused": DW.check_declared_unused,
             "full-python-base": DW.check_full_python_base,
             "cuda-torch-on-cpu": DW.check_cuda_torch_on_cpu}[check](repo, cfg, out)
        return out

    def main(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = DW.main(["--root", self.dir, *argv])
        return rc, buf.getvalue()


def only(findings, check):
    return [f for f in findings if f.check == check]


def assert_finding(findings, check, needle=None, count=None):
    hits = only(findings, check)
    if needle is not None:
        hits = [f for f in hits if needle in f.message or needle in f.path]
    if not hits:
        raise AssertionError("expected a %s finding%s; got %r" % (check, " mentioning %r" % needle if needle else "", findings))
    if count is not None and len(hits) != count:
        raise AssertionError("expected %d %s finding(s), got %d: %r" % (count, check, len(hits), hits))


def assert_clean(findings, check=None):
    hits = only(findings, check) if check else findings
    if hits:
        raise AssertionError("expected no %s findings; got %r" % (check or "", hits))


PY_MAIN = "import requests\n\n\ndef f():\n    return requests.get\n"

# ── declared-unused · Python ─────────────────────────────────────────────────


@case("an imported pin passes; a pin nothing imports is a finding")
def _():
    fx = Fixture({"requirements.txt": "requests==2.33.1\nhumanize==4.9.0\n", "app.py": PY_MAIN})
    f = fx.findings(["declared-unused"])
    assert_finding(f, "declared-unused", "humanize", count=1)
    assert not [x for x in f if "requests" in x.message]


@case("the distribution -> module table: Pillow satisfied by `import PIL`, not by its absence")
def _():
    ok = Fixture({"requirements.txt": "Pillow==11.0.0\n", "a.py": "from PIL import Image\n"})
    assert_clean(ok.findings(["declared-unused"]))
    bad = Fixture({"requirements.txt": "Pillow==11.0.0\n", "a.py": "import os\n"})
    assert_finding(bad.findings(["declared-unused"]), "declared-unused", "Pillow")


@case("`import-name:` in .house-rules.conf maps a dist the table does not know")
def _():
    files = {"requirements.txt": "acme-toolkit==1.0\n", "a.py": "import acmetk\n"}
    assert_finding(Fixture(files).findings(["declared-unused"]), "declared-unused", "acme-toolkit")
    assert_clean(Fixture(files, "import-name: acme-toolkit | acmetk\n").findings(["declared-unused"]))
    bad = Fixture(files, "import-name: acme-toolkit | not a module!\n")
    assert_finding(bad.findings(["declared-unused"]), "config-error", "dotted module name")


@case("`indirect-use:` needs a three-word reason; a bare name is a config error, not a pass")
def _():
    files = {"requirements.txt": "gunicorn==23.0.0\n", "a.py": "import os\n"}
    ok = Fixture(files, "indirect-use: gunicorn | started by the container CMD\n")
    assert_clean(ok.findings(["declared-unused"]))
    bare = Fixture(files, "indirect-use: gunicorn |\n")
    f = bare.findings(["declared-unused"])
    assert_finding(f, "config-error", "three words")
    assert_finding(f, "declared-unused", "gunicorn")
    short = Fixture(files, "indirect-use: gunicorn | CMD\n")
    assert_finding(short.findings(["declared-unused"]), "config-error", "three words")


@case("an `indirect-use:` entry for a package nothing declares is a stale-allowlist finding")
def _():
    fx = Fixture({"requirements.txt": "requests==2.33.1\n", "a.py": PY_MAIN},
                 "indirect-use: tensorflow | user model files import it\n")
    assert_finding(fx.findings(["declared-unused"]), "stale-allowlist", "tensorflow")


@case("a `# dead-weight:` pragma on the pin line or the line above justifies it; two words do not")
def _():
    same = Fixture({"requirements.txt": "safetensors==0.8.0  # dead-weight: transitive of peft, pinned per OQ2\n", "a.py": "import os\n"})
    assert_clean(same.findings(["declared-unused"]))
    above = Fixture({"requirements.txt": "# dead-weight: transitive of peft, pinned per OQ2\nsafetensors==0.8.0\n", "a.py": "import os\n"})
    assert_clean(above.findings(["declared-unused"]))
    short = Fixture({"requirements.txt": "safetensors==0.8.0  # dead-weight: transitive\n", "a.py": "import os\n"})
    assert_finding(short.findings(["declared-unused"]), "config-error", "three words")
    two_above = Fixture({"requirements.txt": "# dead-weight: transitive of peft, pinned per OQ2\n\nsafetensors==0.8.0\n", "a.py": "import os\n"})
    assert_finding(two_above.findings(["declared-unused"]), "declared-unused", "safetensors")


@case("dotted prefixes: azure-servicebus is not satisfied by `import azure.identity`")
def _():
    wrong = Fixture({"requirements.txt": "azure-servicebus==7.14.3\n", "a.py": "import azure.identity\n"})
    assert_finding(wrong.findings(["declared-unused"]), "declared-unused", "azure-servicebus")
    right = Fixture({"requirements.txt": "azure-servicebus==7.14.3\n", "a.py": "from azure.servicebus import ServiceBusClient\n"})
    assert_clean(right.findings(["declared-unused"]))
    plain = Fixture({"requirements.txt": "azure-servicebus==7.14.3\n", "a.py": "import azure.servicebus\n"})
    assert_clean(plain.findings(["declared-unused"]))


@case("a lazy, in-function import counts")
def _():
    fx = Fixture({"requirements.txt": "timm==1.0.26\n", "a.py": "def load():\n    import timm\n    return timm\n"})
    assert_clean(fx.findings(["declared-unused"]))


@case("a relative import does not vouch for a same-named pin")
def _():
    fx = Fixture({"requirements.txt": "utils-lib==1.0\n", "pkg/__init__.py": "", "pkg/a.py": "from .utils_lib import x\n", "pkg/utils_lib.py": "x = 1\n"})
    assert_finding(fx.findings(["declared-unused"]), "declared-unused", "utils-lib")


@case("strings: an ALL-CAPS registry list reaches a Django app; a lowercase dict key does not")
def _():
    apps = Fixture({"requirements.txt": "django-cors-headers==4.6.0\n",
                    "settings.py": 'INSTALLED_APPS = [\n    "django.contrib.auth",\n    "corsheaders",\n]\n'})
    assert_clean(apps.findings(["declared-unused"]))
    key = Fixture({"requirements.txt": "django-cors-headers==4.6.0\n",
                   "settings.py": 'labels = {"corsheaders": "CORS"}\n'})
    assert_finding(key.findings(["declared-unused"]), "declared-unused", "django-cors-headers")


@case("strings: a dotted path anywhere reaches its root; importlib.import_module with a literal reaches a bare name")
def _():
    dotted = Fixture({"requirements.txt": "django-cors-headers==4.6.0\n",
                      "settings.py": 'MW = {"x": "corsheaders.middleware.CorsMiddleware"}\n'})
    assert_clean(dotted.findings(["declared-unused"]))
    imp = Fixture({"requirements.txt": "scikit-survival==0.27.0\n",
                   "a.py": 'import importlib\nmod = importlib.import_module("sksurv")\n'})
    assert_clean(imp.findings(["declared-unused"]))


@case("invoked by name: `black` in a Makefile counts; a /black/ path segment does not")
def _():
    files = {"requirements-dev.txt": "black==26.3.1\n", "a.py": "import os\n"}
    yes = Fixture(dict(files, Makefile="fmt:\n\tblack --check .\n"))
    assert_clean(yes.findings(["declared-unused"]))
    no = Fixture(dict(files, Makefile="docs:\n\tcat docs/black/README.md\n"))
    assert_finding(no.findings(["declared-unused"]), "declared-unused", "black")
    nowhere = Fixture(files)
    assert_finding(nowhere.findings(["declared-unused"]), "declared-unused", "black")


@case("a tool started from a command string in Python code counts; a path mention in prose does not")
def _():
    cmd = Fixture({"requirements.txt": "gunicorn==23.0.0\n",
                   "a.py": 'import subprocess\nsubprocess.run("gunicorn --bind 0.0.0.0:8888 app:create_app()", shell=True)\n'})
    assert_clean(cmd.findings(["declared-unused"]))
    prose = Fixture({"requirements.txt": "gunicorn==23.0.0\n", "a.py": 'HELP = "see docs/gunicorn for details"\n'})
    assert_finding(prose.findings(["declared-unused"]), "declared-unused", "gunicorn")


@case("pytest plugins are reached by the option they add: pytest-cov by --cov, pytest-env by env=")
def _():
    cov_yes = Fixture({"requirements-dev.txt": "pytest==8.0.0\npytest-cov==5.0.0\n", "Makefile": "test:\n\tpytest --cov=app\n", "a.py": ""})
    assert_clean(cov_yes.findings(["declared-unused"]))
    cov_no = Fixture({"requirements-dev.txt": "pytest==8.0.0\npytest-cov==5.0.0\n", "Makefile": "test:\n\tpytest -q\n", "a.py": ""})
    assert_finding(cov_no.findings(["declared-unused"]), "declared-unused", "pytest-cov")
    env_yes = Fixture({"requirements-dev.txt": "pytest==8.0.0\npytest-env==1.6.0\n", "pytest.ini": "[pytest]\nenv=TRACEBLOC_ENV=ci\n", "Makefile": "test:\n\tpytest\n", "a.py": ""})
    assert_clean(env_yes.findings(["declared-unused"]))


@case("setup.py: a literal install_requires is read; a computed one is cannot-parse; a requirements.txt read-through is fine")
def _():
    lit = Fixture({"setup.py": 'from setuptools import setup\nsetup(name="x", install_requires=["requests>=2", "humanize"])\n', "a.py": PY_MAIN})
    f = lit.findings(["declared-unused"])
    assert_finding(f, "declared-unused", "humanize")
    assert_clean(f, "cannot-parse")
    computed = Fixture({"setup.py": 'from setuptools import setup\nsetup(name="x", install_requires=compute())\n', "a.py": ""})
    assert_finding(computed.findings(["declared-unused"]), "cannot-parse", "install_requires")
    delegated = Fixture({"setup.py": 'from setuptools import setup\nsetup(name="x", install_requires=open("requirements.txt").read().splitlines())\n',
                         "requirements.txt": "requests==2.33.1\n", "a.py": PY_MAIN})
    assert_clean(delegated.findings(["declared-unused"]))


@case("pyproject: [project] dependencies and optional extras are read; a pragma above the entry justifies it")
def _():
    if DW.tomllib is None:
        return  # python < 3.11 cannot run this case; the gate runs 3.12
    fx = Fixture({"pyproject.toml": '[project]\nname = "x"\ndependencies = ["requests>=2"]\n\n[project.optional-dependencies]\nboost = [\n  "xgboost==3.2.0",\n  # dead-weight: reached through importlib in require_framework()\n  "lightgbm==4.6.0",\n]\n',
                  "a.py": PY_MAIN})
    f = fx.findings(["declared-unused"])
    assert_finding(f, "declared-unused", "xgboost", count=1)
    assert not [x for x in f if "lightgbm" in x.message]


@case("a Python file that does not parse is a cannot-parse finding; `exclude:` in the config removes it")
def _():
    files = {"requirements.txt": "requests==2.33.1\n", "a.py": PY_MAIN, "legacy/old.py": "print 'py2'\n"}
    assert_finding(Fixture(files).findings(["declared-unused"]), "cannot-parse", "legacy/old.py")
    assert_clean(Fixture(files, "exclude: legacy/*\n").findings(["declared-unused"]))


@case("a repo with no dependency files at all yields nothing (there is nothing to be dead)")
def _():
    assert_clean(Fixture({"a.py": PY_MAIN, "README.md": "hi\n"}).findings(["declared-unused"]))


@case("a pragma on a justified pin does not leak onto the unjustified pin below it")
def _():
    fx = Fixture({"requirements.txt": "safetensors==0.8.0  # dead-weight: transitive of peft, pinned per OQ2\nhumanize==4.9.0\n", "a.py": "import os\n"})
    f = fx.findings(["declared-unused"])
    assert_finding(f, "declared-unused", "humanize", count=1)
    assert not [x for x in f if "safetensors" in x.message]


@case("setup.py's own INSTALL_REQUIRES list, and any *REQUIRE*/*PACKAGES* constant, vouch for nothing")
def _():
    via_const = Fixture({"setup.py": 'from setuptools import setup\nINSTALL_REQUIRES = ["humanize"]\nsetup(name="x", install_requires=INSTALL_REQUIRES)\n', "a.py": "import os\n"})
    assert_finding(via_const.findings(["declared-unused"]), "declared-unused", "humanize")
    # A constant whose NAME says nothing (`DEPS`) is still a declaration when it
    # lives in setup.py: the file, not the spelling, is what disqualifies it.
    via_deps = Fixture({"setup.py": 'from setuptools import setup\nDEPS = ["humanize"]\nsetup(name="x", install_requires=DEPS)\n', "a.py": "import os\n"})
    assert_finding(via_deps.findings(["declared-unused"]), "declared-unused", "humanize")
    elsewhere = Fixture({"requirements.txt": "humanize==4.9.0\n", "deps.py": 'REQUIRED_PACKAGES = ["humanize"]\n'})
    assert_finding(elsewhere.findings(["declared-unused"]), "declared-unused", "humanize")
    registry = Fixture({"requirements.txt": "humanize==4.9.0\n", "conf.py": 'PLUGINS = ["humanize"]\n'})
    assert_clean(registry.findings(["declared-unused"]))


@case("the distribution table matches after normalisation: ruamel.yaml is cleared by `import ruamel.yaml`")
def _():
    ok = Fixture({"requirements.txt": "ruamel.yaml==0.18.6\n", "a.py": "import ruamel.yaml\n"})
    assert_clean(ok.findings(["declared-unused"]))
    no = Fixture({"requirements.txt": "ruamel.yaml==0.18.6\n", "a.py": "import os\n"})
    assert_finding(no.findings(["declared-unused"]), "declared-unused", "ruamel.yaml")


# ── declared-unused · Node ───────────────────────────────────────────────────

PKG = '{\n  "name": "x",\n  "scripts": {"lint": "eslint ."},\n  "dependencies": {%s},\n  "devDependencies": {%s}\n}\n'


@case("node: import / require / dynamic import / css @import each reach a package; an unreferenced one is a finding")
def _():
    fx = Fixture({"package.json": PKG % ('"react": "^18", "clsx": "^2", "@radix-ui/react-dialog": "^1", "katex": "^0.16", "lodash": "^4"', '"eslint": "^9"'),
                  "src/a.tsx": "import React from 'react';\nimport { clsx } from \"clsx\";\nconst D = () => import('@radix-ui/react-dialog/dist/index.js');\n",
                  "src/b.js": "const k = require('katex');\n",
                  "src/s.css": "@import \"katex/dist/katex.min.css\";\n"})
    f = fx.findings(["declared-unused"])
    assert_finding(f, "declared-unused", "lodash", count=1)


@case("node: @types/x is reached by x; @types/scope__name by @scope/name; an orphan @types is a finding")
def _():
    ok = Fixture({"package.json": PKG % ('"react-dom": "^18", "react": "^18", "@testing-library/jest-dom": "^6"', '"@types/react-dom": "^18", "@types/testing-library__jest-dom": "^5"'),
                  "src/a.tsx": "import React from 'react';\nimport { createRoot } from 'react-dom/client';\nimport '@testing-library/jest-dom';\n"})
    assert_clean(ok.findings(["declared-unused"]))
    orphan = Fixture({"package.json": PKG % ('"react": "^18"', '"@types/lodash": "^4"'), "src/a.tsx": "import React from 'react';\n"})
    assert_finding(orphan.findings(["declared-unused"]), "declared-unused", "@types/lodash")


@case("node: a peer-only package passes when the package that requires it is declared AND used, not otherwise")
def _():
    ok = Fixture({"package.json": PKG % ('"@mui/material": "^7", "@emotion/react": "^11", "react": "^18"', ''),
                  "src/a.tsx": "import Button from '@mui/material/Button';\nimport React from 'react';\n"})
    assert_clean(ok.findings(["declared-unused"]))
    alone = Fixture({"package.json": PKG % ('"@emotion/react": "^11", "react": "^18"', ''), "src/a.tsx": "import React from 'react';\n"})
    assert_finding(alone.findings(["declared-unused"]), "declared-unused", "@emotion/react")
    undeclared_requirer = Fixture({"package.json": PKG % ('"@emotion/react": "^11", "react": "^18"', ''),
                                   "src/a.tsx": "import React from 'react';\nimport Button from '@mui/material/Button';\n"})
    assert_finding(undeclared_requirer.findings(["declared-unused"]), "declared-unused", "@emotion/react")


@case("node: sass is implied by a .scss file, typescript by tsconfig.json; neither without them")
def _():
    yes = Fixture({"package.json": PKG % ('"react": "^18"', '"sass": "^1", "typescript": "^5"'),
                   "src/a.tsx": "import React from 'react';\n", "src/x.module.scss": ".a{}\n", "tsconfig.json": "{}\n"})
    assert_clean(yes.findings(["declared-unused"]))
    no = Fixture({"package.json": PKG % ('"react": "^18"', '"sass": "^1"'), "src/a.jsx": "import React from 'react';\n"})
    assert_finding(no.findings(["declared-unused"]), "declared-unused", "sass")


@case("node: eslint-config-next is reached by extends \"next/...\"; eslint-plugin-react by \"plugin:react/...\"")
def _():
    cfg_yes = Fixture({"package.json": PKG % ('"react": "^18"', '"eslint": "^9", "eslint-config-next": "^15", "eslint-plugin-react": "^7"'),
                       "eslint.config.mjs": 'export default [...compat.extends("next/core-web-vitals"), ...compat.extends("plugin:react/recommended")];\n',
                       "src/a.jsx": "import React from 'react';\n"})
    assert_clean(cfg_yes.findings(["declared-unused"]))
    cfg_no = Fixture({"package.json": PKG % ('"react": "^18"', '"eslint": "^9", "eslint-config-next": "^15"'),
                      "eslint.config.mjs": "export default [];\n", "src/a.jsx": "import React from 'react';\n"})
    assert_finding(cfg_no.findings(["declared-unused"]), "declared-unused", "eslint-config-next")


@case("node: a tool named in package.json scripts is invoked; a coverage provider is reached by --coverage")
def _():
    fx = Fixture({"package.json": '{"name":"x","scripts":{"test":"vitest run --coverage","build":"tsup"},"devDependencies":{"vitest":"^3","@vitest/coverage-v8":"^3","tsup":"^8","rimraf":"^6"}}\n',
                  "src/a.ts": "export const a = 1;\n", "tsconfig.json": "{}\n"})
    f = fx.findings(["declared-unused"])
    assert_finding(f, "declared-unused", "rimraf", count=1)


@case("node: @types/node is tied to a tsconfig like every implied tool, never accepted on its name alone")
def _():
    yes = Fixture({"package.json": PKG % ('"react": "^18"', '"@types/node": "^22"'), "src/a.tsx": "import React from 'react';\n", "tsconfig.json": "{}\n"})
    assert_clean(yes.findings(["declared-unused"]))
    no = Fixture({"package.json": PKG % ('"react": "^18"', '"@types/node": "^22"'), "src/a.jsx": "import React from 'react';\n"})
    assert_finding(no.findings(["declared-unused"]), "declared-unused", "@types/node")


# ── full-python-base ─────────────────────────────────────────────────────────


@case("full-python-base: python:3.11 is a finding; -slim and -alpine pass; other images are ignored")
def _():
    fx = Fixture({"Dockerfile": "FROM python:3.11\n", "Dockerfile.slim": "FROM python:3.11-slim\n",
                  "Dockerfile.alpine": "FROM public.ecr.aws/docker/library/python:3.12-alpine AS build\n",
                  "Dockerfile.cuda": "FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04\n"})
    f = fx.findings(["full-python-base"])
    assert_finding(f, "full-python-base", "Dockerfile", count=1)
    assert f[0].path == "Dockerfile"


@case("full-python-base: a same-line comment of three or more words justifies; two words do not; AS-stage FROMs count")
def _():
    ok = Fixture({"Dockerfile": "FROM python:3.11  # cv2 needs libGL from the full image\n"})
    assert_clean(ok.findings(["full-python-base"]))
    short = Fixture({"Dockerfile": "FROM python:3.11  # needs libgl\n"})
    assert_finding(short.findings(["full-python-base"]), "full-python-base")
    staged = Fixture({"Dockerfile": "FROM python:3.11 AS build\nFROM python:3.11-slim\n"})
    assert_finding(staged.findings(["full-python-base"]), "full-python-base", count=1)


@case("full-python-base: an ARG default resolves the tag; an ARG without a default is cannot-tell")
def _():
    ok = Fixture({"Dockerfile": "ARG PY=3.11-slim\nFROM python:${PY}\n"})
    assert_clean(ok.findings(["full-python-base"]))
    full = Fixture({"Dockerfile": "ARG PY=3.11\nFROM python:$PY\n"})
    assert_finding(full.findings(["full-python-base"]), "full-python-base", "full Debian")
    unknown = Fixture({"Dockerfile": "ARG PY\nFROM python:${PY}\n"})
    assert_finding(unknown.findings(["full-python-base"]), "full-python-base", "cannot be told")


# ── cuda-torch-on-cpu ────────────────────────────────────────────────────────

REQ_TORCH = "numpy==2.2.0\ntorch==2.13.0\n"
DOCKER_CPU = "FROM python:3.11-slim\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\n"
WF_CPU = "name: t\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          python -m pip install -r requirements.txt\n"


@case("cuda-torch: a plain torch pin installed by a CPU Dockerfile is a finding, naming the installer")
def _():
    fx = Fixture({"requirements.txt": REQ_TORCH, "Dockerfile": DOCKER_CPU})
    f = fx.findings(["cuda-torch-on-cpu"])
    assert_finding(f, "cuda-torch-on-cpu", "Dockerfile:3", count=1)
    assert f[0].path == "requirements.txt" and f[0].line == 2


@case("cuda-torch: the file naming the CPU index passes; so does a +cpu pin; so does the index on the install line")
def _():
    idx = Fixture({"requirements.txt": "--extra-index-url https://download.pytorch.org/whl/cpu\n" + REQ_TORCH, "Dockerfile": DOCKER_CPU})
    assert_clean(idx.findings(["cuda-torch-on-cpu"]))
    local = Fixture({"requirements.txt": 'numpy==2.2.0\ntorch==2.13.0+cpu; sys_platform == "linux"\ntorch==2.13.0; sys_platform == "darwin"\n', "Dockerfile": DOCKER_CPU})
    assert_clean(local.findings(["cuda-torch-on-cpu"]))
    line = Fixture({"requirements.txt": REQ_TORCH,
                    "Dockerfile": "FROM python:3.11-slim\nRUN pip install \\\n      --extra-index-url https://download.pytorch.org/whl/cpu \\\n      -r requirements.txt\n"})
    assert_clean(line.findings(["cuda-torch-on-cpu"]))


@case("cuda-torch: GPU contexts are out of scope -- a CUDA base image, a *_cuda requirements file, a GPU runner, a cu12x index")
def _():
    base = Fixture({"requirements.txt": REQ_TORCH, "Dockerfile.gpu": "FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04\nRUN pip install -r requirements.txt\n"})
    assert_clean(base.findings(["cuda-torch-on-cpu"]))
    named = Fixture({"requirements_cuda.txt": REQ_TORCH, "Dockerfile": "FROM python:3.11-slim\nRUN pip install -r requirements_cuda.txt\n"})
    assert_clean(named.findings(["cuda-torch-on-cpu"]))
    runner = Fixture({"requirements.txt": REQ_TORCH, ".github/workflows/t.yml": WF_CPU.replace("ubuntu-latest", "[self-hosted, gpu]")})
    assert_clean(runner.findings(["cuda-torch-on-cpu"]))
    cu = Fixture({"requirements.txt": "--index-url https://download.pytorch.org/whl/cu124\n" + REQ_TORCH, "Dockerfile": DOCKER_CPU})
    assert_clean(cu.findings(["cuda-torch-on-cpu"]))


@case("cuda-torch: a hosted-runner workflow step is an installer; PIP_EXTRA_INDEX_URL with the CPU index in that workflow clears it")
def _():
    wf = Fixture({"requirements.txt": REQ_TORCH, ".github/workflows/t.yml": WF_CPU})
    assert_finding(wf.findings(["cuda-torch-on-cpu"]), "cuda-torch-on-cpu", ".github/workflows/t.yml:8", count=1)
    env = Fixture({"requirements.txt": REQ_TORCH,
                   ".github/workflows/t.yml": WF_CPU.replace("    steps:", "    env:\n      PIP_EXTRA_INDEX_URL: https://download.pytorch.org/whl/cpu\n    steps:")})
    assert_clean(env.findings(["cuda-torch-on-cpu"]))


@case("cuda-torch: an include chain (-r) carries the installer down; a file nothing installs is not judged")
def _():
    chain = Fixture({"requirements.txt": REQ_TORCH, "requirements-dev.txt": "-r requirements.txt\npytest==8.0.0\n",
                     ".github/workflows/t.yml": WF_CPU.replace("requirements.txt", "requirements-dev.txt")})
    f = chain.findings(["cuda-torch-on-cpu"])
    assert_finding(f, "cuda-torch-on-cpu", count=1)
    assert f[0].path == "requirements.txt"
    orphan = Fixture({"requirements.txt": REQ_TORCH, "README.md": "pip install -r requirements.txt\n"})
    assert_clean(orphan.findings(["cuda-torch-on-cpu"]))


@case("cuda-torch: GPU context and CPU-index env are read per JOB -- a GPU job or a comment beside a CPU job exempts nothing")
def _():
    two_jobs = ("name: t\non: [push]\njobs:\n"
                "  gpu:\n    runs-on: [self-hosted, gpu]\n    env:\n      PIP_EXTRA_INDEX_URL: https://download.pytorch.org/whl/cpu\n    steps:\n      - run: echo hi\n"
                "  cpu:\n    # runs-on: gpu -- a comment, not a runner\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          pip install -r requirements.txt\n")
    fx = Fixture({"requirements.txt": REQ_TORCH, ".github/workflows/t.yml": two_jobs})
    f = fx.findings(["cuda-torch-on-cpu"])
    assert_finding(f, "cuda-torch-on-cpu", ".github/workflows/t.yml:15", count=1)
    same_job = two_jobs.replace("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    env:\n      PIP_EXTRA_INDEX_URL: https://download.pytorch.org/whl/cpu\n")
    assert_clean(Fixture({"requirements.txt": REQ_TORCH, ".github/workflows/t.yml": same_job}).findings(["cuda-torch-on-cpu"]))


# ── config + CLI ─────────────────────────────────────────────────────────────


@case("`dead-weight-disable:` turns one check off; an unknown check name is a config error")
def _():
    off = Fixture({"Dockerfile": "FROM python:3.11\n"}, "dead-weight-disable: full-python-base\n")
    assert_clean(off.findings())
    bad = Fixture({"Dockerfile": "FROM python:3.11-slim\n"}, "dead-weight-disable: no-such-check\n")
    assert_finding(bad.findings(), "config-error", "unknown check")


@case("main(): findings exit 1, --soft-fail exits 0 and still prints them, a clean tree exits 0, --check filters")
def _():
    fx = Fixture({"requirements.txt": "humanize==4.9.0\n", "a.py": "import os\n", "Dockerfile": "FROM python:3.11\n"})
    rc, out = fx.main()
    assert rc == 1 and "declared-unused" in out and "full-python-base" in out, (rc, out)
    rc, out = fx.main("--soft-fail")
    assert rc == 0 and "humanize" in out, (rc, out)
    rc, out = fx.main("--check", "full-python-base")
    assert rc == 1 and "humanize" not in out and "full-python-base" in out, (rc, out)
    clean = Fixture({"requirements.txt": "requests==2.33.1\n", "a.py": PY_MAIN})
    rc, out = clean.main()
    assert rc == 0 and "0 finding(s)" in out, (rc, out)


@case("main(): --github emits ::error annotations, ::warning under --soft-fail, and --summary appends a table")
def _():
    fx = Fixture({"Dockerfile": "FROM python:3.11\n"})
    rc, out = fx.main("--github")
    assert "::error file=Dockerfile,line=1" in out, out
    rc, out = fx.main("--github", "--soft-fail")
    assert "::warning file=Dockerfile,line=1" in out, out
    summary = os.path.join(fx.dir, "summary.md")
    fx.main("--summary", summary)
    text = open(summary, encoding="utf-8").read()
    assert "| full-python-base | `Dockerfile` | 1 |" in text, text


def main():
    passed, failed = 0, 0
    for name, fn in RESULTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 -- every failure must be reported, whatever its type
            failed += 1
            print("FAIL: %s -- %s: %s" % (name, type(exc).__name__, exc))
        else:
            passed += 1
            print("ok    %s" % name)
    print("dead-weight-selftest: %d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
