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
#   bricked-prs-selftest.yml  -> `selftest-bricked-prs`
#   kanban-columns.yml        -> `selftest-kanban-columns`
#   kanban-deploy-state-selftest.yml -> `selftest-kanban-deploy-state`
#   selftests.yml             -> `selftests`, `mint-scope`, `reason-citations`
#                                AND `selftest-house-rules`
#     (the `selftests` required context runs all three. `selftests` + the fixture
#      suites prove the rules CATCH; `mint-scope` proves the real workflows COMPLY.
#      They disagree in either direction, so neither substitutes for the other.
#      house-rules' MUTATION tier is separate again: the fast `--dry` anchor resolve
#      rides `lint`, the full 58s run is in `check-all` and in selftests.yml.)
#
# When one of those workflows changes, change the matching line here. Adding a
# NEW selftest needs no edit to this list to be CAUGHT — `selftests-cover`
# fails until it is wired up (backend#1966). The list is here to say which
# workflow each target mirrors, not to be the record of what exists.
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
	@echo "              + all $(words $(SELFTEST_FILES)) selftests — run this before every push (~18 s)"
	@echo "  check-all   the same, plus gitleaks (needs the gitleaks binary)"
	@echo "  setup       preflight the tools check needs; installs the pre-push hook"
	@echo "  install-hooks  (re)install the git pre-push hook that runs 'make check'"
	@echo
	@echo "  lint            ruff + shellcheck + house-rules + action-pins +"
	@echo "                  mint-scope + actionlint"
	@echo "  selftests       all $(words $(SELFTEST_FILES)) gate selftests (+ the coverage assertion)"
	@echo "  credential-scan gitleaks over the whole history, as code-quality.yml runs it"
	@echo "  audit           caller-drift.py against the live org — needs a token"
	@echo "  reason-citations  the live inventory's ticket citations — needs a token"
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
check-all: check credential-scan mutations
	@echo "==> check-all: green"

# ---- lint --------------------------------------------------------

.PHONY: lint
lint: ruff shellcheck house-rules action-pins mint-scope actionlint mutations-dry

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
# mint-scope: no App-token mint may carry the App's FULL installation grant
# (backend#2157). In `lint` rather than `selftests` because it is a property of the
# workflows in this repo, not of a script -- same tier as action-pins, which asks a
# structurally identical question about the same files.
.PHONY: selftest-mint-scope
selftest-mint-scope: guard-pyyaml
	$(PYTHON) scripts/tests/mint-scope-selftest.py

.PHONY: mint-scope
mint-scope: guard-pyyaml
	$(PYTHON) scripts/mint-scope.py

