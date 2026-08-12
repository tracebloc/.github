# Makefile for tracebloc/.github — uniform entry points (backend#1606).
#
# Every active tracebloc repo exposes the SAME targets, so "run your tests
# before you push" stops being a rule you can only obey with per-repo tribal
# knowledge:
#
#   make check      lint + the selftests.  Budget: under 60 s.
#   make check-all  everything CI runs, including what needs a downloaded tool.
#   make setup      preflight what those targets need, and install a git
#                   pre-push hook that runs `make check` (skip: --no-verify).
#
# This file is a THIN WRAPPER. Every command below is copied from the workflow
# that already runs it — no new tool, no new config, no new rule — because a
# local check that disagrees with CI is worse than no local check.
#
#   actionlint.yml            -> the `actionlint` target
#   code-quality-caller.yml   -> `ruff`, `shellcheck`, `house-rules`,
#     (which calls code-quality.yml    `action-pins`, `credential-scan`
#      with python: true, shell: true,
#      soft-fail: false, action-pins: true, action-pins-soft-fail: false)
#   caller-drift.yml          -> `selftest-caller-drift`
#   blocked-gate-selftest.yml -> `selftest-blocked-marker`
#   standards-sync.yml        -> `selftest-standards-sync`
#   version-bump-gate-selftest.yml -> `selftest-version-bump-gate`
#
# When one of those workflows changes, change the matching line here.
#
# TWO THINGS CI DOES THAT `check` DOES NOT — named, not hidden:
#
#   credential-scan (gitleaks)  in `check-all`. Needs a binary that CI
#     downloads per run from a pinned, digest-verified release tarball and
#     that is on no developer machine by default. See the target.
#
#   conformance-gate.yml  in NEITHER, and cannot be. It does not check the
#     tree at all: it polls the GitHub API for caller-drift.yml's verdict on
#     the PR head sha. There is no head sha and no run to poll before you
#     push. What it gates on IS local, though — `check` runs the
#     caller-drift SELFTEST, which is the half of caller-drift.yml that
#     needs no token. The other half (`audit`) queries 20 repos' workflow
#     files through the API; see `audit` below to run it deliberately.

.DEFAULT_GOAL := help

# Tools are taken from PATH. Unlike backend/ and e2e-test-agent/ this repo has
# no venv and no requirements file to install a pin into, so `setup` preflights
# and version-checks instead of installing. Override any of these to point at a
# specific build.
PYTHON      ?= python3
RUFF        ?= ruff
SHELLCHECK  ?= shellcheck
ACTIONLINT  ?= actionlint
GITLEAKS    ?= gitleaks

# Pins, each mirroring the workflow that sets it. A tool whose rule set has
# moved gives a different answer to the same tree than CI does, which is the
# one thing this file exists to prevent. Bump these WITH the workflow.
#
# NO INLINE `# comment` AFTER THESE VALUES. make strips the comment but KEEPS
# the whitespace ahead of it, so `?= 0.15.20   # ...` defines the version as
# "0.15.20   " — and version-check then warned that ruff 0.15.20 differs from
# the pinned "0.15.20   " on a machine that matched exactly. A version guard
# that cries wolf is a version guard people learn to ignore. (Caught locally
# before this file was pushed.)
#
# code-quality.yml, `ruff-version` default:
RUFF_VERSION ?= 0.15.20
# actionlint.yml, ACTIONLINT_VERSION:
ACTIONLINT_VERSION ?= 1.7.12
# code-quality.yml, GITLEAKS_VERSION:
GITLEAKS_VERSION ?= 8.30.1

# code-quality.yml's ruff-select default, used because this repo has no ruff
# config of its own. --isolated so a stray user-level config cannot change the
# answer, matching what the workflow does in the same situation.
RUFF_SELECT ?= E4,E7,E9,F

# code-quality.yml's shellcheck job: severity `error` (the input default, and
# this caller does not override it) and SC1091 excluded.
SHELLCHECK_SEVERITY ?= error

