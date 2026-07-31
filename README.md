# .github

Org-wide defaults for `tracebloc`: shared issue templates, the PR template, CODEOWNERS,
and the reusable workflows every repo calls.

## New repo checklist

Do all of this before the first PR is opened, not after — several items only take effect
on later events, and two of them cause confusing failures if they are missing.

- [ ] **Disable "Rebase and merge".** Leave merge-commit and squash enabled.

      ```bash
      gh api -X PATCH repos/tracebloc/<repo> -F allow_rebase_merge=false
      ```

      GitHub enables rebase-merge by default on every new repo, and it breaks the FR
      gate: the gate proves each commit in a promotion range came from a merged PR via
      `repos/{owner}/{repo}/commits/{sha}/pulls`, and rebase-merging rewrites commits
      into shas that API cannot associate with any PR. Because the gate fails closed,
      the symptom is a promotion blocked on properly-reviewed code — which is what
      happened on design-system#88 and needed an audited `skip-fr-gate` override
      (backend#1337). Never disable all three: a repo with no merge method enabled
      cannot merge anything. `merge-settings-drift.yml` re-checks this weekly.

- [ ] **Add the thin caller workflows**, each referencing `@main`:
      `fr-gate-caller.yml`, `set-pr-status.yml`, `advance-deploy-env.yml`,
      `kanban-closure-routing.yml`, `fr-pass-comment-caller.yml`,
      `code-quality-caller.yml`, `wip-limit-check.yml`, `customer-priority-bump.yml`,
      `add-to-kanban.yml`, `stale-backlog.yml`. Copy them from a comparable existing
      repo rather than writing them fresh, and pin any third-party action to a full
      semver tag (`actions/add-to-project@v1.0.2` — `@v1` does not exist).

- [ ] **Create `develop`** from the default branch. All PRs target `develop`; only the
      release train promotes onward to `staging` and then to the prod branch.

- [ ] **Protect `develop`, `staging` and the prod branch**: one approving review,
      dismiss stale reviews on new commits, require conversation resolution, no force
      pushes, no deletions.

- [ ] **Decide whether the repo joins the release train.** If it ships an artifact, add
      an entry to `repos.yml` in [tracebloc/release-train](https://github.com/tracebloc/release-train)
      (including `version_file` if it should be tagged), rather than adding a
      repo-local tagging workflow. A repo-local workflow pushing tags as
      `github-actions[bot]` will be refused by the `v*` tag ruleset (backend#1345).

- [ ] **Confirm it is private unless it genuinely must be public.** Org-internal
      tracking, planning and security work belongs in a private repo — `backend` is the
      default home for anything cross-cutting.

## Cross-repo changes

The release train promotes **each repo independently** — the matrix runs with
`fail-fast: false` and there is no ordering between repos. A hop routinely merges
some and holds others (the 2026-07-29 prod hop merged 8 of 10). That is by design,
and it has a consequence worth internalising: **you cannot assume the rest of the
fleet moves with you.** A change in `averaging-service` that needs a `backend`
change ships broken for as long as backend is held — and backend can be held by
something entirely unrelated to your work, like an unrelated review finding.

Design for that rather than trying to orchestrate around it. **Expand, then
contract:**

1. **Expand** — ship the additive change first, in the *producing* repo, with the
   old behaviour still working. A new field, a new endpoint, a new optional
   argument. Nothing consumes it yet, so it is safe to promote alone.
2. **Adopt** — consumers switch to the new path in a later PR, once the producer
   is actually in production.
3. **Contract** — remove the old path a release *after* every consumer has
   adopted it.

Every step is independently promotable and safe in any order. That is the whole
point: it removes the dependency on promotion timing instead of managing it.

**When you genuinely cannot** — a change that is breaking by nature — say so
explicitly:

- tick **Breaking change** in the PR's *Type of change*;
- put the required rollout order in **Deployment notes**, naming the other repos'
  PRs by owner-qualified reference (`tracebloc/backend#123`);
- tell whoever fires the train, so the hop is sequenced by hand.

Do not rely on the two PRs happening to merge in the right order. Nothing enforces
that today.

## Scheduled checks in this repo

| Workflow | Cadence | What it does |
|---|---|---|
| `merge-settings-drift.yml` | Mondays 06:00 UTC | Reports any active repo where rebase-merge got re-enabled, or that has no merge method at all. Report-only — it never changes a setting. |
| `kanban-reconcile.yml` | see workflow | Reconciles board membership and archives closed promotion cards. |
| `stale-backlog.yml` | Mondays 00:00 UTC | Warns on backlog issues idle 6 weeks, closes at 8. Exempt with `keep-open` or `blocked`. |