# reason-citations: a ticket cited by a repo-inventory exemption reason must still
# be live (backend#2449). THREE targets, in three different tiers, because they
# answer three different questions:
#
#   selftest-reason-citations   does the rule CATCH?      fixtures, offline, in `check`
#   mutation-reason-citations   would the suite NOTICE?   `mutations`, in `check-all`
#   reason-citations            does the INVENTORY comply? the live file, needs a token
#
# The third is NOT in `check` or `lint`, unlike `mint-scope` -- and that is the
# only reason the two are wired differently. mint-scope reads workflow files off
# disk; this one reads issue state from the API, so it needs `gh` authenticated
# and the network. `make check` is the offline pre-push tier with an ~18 s budget,
# and a target that can fail on somebody's train wifi does not belong in it. It
# runs in CI from selftests.yml, where the App token is minted, and on demand
# here -- the same split `audit` gets, for the same reason.
.PHONY: reason-citations
reason-citations: guard-pyyaml
	$(PYTHON) scripts/reason-citations.py


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
# The gate selftests, each the `selftest` job of its workflow. All are
# token-free and network-free, which is exactly why they belong in `check`:
# in CI every one of them is behind a `paths:` filter, so a PR that refactors
# a gate without touching its named trigger paths never runs them. Locally
# there is no filter and no reason for one — they cost ~15 s together.
#
# HOW MANY THERE ARE IS DERIVED, NEVER RESTATED (backend#1966). This file used
# to say "all four" in four places while CI ran six, and by the time that was
# filed develop had seven — the count went stale twice over. `SELFTEST_FILES`
# comes from the directory, `selftests-cover` fails if any of them is not wired
# to a target, and every message prints `$(words ...)`. Nothing hand-maintains N.
#
# The per-selftest targets stay explicit on purpose rather than being generated
# from the glob: the invocations are NOT uniform. selftest-blocked-marker runs a
# SECOND command, version-bump-gate is bash, and only some need PyYAML. A
# glob-driven recipe would have silently dropped that second command — so what
# is derived is the COVERAGE ASSERTION, not the invocation.
SELFTEST_FILES := $(sort $(wildcard scripts/tests/*-selftest.py scripts/tests/*-selftest.sh))

# A SECOND FAMILY, added rather than folded in (backend#1788). A mutation runner is
# not a selftest: it re-runs one, so putting it in `selftests` would nest the fast
# tier inside itself and cost ~58s instead of ~3s. But it must still be RUN BY
# SOMETHING, or it rots exactly like an unwired selftest -- so it gets its own
# wildcard and its own coverage assertion below, keyed on the targets that run it.
#
# The naming check above now admits `*-mutations.py` for the same reason it admits
# `*-selftest.*`: a file under scripts/tests/ that matches no wildcard is invisible,
# and an invisible file makes the coverage assertion pass vacuously.
MUTATION_FILES := $(sort $(wildcard scripts/tests/*-mutations.py))
MUTATION_TARGETS := mutation-house-rules mutation-pipefail-early-close \
                   mutation-bugbot-gate mutation-closing-ref-gate mutation-bug-to-ready \
                   mutation-branch-owner mutation-reason-citations \
                   mutation-mutation-baseline mutation-standards-sync \
                   mutation-conflict-gate \
                   mutation-triage-labels \
                   mutation-archive-baseline

# THE WHOLE MUTATION TIER, BY NAME OF THE LIST. Every entry point -- CI,
# `check-all`, `lint` -- depends on one of these two rather than on any
# individual runner.
#
# Twice in one PR a target named ONE MEMBER of the list and so silently skipped
# the second runner: `selftests.yml` ran `make mutation-house-rules`, and then
# `check-all`/`lint` were left doing the same after CI was fixed (Bugbot,
# .github#300, twice). That is the paired-construct shape -- change one half of
# something that must move together and the other half is now a bug. Naming the
# list is what makes adding the next runner a ONE-WORD edit to MUTATION_TARGETS
# that cannot be half-wired.
#
# The dry list is DERIVED from the real one, not written beside it: a
# hand-maintained second list is the same drift one level down.
.PHONY: mutations mutations-dry
mutations: $(MUTATION_TARGETS)
mutations-dry: $(addsuffix -dry,$(MUTATION_TARGETS))

.PHONY: selftests
# The targets that actually RUN a selftest. Named once: `selftests` depends on
# them, and `selftests-cover` asks make what these would execute. Adding a
# selftest target means adding it here, which is the single edit that both wires
# it into `make check` and brings it under the coverage guard.
SELFTEST_TARGETS := selftest-caller-drift selftest-blocked-marker selftest-standards-sync \
	selftest-stale-backlog \
                    selftest-version-bump-gate selftest-version-file \
                    selftest-bricked-prs selftest-kanban-columns \
                    selftest-kanban-deploy-state selftest-git-reap \
                    selftest-mint-scope selftest-house-rules \
                    selftest-reason-citations \
                    selftest-pipefail-early-close \
                    selftest-bugbot-gate \
                    selftest-closing-ref-gate \
                    selftest-bug-to-ready \
                    selftest-branch-owner \
                    selftest-mutation-baseline \
                    selftest-conflict-gate \
                    selftest-triage-labels \
                    selftest-archive-baseline

selftests: selftests-cover $(SELFTEST_TARGETS)

# Guard the guard-runner. Two assertions, both fail-closed:
#
#   1. Every file under scripts/tests/ matches `*-selftest.{py,sh}`. Without
#      this, a selftest added under any other name is invisible to the wildcard
#      and assertion 2 passes vacuously — the exact inert-verification shape
#      backend#1729 catalogued.
#   2. Every matched file is RUN BY A RECIPE. SELFTEST_FILES is built by
#      wildcard, so the variable line does not itself contain the paths.
#
# ASSERTION 2 SEARCHES RECIPE LINES ONLY (lines starting with a tab), and that
# is the whole correctness of it. An earlier revision grepped the entire file —
# and passed on an unwired selftest because a COMMENT in this very block named
# it. A guard that a comment can satisfy is inert verification of itself, which
# is a fine joke to make once and never ship. Recipe-only means a match is a
# command that actually runs.
#
# Mutation-proved, all three reddening and each with its anchor asserted:
# a new `*-selftest.py` with no target; a file in scripts/tests/ off-convention;
# and unwiring an existing selftest's recipe line.
.PHONY: selftests-cover
selftests-cover:
	@fail=0; \
	for path in scripts/tests/*; do \
	  [ -e "$$path" ] || continue; \
	  case "$$path" in \
	    *-selftest.py|*-selftest.sh) ;; \
	    *-mutations.py) ;; \
	    scripts/tests/mutation_baseline.py) ;; \
	    scripts/tests/__pycache__) ;; \
	    *) echo "$$path does not match '*-selftest.{py,sh}' or '*-mutations.py', so no wildcard can see it."; \
	       echo "  Rename it, or teach the wildcards about the new convention — do NOT"; \
	       echo "  leave it unmatched: assertion 2 below would then pass without covering it."; \
	       fail=1 ;; \
	  esac; \
	done; \
	cmds="$$(make --dry-run --no-print-directory $(SELFTEST_TARGETS) 2>/dev/null)"; \
	[ -n "$$cmds" ] || { echo "could not ask make what $(SELFTEST_TARGETS) would run — refusing to report coverage"; exit 1; }; \
	mcmds="$$(make --dry-run --no-print-directory $(MUTATION_TARGETS) 2>/dev/null)"; \
	[ -n "$$mcmds" ] || { echo "could not ask make what $(MUTATION_TARGETS) would run — refusing to report coverage"; exit 1; }; \
	for f in $(MUTATION_FILES); do \
	  printf '%s\n' "$$mcmds" | grep -qF -- "$$f" || { \
	    echo "$$f is not run by any mutation target in this Makefile."; \
	    echo "  A mutation runner nobody runs is the same dead weight as an unwired"; \
	    echo "  selftest, with the extra cost that it LOOKS like the tier exists."; \
	    fail=1; }; \
	done; \
	for f in $(SELFTEST_FILES); do \
	  printf '%s\n' "$$cmds" | grep -qF -- "$$f" || { \
	    echo "$$f is not run by any target in this Makefile."; \
	    echo "  'make check' would report green without it, which breaks this file's"; \
	    echo "  own 'thin wrapper of what CI runs' contract. Add a selftest-* target"; \
	    echo "  and list it under 'selftests'."; \
	    fail=1; }; \
	done; \
	wf=.github/workflows/selftests.yml; \
	[ -r "$$wf" ] || { echo "$$wf is unreadable — refusing to report that CI runs these"; exit 1; }; \
	for m in $(MUTATION_TARGETS); do \
	  grep -qE "^(check-all|lint):.*[[:space:]]$$m(-dry)?([[:space:]]|$$)" Makefile && { \
	    echo "check-all/lint names the individual runner '$$m' instead of 'mutations'/'mutations-dry'."; \
	    echo "  Naming one member of MUTATION_TARGETS is how the second runner gets skipped"; \
	    echo "  silently — it happened twice in .github#300. Depend on the list."; \
	    fail=1; }; \
	done; \
	for t in selftests mutations; do \
	  grep -qE "^[[:space:]]*run:[[:space:]]*make[[:space:]]+$$t([[:space:]]|$$)" "$$wf" || { \
	    echo "$$wf does not run 'make $$t'."; \
	    echo "  Being wired to a Makefile target is only half of it: the tier also has to"; \
	    echo "  EXECUTE in CI. mutation-pipefail-early-close was covered by this guard and"; \
	    echo "  never run, because the workflow named one member of MUTATION_TARGETS"; \
	    echo "  instead of the list (Bugbot, .github#300). Covered on paper, unrun in fact."; \
	    fail=1; }; \
	done; \
	[ "$$fail" = 0 ] || exit 1; \
	echo "selftests-cover: all $(words $(SELFTEST_FILES)) selftests and $(words $(MUTATION_FILES)) mutation runner(s) are wired to a target, and CI runs both tiers"

.PHONY: selftest-caller-drift
selftest-caller-drift: guard-pyyaml
	$(PYTHON) scripts/tests/caller-drift-selftest.py

# Two steps, mirroring blocked-gate-selftest.yml exactly: the marker table, then
# the check that the gate does NOT fire on its own filenames. That self-reference
# step is the only coverage for a "blocked"-in-a-path false positive, so make
# check must run it too or it reports green on a matcher change CI would fail.
.PHONY: selftest-blocked-marker
selftest-blocked-marker:
	$(PYTHON) scripts/tests/blocked-marker-selftest.py
	$(PYTHON) scripts/blocked-marker.py --title "ci(gate): add blocked-gate.yml and blocked-marker.py"

.PHONY: selftest-standards-sync
selftest-standards-sync: guard-pyyaml
	$(PYTHON) scripts/tests/standards-sync-selftest.py

# The slowest of them all (~15 s): it builds a throwaway git repo per case.
.PHONY: selftest-version-bump-gate
selftest-version-bump-gate: guard-pyyaml
	bash scripts/tests/version-bump-gate-selftest.sh

# No guard-pyyaml: version_file.py is stdlib-only on purpose, so the writer the
# release path depends on cannot be broken by a dependency resolution (backend#2758).
selftest-version-file:
	python3 scripts/tests/version-file-selftest.py

# guard-pyyaml, mirroring bricked-prs-selftest.yml's `pip install pyyaml` step:
# bricked-prs.py imports caller-drift.py for the protection reader, and that
# module hard-fails without PyYAML by design. Measured with the module blocked:
# exit 2 without it.
.PHONY: selftest-bricked-prs
selftest-bricked-prs: guard-pyyaml
	$(PYTHON) scripts/tests/bricked-prs-selftest.py

# NO guard-pyyaml: the sweep parses no YAML. Asserted rather than assumed -- the
# selftest imports the script and would fail on a missing module, and the reusable
# workflow runs this exact target with no pip step (backend#1979).
selftest-stale-backlog:
	$(PYTHON) scripts/tests/stale-backlog-selftest.py

# NO guard-pyyaml, and that is asserted rather than assumed: kanban-columns.yml
# runs this with no pip step, and it was measured to pass with the yaml module
# blocked. Do not add a dependency this selftest does not have.
.PHONY: selftest-kanban-columns
selftest-kanban-columns:
	$(PYTHON) scripts/tests/kanban-columns-selftest.py
	$(PYTHON) scripts/tests/branch-status-map-selftest.py

.PHONY: selftest-kanban-deploy-state
selftest-kanban-deploy-state: guard-pyyaml
	$(PYTHON) scripts/tests/kanban-deploy-state-selftest.py

# Builds a throwaway repo per case and stubs `gh` on PATH, so it needs neither a
# token nor a network — but it DOES need a committer identity, which a bare CI
# runner lacks. git-reap-selftest.yml configures one; a developer machine
# already has it, so nothing is set here.
.PHONY: selftest-house-rules
selftest-house-rules:
	bash scripts/tests/house-rules-selftest.sh

# The mutation tier for house-rules (backend#1788). NOT in `selftests`: each mutation
# re-runs the whole suite, so the full pass is ~58s against the suite's own ~2.8s.
# `--dry` resolves every anchor in milliseconds and is what belongs in the fast tier --
# it catches the way this job actually breaks (a refactor moving a line an anchor
# matched on) without proving the cases still catch. Measured 2026-08-20.
.PHONY: mutation-house-rules
mutation-house-rules:
	$(PYTHON) scripts/tests/house-rules-mutations.py

.PHONY: mutation-house-rules-dry
mutation-house-rules-dry:
	$(PYTHON) scripts/tests/house-rules-mutations.py --dry

# The early-close gate (backend#2264). Two files, because the rule genuinely
# lives in two: the awk decides which LINES are offenders, the wrapper decides
# which FILES run under errexit+pipefail (the inheritance fixpoint, the option
# sign, the derived file list).
.PHONY: selftest-pipefail-early-close
selftest-pipefail-early-close:
	bash scripts/tests/pipefail-early-close-selftest.sh

.PHONY: mutation-pipefail-early-close mutation-pipefail-early-close-dry
mutation-pipefail-early-close:
	$(PYTHON) scripts/tests/pipefail-early-close-mutations.py

mutation-pipefail-early-close-dry:
	$(PYTHON) scripts/tests/pipefail-early-close-mutations.py --dry

# The Bugbot review gate (backend#2284). NO guard-pyyaml, and that is asserted
# rather than assumed: the gate imports only the standard library, and
# `bugbot-gate.yml` runs it with no pip step. Do not add a dependency it does
# not have.
.PHONY: selftest-bugbot-gate
selftest-bugbot-gate:
	$(PYTHON) scripts/tests/bugbot-gate-selftest.py

# The mutation tier for the gate. NOT in `selftests`: each of the 26 mutations
# re-runs the whole suite. Measured on a laptop: the suite alone is ~0.35s, the
# full mutation pass ~9s -- cheap enough for the required `selftests` context via
# `make mutations`, which is where selftests.yml runs it.
#
# `--dry` resolves every anchor in milliseconds and is what belongs in the fast
# tier: it catches the way this job actually breaks (a refactor moving a line an
# anchor matched on) without proving the cases still catch.
.PHONY: mutation-bugbot-gate mutation-bugbot-gate-dry
mutation-bugbot-gate:
	$(PYTHON) scripts/tests/bugbot-gate-mutations.py

mutation-bugbot-gate-dry:
	$(PYTHON) scripts/tests/bugbot-gate-mutations.py --dry

# The merge-conflict gate (backend#2637): a conflicted PR runs NO `pull_request`
# workflow, so every drift guard is silently inactive and the rollup reads green.
# guard-pyyaml, mirroring conflict-gate.yml's own `pip install pyyaml` step:
# conflict-gate.py imports caller-drift.py for the `gh` wrappers and the inventory
# loader, and that module hard-fails without PyYAML by design.
.PHONY: selftest-conflict-gate
selftest-conflict-gate: guard-pyyaml
	$(PYTHON) scripts/tests/conflict-gate-selftest.py

# The archive's cross-run floor (backend#2802). guard-pyyaml: the suite parses
# kanban-archive.yml -- it extracts the comparison's shell out of the assert step
# and runs it verbatim, and reads the upload step's wiring from the same document.
# No token and no network: every case is files in a temp dir.
.PHONY: selftest-archive-baseline
selftest-archive-baseline: guard-pyyaml
	$(PYTHON) scripts/tests/archive-baseline-selftest.py

.PHONY: mutation-archive-baseline mutation-archive-baseline-dry
mutation-archive-baseline:
	$(PYTHON) scripts/tests/archive-baseline-mutations.py

mutation-archive-baseline-dry:
	$(PYTHON) scripts/tests/archive-baseline-mutations.py --dry

.PHONY: mutation-conflict-gate mutation-conflict-gate-dry
mutation-conflict-gate:
	$(PYTHON) scripts/tests/conflict-gate-mutations.py

mutation-conflict-gate-dry:
	$(PYTHON) scripts/tests/conflict-gate-mutations.py --dry

# The closing-ref gate (backend#2364): a PR whose TITLE names a ticket must LINK
# it. NO guard-pyyaml, for the same asserted reason as bugbot-gate above -- the
# checker imports only the standard library, and its job in `set-pr-status.yml`
# runs it with no pip step. Do not add a dependency it does not have.
.PHONY: selftest-closing-ref-gate
selftest-closing-ref-gate:
	$(PYTHON) scripts/tests/closing-ref-gate-selftest.py

# Measured on a laptop: the suite alone ~0.5s, the full mutation pass ~13s for 34
# mutations (each re-runs the whole suite). Same split as every other runner --
# the full pass rides the required `selftests` context via `make mutations`, and
# `--dry` (anchor resolution only, milliseconds) rides `make check`.
.PHONY: mutation-closing-ref-gate mutation-closing-ref-gate-dry
mutation-closing-ref-gate:
	$(PYTHON) scripts/tests/closing-ref-gate-mutations.py

mutation-closing-ref-gate-dry:
	$(PYTHON) scripts/tests/closing-ref-gate-mutations.py --dry

# The reason-citation check (backend#2449). guard-pyyaml: it parses
# repo-inventory.yml. The suite stubs `gh` on PATH, so neither of these two needs
# a token or the network -- only the `reason-citations` target above does.
.PHONY: selftest-reason-citations
selftest-reason-citations: guard-pyyaml
	$(PYTHON) scripts/tests/reason-citations-selftest.py

# Measured on a laptop: the suite alone ~4 s (every case runs the checker as a
# subprocess against a fixture inventory), the full mutation pass ~95 s for 23
# mutations. Same split as every other runner -- the full pass rides the required
# `selftests` context via `make mutations`, and `--dry` (anchor resolution only,
# milliseconds) rides `make check`.
.PHONY: mutation-reason-citations mutation-reason-citations-dry
mutation-reason-citations:
	$(PYTHON) scripts/tests/reason-citations-mutations.py

mutation-reason-citations-dry:
	$(PYTHON) scripts/tests/reason-citations-mutations.py --dry
# The bug-label promotion (backend#2348). guard-pyyaml: the suite parses THREE
# workflows -- it extracts the decision out of `customer-priority-bump.yml` by its
# `# selftest:` markers, asserts `col_index` byte-identical to the router's, and
# derives the board's Status vocabulary from advance-deploy-env's `rank()`.
# selftests.yml installs PyYAML, which is where CI runs this.
.PHONY: selftest-bug-to-ready
selftest-bug-to-ready: guard-pyyaml
	$(PYTHON) scripts/tests/bug-to-ready-selftest.py

# NOT in `selftests`: each mutation re-runs the suite. Measured on a laptop: the
# suite alone ~1.5s, the full 21-mutation pass ~30s. `--dry` resolves every anchor
# in milliseconds and rides `lint`, which catches the way this really breaks -- a
# refactor moving a line an anchor matched on -- without proving the cases catch.
.PHONY: mutation-bug-to-ready mutation-bug-to-ready-dry
mutation-bug-to-ready: guard-pyyaml
	$(PYTHON) scripts/tests/bug-to-ready-mutations.py

mutation-bug-to-ready-dry: guard-pyyaml
	$(PYTHON) scripts/tests/bug-to-ready-mutations.py --dry

# Triage-label existence across the fleet (backend#2598). guard-pyyaml: both the
# check and its suite parse the two PRODUCERS the label domain is derived from --
# `customer-priority-bump.yml`'s `workflow_call` inputs and every
# `.github/ISSUE_TEMPLATE/*.yml` -- as YAML rather than by grep. The suite needs
# no token: it stubs `gh` on PATH and drives `audit()` with an injected reader.
.PHONY: selftest-triage-labels
selftest-triage-labels: guard-pyyaml
	$(PYTHON) scripts/tests/triage-labels-selftest.py

# NOT in `selftests`: each mutation re-runs the suite. `--dry` resolves every
# anchor in milliseconds and rides `lint`, which catches the way this really
# breaks -- a refactor moving a line an anchor matched on.
.PHONY: mutation-triage-labels mutation-triage-labels-dry
mutation-triage-labels: guard-pyyaml
	$(PYTHON) scripts/tests/triage-labels-mutations.py

mutation-triage-labels-dry: guard-pyyaml
	$(PYTHON) scripts/tests/triage-labels-mutations.py --dry

.PHONY: selftest-git-reap
selftest-git-reap:
	bash scripts/tests/git-reap-selftest.sh

# Branch OWNERSHIP, which git-reap above deliberately does not need: it reaps the
# caller's OWN local branches, so "whose is it" never arises. Anything that
# reasons about ownership across people -- a per-person report, a remote sweep --
# calls `branch_owner.py`, and this is the tier that keeps that rule honest
# (backend#2365). NO guard-pyyaml: the module imports only the standard library
# and the suite stubs both of its seams, so there is nothing to install.
.PHONY: selftest-branch-owner
selftest-branch-owner:
	$(PYTHON) scripts/tests/branch-owner-selftest.py

.PHONY: mutation-branch-owner mutation-branch-owner-dry
mutation-branch-owner:
	$(PYTHON) scripts/tests/branch-owner-mutations.py

mutation-branch-owner-dry:
	$(PYTHON) scripts/tests/branch-owner-mutations.py --dry

# The BASELINE every runner above measures against (backend#2441). Each of them
# overwrites a tracked file and restores it in a `finally`, which SIGKILL, a
# runner timeout and a concurrent run all skip -- and the mutation left on disk
# becomes the next run's `pristine`, which then reports `0 uncaught` about a
# premise nobody typed. `mutation_baseline.guard()` refuses instead, and this is
# the tier that keeps the refusal honest. NO guard-pyyaml: the module imports
# only the standard library and the suite builds real throwaway git repos, so
# there is nothing to install.
.PHONY: selftest-mutation-baseline
selftest-mutation-baseline:
	$(PYTHON) scripts/tests/mutation-baseline-selftest.py

.PHONY: mutation-mutation-baseline mutation-mutation-baseline-dry
mutation-mutation-baseline:
	$(PYTHON) scripts/tests/mutation-baseline-mutations.py

mutation-mutation-baseline-dry:
	$(PYTHON) scripts/tests/mutation-baseline-mutations.py --dry

mutation-standards-sync:
	$(PYTHON) scripts/tests/standards-sync-mutations.py

mutation-standards-sync-dry:
	$(PYTHON) scripts/tests/standards-sync-mutations.py --dry

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
	@got=$$($(ACTIONLINT) -version 2>/dev/null | head -1 | sed 's/^v//'); \
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

# PyYAML, which most of the selftests import (directly, or via a module that
# hard-fails without it). Every workflow that runs one of those pip-installs
# 'pyyaml==6.0.2' on a clean setup-python first, so an ImportError here is a
# missing dependency and not a broken test.
#
# No count here on purpose: it was "three of the four" while there were six.
# Which targets depend on this is visible at the targets themselves, and each
# one was measured with the yaml module blocked rather than guessed.
.PHONY: guard-pyyaml
guard-pyyaml:
	@$(PYTHON) -c 'import yaml' 2>/dev/null || { \
	  echo "PyYAML is missing from $(PYTHON) — the selftests import it:"; \
	  echo "  $(PYTHON) -m pip install 'pyyaml==6.0.2'"; \
	  exit 1; }