.PHONY: help
help:
	@echo "tracebloc/.github — make targets"
	@echo
	@echo "  check       ruff + shellcheck + house-rules + action-pins + actionlint"
	@echo "              + all four selftests — run this before every push (~18 s)"
	@echo "  check-all   the same, plus gitleaks (needs the gitleaks binary)"
	@echo "  setup       preflight the tools check needs; installs the pre-push hook"
	@echo "  install-hooks  (re)install the git pre-push hook that runs 'make check'"
	@echo
	@echo "  lint            ruff + shellcheck + house-rules + action-pins + actionlint"
	@echo "  selftests       all four gate selftests"
	@echo "  credential-scan gitleaks over the whole history, as code-quality.yml runs it"
	@echo "  audit           caller-drift.py against the live org — needs a token"
	@echo
	@echo "  Not reproducible locally, by construction:"
	@echo "    conformance-gate.yml polls the API for caller-drift's verdict on a"
	@echo "    pushed head sha. 'check' runs the selftest it ultimately gates on."

# ---- check / check-all -------------------------------------------
#
# MEASURED on this tree: check is green in ~18 s, of which ~15 s is the
# version-bump-gate selftest (it shells out to git in a temp repo per case).
# Everything else together is under 3 s.
.PHONY: check
check: lint selftests
	@echo "==> check: green (gitleaks runs in 'make check-all')"

# check-all adds the one CI job that needs a tool nobody has by default.
# It does NOT add a slow test tier, because there isn't one: this repo's
# heavy jobs are the two CRONS (kanban-reconcile, kanban-archive) and the
# `audit` halves of caller-drift/standards-sync, all of which need
# PROJECTS_KANBAN_TOKEN and reach out to the live org. Those are not
# pre-push checks under any definition; `audit` below runs one on demand.
.PHONY: check-all
check-all: check credential-scan
	@echo "==> check-all: green"

# ---- lint --------------------------------------------------------

.PHONY: lint
lint: ruff shellcheck house-rules action-pins actionlint

# ruff: code-quality.yml's `ruff` job in all-files mode. This repo has no ruff
# config, so the workflow falls back to --isolated --select <ruff-select>; that
# fallback is reproduced exactly rather than approximated.
.PHONY: ruff
ruff: guard-ruff
	$(RUFF) check --isolated --select $(RUFF_SELECT) .

# shellcheck: code-quality.yml's `shellcheck` job in all-files mode, INCLUDING
# its file selection. That selection is not "*.sh": the job takes every tracked
# file whose extension is .sh/.bash/.ksh OR whose first line is a sh/bash/dash/ksh
# shebang, and explicitly skips .bats/.ps1/.psm1/.zsh. Reproduced here because a
# looser or tighter local glob checks a different set of files than the gate and
# then disagrees with it. Today it selects scripts/house-rules.sh and
# scripts/tests/version-bump-gate-selftest.sh.
.PHONY: shellcheck
shellcheck: guard-shellcheck
	@set -e; \
	 files=$$(mktemp); \
	 all=$$(mktemp); \
	 git ls-files > "$$all"; \
	 while IFS= read -r f; do \
	   [ -f "$$f" ] || continue; \
	   case "$$f" in \
	     *.sh|*.bash|*.ksh) printf '%s\n' "$$f" >> "$$files" ;; \
	     *.bats|*.ps1|*.psm1|*.zsh) ;; \
	     *) head -n 1 "$$f" 2>/dev/null \
	          | grep -Eq '^#![[:space:]]*[^[:space:]]*(/|[[:space:]])(ba|da|k)?sh([[:space:]]|$$)' \
	          && printf '%s\n' "$$f" >> "$$files" || true ;; \
	   esac; \
	 done < "$$all"; \
	 rm -f "$$all"; \
	 n=$$(wc -l < "$$files" | tr -d ' '); \
	 echo "Shell files to check: $$n"; \
	 if [ "$$n" = "0" ]; then \
	   echo "shellcheck: no shell files in scope"; rm -f "$$files"; exit 0; \
	 fi; \
	 tr '\n' '\0' < "$$files" \
	   | xargs -0 $(SHELLCHECK) --severity=$(SHELLCHECK_SEVERITY) --format=gcc --exclude=SC1091; \
	 rc=$$?; rm -f "$$files"; \
	 [ "$$rc" = 0 ] && echo "shellcheck: clean"; exit $$rc

# house-rules: code-quality.yml's `house-rules` job in all-files mode. Note that
# CI checks out tracebloc/.github@main to get this script — in THIS repo the
# script is simply the working copy, which is the point: a change to
# house-rules.sh is checkable here before every other repo consumes it at @main.
# POSIX sh + awk only, so there is nothing to install.
.PHONY: house-rules
house-rules:
	./scripts/house-rules.sh --all

