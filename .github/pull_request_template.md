## Summary
<!-- 1–3 sentences. What does this PR do and why? -->

## Related
<!-- REQUIRED when the title names a ticket: the `closing-ref` check reads this section's link graph, not the title. Same repo: Closes #123 · Cross-repo: Closes tracebloc/backend#2364 (owner-qualified — a bare `Closes #2364` resolves against THIS repo and links the wrong issue or none). Every train repo's default branch is now `develop` (measured 2026-08-23), so a closing keyword DOES fire on merge — the earlier note here saying it does not predates that migration. -->

## Type of change
- [ ] Feature
- [ ] Bug fix
- [ ] Tech-debt / refactor
- [ ] Docs
- [ ] Security / hardening
- [ ] Breaking change

## Test plan
<!-- What did you test? Commands run? Manual steps? -->

## Screenshots / recordings
<!-- For UI changes. Remove if N/A. -->

## Deployment notes
<!-- Env vars, migrations, rollout order, feature flags. Remove if N/A. -->

## Checklist
- [ ] **Sibling check** — what class is this bug in, and how did you cover it? *(paste the grep, or "N/A — one-off")*
- [ ] Tests added / updated and passing locally
- [ ] Docs updated if behavior or config changed
- [ ] No secrets / credentials in the diff
- [ ] For security-sensitive paths: appropriate reviewer requested
- [ ] The ticket named in the title is LINKED in *Related* above, cross-repo as `Closes tracebloc/<repo>#N` — a bare `#N` resolves against this repo, and a title reference alone links nothing (backend#2364)
- [ ] If this depends on a change in another repo: shipped **expand-then-contract** (additive first, consumers adopt later), or **Breaking change** ticked above with the rollout order in *Deployment notes* — repos promote independently, so the other change may not ship with this one
