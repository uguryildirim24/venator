# Public export rules

The public tree is an export of private `venator-prod`.
This review does not edit that repository or rewrite history.
Rolf must update the next sync. No export script was found in its tracked file list.

## Export manifest

Use the exact file list from Rolf's reviewed public revision as an allowlist.
A new private tracked file is not automatically public. Review its contents first.
Do not copy the private tracked tree and rely on `.gitignore` afterward.
Ignore rules do not remove tracked material.

Keep reviewed source, existing tests, build tooling, licenses, documentation,
logos, app icons and runtime data. Include only the fictional `profiles/example/`.
The reference tracks non-example Profiles and personal stores. Exclude them.
Also exclude private labels, model settings, contacts, application bundles,
résumés, recruiter messages, job lists, notes, handoffs and machine-local links.
Exclude generated databases, snapshots, dependencies and build output.
`ui/.env.desktop` contains a public loopback URL. Do not export private environment files.

## Changes needed on the private sync side

1. Carry the fictional dashboard data from `ui/fixtures/make-fixture.ts`, its
   sample label and the existing test and smoke expectations. All employers,
   job identifiers, descriptions, candidate evidence and decisions in that
   fixture are invented. Links use example.com. Do not regenerate it from
   private postings or Profile facts. Keep its original schema and history cases.
2. Carry the fictional candidate fixtures and their existing expectations under
   `tests/` and `ui/tests/`. Use the sample GPA `3.50/4.00`, fictional degree dates
   and placeholder authorization status. Keep the regenerated qualification
   goldens and measured compact-input hash together with their fixture inputs.
3. Keep the four job-search design captures out of the export:
   `ui/design/detail-light.png`, `ui/design/for-you-dark.png`,
   `ui/design/for-you-light.png` and `ui/design/list-light.png`.
   They contain job-search or résumé context, not verified public sample data.
4. Exclude the broken `.claude/skills/rundown` link. Remove the empty retired
   `tests/qualification/` and `train/qualification/` scaffolds and the dead
   private-corpus helper in `tests/match/test_pass_facts.py`. Keep the working
   lint installer, skill lock and active lint rules.
5. Carry the corrected run documentation. Keep Vite's committed defaults and
   open the browser preview at localhost:5173.
   Invoke pnpm inside `ui/` so Corepack selects its packageManager pin.
   Describe optional external processing and keep the private Install outside Git.
   Correct the reader documentation and comments too. The Posting page renders
   extracted text through `PageBlocks` and `PageBlockText`. Its iframes display
   prepared PDFs, not employer HTML. There is no Read Full Description frame.
6. Preserve ignore rules, default secret detectors and the exact, path-bound
   synthetic JWT exception. Retain GeoNames data and attribution, plus the
   supplied lint license and source notes.

The schedule Git stage, historical scoring reads, earlier submit-record reads,
status contracts and CI behavior are at HEAD. The extra Vite host binding was
reverted during this scope review.
Do not carry the earlier pass's feature removals or CI rewrite into the private repo.
The CLI's explicit `commit` stage is not part of default Refresh or the default loop.
Never select it for personal stores or public exports.

## Review before the next sync

Scan the manifest for secrets and personal information. Review image contents too.
Run existing checks with a private, disposable Install. Keep logs ignored.
Only aggregate observations belong in `docs/VALIDATION.md`.

The existing CI guard uses Git worktrees. For a plumbing-only change without
version movement, it also expects a posting corpus that the public tree does not
contain. Rolf must decide any later CI policy change separately from this tidy.
Do not export private postings to satisfy that check.

History review and scrubbing are separate. Rolf reviews the diff and handles any
commits or pushes. Agents do neither.