# action-pins: code-quality.yml's `action-pins` job, EXTRACTED from the workflow
# rather than reimplemented. The job body is a python heredoc, and a
# hand-copied second version of a supply-chain gate is a version that can
# silently disagree with the one that actually gates merges — the failure mode
# backend#1606 is about, in the file where it would cost the most. Extracting
# means this target runs the gate's own source, byte for byte, so drift is
# impossible by construction. Same posture as
# scripts/tests/version-bump-gate-selftest.sh, which extracts its gate's `run:`
# block for the same reason.
#
# The extraction refuses to guess: exactly one `python3 - <<'PY'` block must
# exist and the extracted body must be non-empty, or this fails loudly. A guard
# that cannot verify must not claim it did.
#
# SOFT_FAIL=false because that is what this repo's caller passes
# (action-pins-soft-fail: false) — findings are red here, not advisory.
.PHONY: action-pins
action-pins:
	@set -e; \
	 wf=.github/workflows/code-quality.yml; \
	 n=$$(grep -c "python3 - <<'PY'" "$$wf" || true); \
	 if [ "$$n" != "1" ]; then \
	   echo "error: expected exactly 1 \"python3 - <<'PY'\" block in $$wf, found $$n."; \
	   echo "       the action-pins job moved or gained a sibling — fix this target."; \
	   exit 1; \
	 fi; \
	 src=$$(mktemp); \
	 awk "/python3 - <<'PY'/{f=1;next} f&&/^          PY\$$/{f=0} f{sub(/^          /,\"\");print}" \
	   "$$wf" > "$$src"; \
	 if [ ! -s "$$src" ]; then \
	   echo "error: extracted an EMPTY action-pins script from $$wf — refusing to report clean."; \
	   rm -f "$$src"; exit 1; \
	 fi; \
	 SOFT_FAIL=false $(PYTHON) "$$src"; \
	 rc=$$?; rm -f "$$src"; exit $$rc

# actionlint: actionlint.yml's `Lint workflows` step. Its own header prescribes
# this exact invocation for local runs. -shellcheck is passed explicitly, as
# there, so the integration cannot be silently off — actionlint SKIPS every
# `run:`-block shell check and still exits 0 when shellcheck is missing, which
# is why guard-shellcheck is a prerequisite and not a suggestion.
.PHONY: actionlint
actionlint: guard-actionlint guard-shellcheck
	$(ACTIONLINT) -no-color -oneline -shellcheck $(SHELLCHECK)
	@echo "actionlint: 0 findings"

# ---- selftests ---------------------------------------------------
#
# The four gate selftests, each the `selftest` job of its workflow. All are
# token-free and network-free, which is exactly why they belong in `check`:
# in CI every one of them is behind a `paths:` filter, so a PR that refactors
# a gate without touching its named trigger paths never runs them. Locally
# there is no filter and no reason for one — they cost ~15 s together.
.PHONY: selftests
selftests: selftest-caller-drift selftest-blocked-marker selftest-standards-sync selftest-version-bump-gate

.PHONY: selftest-caller-drift
selftest-caller-drift: guard-pyyaml
	$(PYTHON) scripts/tests/caller-drift-selftest.py

.PHONY: selftest-blocked-marker
selftest-blocked-marker:
	$(PYTHON) scripts/tests/blocked-marker-selftest.py

.PHONY: selftest-standards-sync
selftest-standards-sync: guard-pyyaml
	$(PYTHON) scripts/tests/standards-sync-selftest.py

# The slowest of the four (~15 s): it builds a throwaway git repo per case.
.PHONY: selftest-version-bump-gate
selftest-version-bump-gate: guard-pyyaml
	bash scripts/tests/version-bump-gate-selftest.sh

# ---- CI steps that need something a working tree does not have ----

# credential-scan: code-quality.yml's `gitleaks` job in all-files mode.
#
# NOT in `check`, and the reason is the tool rather than the check: CI installs
# gitleaks per run from a version- and SHA-256-pinned release tarball, and it is
# on no developer machine by default. Making `check` depend on it would either
# fail every clean checkout or — far worse — get itself quietly skipped, and a
# credential scan that is skipped is a credential scan that reports clean.
#
# `gitleaks git` (commit-scoped, not `gitleaks dir`) with the workflow's flags,
# scanning the whole history exactly as all-files mode does.
.PHONY: credential-scan
credential-scan: guard-gitleaks
	$(GITLEAKS) git --no-banner --redact --report-format json \
	  --report-path $${TMPDIR:-/tmp}/gitleaks.json .
	@echo "gitleaks: clean"

