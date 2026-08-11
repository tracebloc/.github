# Bugbot guide — tracebloc/.github

## Context

**Public**, and it holds the reusable workflows every other repo calls. Roughly 120
callers across 16 repos consume these at `@main`, so a change here reaches the whole
fleet on its first successful run — there is no per-consumer rollout.

It also holds the conformance contract: `repo-inventory.yml` (what every repo must
have), `scripts/caller-drift.py` (the guard that checks it), and
`conformance-gate.yml` (the required check that refuses a contract change whose audit
did not pass).

Three properties shape the real defects here:

1. **A caller may only pass inputs the `@main` callee declares.** Add an input to a
   caller before the reusable that accepts it has reached `main` and the whole call
   dies with `startup_failure` — not a red job, a job that never starts.
2. **This repo is where "the check passed" is decided.** Most defects found here are
   not wrong logic; they are a guard that reports success it did not verify.
3. **It is public.** No customer names, internal URLs, or internal paths in code,
   comments, workflow prose, or fixtures.

## Always flag

- **Any path where a guard can report success without having checked.** This is the
  house speciality and it has many shapes, all seen in this repo: an empty API
  response read as "nothing found"; `|| echo 0` turning a failed call into a clean
  count; a `grep -q` in a pipeline where `pipefail` turns a real hit into rc=141; a
  required check whose job never runs on some PRs; a soft-fail default that makes a
  "required" check exit 0 on findings. If a read can fail, the failure must produce an
  UNREADABLE record, never a zero.

- **A check that cannot fail, or cannot report.** Two mirror defects: a job that always
  exits 0 (advisory by default while listed as required), and a job that is path- or
  branch-filtered while being a *required* status check, so PRs outside the filter wait
  forever at "Expected — waiting for status to be reported". Both look green.

- **Anything that reads only ONE of GitHub's two protection systems.** A branch
  protected solely by a ruleset returns 404 from `branches/{b}/protection` while the
  branch list reports `protected: true`. Reading only the classic endpoint reports a
  protected branch as unprotected. `bypass_actors` exists only on `/rulesets/{id}` — the
  per-branch rules endpoint omits it entirely, so an allowlist asserted from that
  endpoint asserts nothing.

- **A new input to a reusable that a caller starts passing in the same change.** See
  property 1 — land the callee first, flip the caller in a follow-up.

- **Changing a required check's job `name:`.** The name IS the contract; branch
  protection matches on it. A rename silently turns the old required context into one
  that can never report.

## Known non-issues — do not flag

- **`pii-gate / pii-check` failing red.** It was retired (backend#1409); a stale
  required context can linger on old PRs. Not a leak.
- **The long incident comments.** Several files carry a paragraph explaining a specific
  outage that shaped the code. They are load-bearing: each one is the reason a guard is
  written the awkward way it is. Do not suggest trimming them for brevity.
- **`strict: false` on branch protection.** Deliberate fleet-wide (backend#1276) — the
  release train handles stale bases better by re-evaluating and merging sha-pinned.
- **Duplicated prose between `org-standards.md` and repo CLAUDE.md files.** The block is
  synced, not copy-pasted; never edit the consuming copy.

## Tone

The valuable finding here is almost always "on this path, the guard says yes without
having looked". Style findings on shell are low value; a fail-open is high value even
when it is currently unreachable, because this repo's whole job is to be the thing that
does not fail open.

## Working with Bugbot findings (team norm)

Triage every finding the same day: fix it, or reply on the thread saying why not. No
silent dismissals — unresolved threads block the merge and stall the release train's
settle stage. A finding that recurs becomes a rule: add it here, and if it is
grep-expressible, to code-quality's house-rules.
