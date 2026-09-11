# .github

The organization-level defaults for `tracebloc` that GitHub only reads from a
repository named `.github`:

- **`profile/README.md`** — the organization profile shown at
  [github.com/tracebloc](https://github.com/tracebloc).
- **`.github/ISSUE_TEMPLATE/`** — the issue templates offered in every
  repository of the organization that does not define its own.
- **`.github/pull_request_template.md`** — the default pull request template,
  on the same terms.

That is the whole repository. The reusable workflows, scripts, conformance
inventory and engineering standards that used to live here moved to a private
repository as part of the repository restructuring; the public repositories
that still called them have been re-pointed. Nothing here runs, gates, or
publishes anything.

Changes to the profile or the templates are ordinary pull requests against
`develop`.