# audit: caller-drift.py against the live org — the `audit` job of
# caller-drift.yml, and the check whose verdict conformance-gate.yml requires.
#
# NOT in check or check-all: it reads the workflow files of ~20 repos through
# the GitHub API, so it needs a token with that reach and it is neither fast nor
# offline. It is also not a check on THIS tree — it compares the tree's
# inventory against the live fleet, so its answer can change with no local
# commit. Run it deliberately when you touch repo-inventory.yml; the selftest in
# `check` covers the parser's own logic.
#
# --inventory and --source-dir are passed EXPLICITLY, exactly as caller-drift.yml
# passes them, even though both happen to match the script's own defaults today.
# This file's whole claim is that its commands are copied from the workflow
# rather than merely equivalent to it — and a default is the kind of thing that
# changes under you, which is how the two would quietly stop agreeing.
.PHONY: audit
audit: guard-pyyaml
	@command -v gh >/dev/null 2>&1 || { echo "audit needs the gh CLI on PATH"; exit 1; }
	@echo "note: reads ~20 repos through the API; needs a token with org read access."
	$(PYTHON) scripts/caller-drift.py \
	  --inventory repo-inventory.yml \
	  --source-dir .

# ---- setup / hooks -----------------------------------------------
#
# There is nothing to install: no venv, no lockfile, no package. So `setup` does
# the two things that are actually load-bearing here — prove every tool `check`
# needs is present (and warn where a version differs from the pin CI uses), then
# install the pre-push hook.
.PHONY: setup
setup: guard-ruff guard-shellcheck guard-actionlint guard-pyyaml
	@$(MAKE) --no-print-directory version-check
	@echo "==> setup: tools present; run 'make check'"
	@echo "    'make check-all' additionally needs gitleaks:"
	@echo "      macOS: brew install gitleaks   Linux: see code-quality.yml for the pinned release"
	@$(MAKE) --no-print-directory install-hooks

# version-check warns, and deliberately does not fail. A mismatch is a real
# risk — ruff's rule set and actionlint's checks move between releases, so a
# different version can give a different answer to the same tree — but this
# repo pins nothing locally to install FROM, so hard-failing would block a
# contributor with no in-repo way to comply. Say it loudly, let CI arbitrate.
.PHONY: version-check
version-check:
	@got=$$($(RUFF) --version 2>/dev/null | awk '{print $$2}'); \
	 [ "$$got" = "$(RUFF_VERSION)" ] \
	   || echo "warning: ruff $$got on PATH, CI pins $(RUFF_VERSION) — findings may differ."
	@got=$$($(ACTIONLINT) -version 2>/dev/null | head -1); \
	 [ "$$got" = "$(ACTIONLINT_VERSION)" ] \
	   || echo "warning: actionlint $$got on PATH, CI pins $(ACTIONLINT_VERSION) — findings may differ."

# install-hooks: a pre-push hook that runs `make check`, so "run the checks
# before you push" is carried by tooling rather than by memory. Factored out of
# `setup` so it is independently runnable.
#
# Honest by design: it catches FORGETTING, not defiance — `git push --no-verify`
# skips it and always will. It refuses to clobber a pre-push hook that is
# already there and not ours (e.g. one pre-commit manages) rather than silently
# stomping a contributor's setup.
#
# `git rev-parse --git-path hooks` rather than a hard-coded `.git/hooks`, so it
# lands correctly inside a linked worktree or a submodule.
.PHONY: install-hooks
install-hooks:
	@if ! git rev-parse --git-dir >/dev/null 2>&1; then \
	  echo "note: not a git checkout — skipping pre-push hook install"; \
	else \
	  hook="$$(git rev-parse --git-path hooks)/pre-push"; \
	  if [ -e "$$hook" ] && ! grep -q 'tracebloc pre-push hook' "$$hook" 2>/dev/null; then \
	    echo "note: $$hook already exists and is not ours — leaving it untouched."; \
	    echo "      add 'make check' to it, or remove it and re-run 'make install-hooks'."; \
	  else \
	    mkdir -p "$$(dirname "$$hook")" && \
	    printf '%s\n' \
	      '#!/bin/sh' \
	      '# tracebloc pre-push hook installed by make setup (backend#1606).' \
	      '# Runs make check so a push that would be red in CI is caught locally first.' \
	      '# It catches forgetting, not defiance: git push --no-verify skips it.' \
	      '#' \
	      '# Nothing to check on a delete/no-op push: a branch delete streams a' \
	      '# local sha of all-zeros on stdin (no new commits). Skip so a red tree' \
	      '# cannot block "git push --delete".' \
	      'z=0000000000000000000000000000000000000000' \
	      'had_update=0' \
	      'while read -r _ local_sha _ _; do' \
	      '  [ "$$local_sha" != "$$z" ] && had_update=1' \
	      'done' \
	      '[ "$$had_update" = 0 ] && exit 0' \
	      '#' \
	      '# Degrade gracefully when make is absent: GUI git clients launch hooks' \
	      '# with a minimal PATH, and several do not expose --no-verify. Skipping' \
	      '# beats hard-blocking every push with "make: command not found".' \
	      'command -v make >/dev/null 2>&1 || exit 0' \
	      '#' \
	      '# Git exports GIT_DIR/GIT_WORK_TREE/etc into hook processes; a nested git' \
	      '# invocation then fails in a linked worktree with exit status 128. Clear' \
	      '# them so make check runs as it would from the shell.' \
	      'unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR GIT_OBJECT_DIRECTORY' \
	      'exec make check' > "$$hook" && \
	    chmod +x "$$hook" && \
	    echo "==> pre-push hook installed at $$hook" && \
	    echo "    'make check' now runs before each push (skip once with: git push --no-verify)"; \
	  fi; \
	fi

# ---- guards ------------------------------------------------------
#
# Each names the tool, why it is needed, and how to get it. A missing linter
# must fail loudly: silently checking nothing is the outcome this file exists
# to prevent.

.PHONY: guard-ruff
guard-ruff:
	@command -v $(RUFF) >/dev/null 2>&1 || { \
	  echo "ruff is not on PATH — code-quality.yml's ruff job needs it:"; \
	  echo "  pipx install ruff==$(RUFF_VERSION)   (or: brew install ruff)"; \
	  exit 1; }

.PHONY: guard-shellcheck
guard-shellcheck:
	@command -v $(SHELLCHECK) >/dev/null 2>&1 || { \
	  echo "shellcheck is not on PATH — the shellcheck job needs it, and"; \
	  echo "actionlint SKIPS every run:-block shell check without it:"; \
	  echo "  macOS: brew install shellcheck"; \
	  echo "  Debian/Ubuntu: apt-get install shellcheck"; \
	  exit 1; }

.PHONY: guard-actionlint
guard-actionlint:
	@command -v $(ACTIONLINT) >/dev/null 2>&1 || { \
	  echo "actionlint is not on PATH — actionlint.yml is a required check here:"; \
	  echo "  macOS: brew install actionlint"; \
	  echo "  Linux: go install github.com/rhysd/actionlint/cmd/actionlint@latest"; \
	  exit 1; }

.PHONY: guard-gitleaks
guard-gitleaks:
	@command -v $(GITLEAKS) >/dev/null 2>&1 || { \
	  echo "gitleaks is not on PATH — 'make check-all' needs it:"; \
	  echo "  macOS: brew install gitleaks"; \
	  echo "  Linux: install v$(GITLEAKS_VERSION) from the pinned release tarball"; \
	  echo "         (URL and SHA-256 are in .github/workflows/code-quality.yml)"; \
	  exit 1; }

# PyYAML, which three of the four selftests import. Every workflow that runs
# them pip-installs 'pyyaml==6.0.2' on a clean setup-python first, so an
# ImportError here is a missing dependency and not a broken test.
.PHONY: guard-pyyaml
guard-pyyaml:
	@$(PYTHON) -c 'import yaml' 2>/dev/null || { \
	  echo "PyYAML is missing from $(PYTHON) — the selftests import it:"; \
	  echo "  $(PYTHON) -m pip install 'pyyaml==6.0.2'"; \
	  exit 1; }
