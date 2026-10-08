# Venator vocabulary

Venator finds postings, filters them against a Profile, prepares application
materials and tracks manual applications. It never submits an application.

## People and facts

**Person**
Someone using their own Install. Rolf directs development of this project.
A Profile describes confirmed facts, not assumptions about a person.

**Profile**
Confirmed résumé facts, work authorization, target roles, locations and sources.
Drafting may use only these facts and confirmed answer-library statements.
An Install can hold several Profiles, one per search. Stages take `--profile`.

**Install**
One person's local Profiles and stores. Its boundary is the application data
folder (`VENATOR_HOME` or the platform default), not a Git checkout.
See [ADR-0002](docs/adr/0002-checkout-is-the-tenant-boundary.md).
Persistent storage is local. Optional assistant, scoring and training requests
send selected inputs externally, as described in the README.

## Discovery and filtering

**Source**
An employer board or search service from which Discover fetches postings.

**Posting**
One job ad as a source published it. Filtering records a decision rather than
silently dropping it from the store.

**Hard Filter**
A configured exclusion rule: `work_authorization`, `education_fit`, `role_target`,
`eligibility` or `location`. Unclear wording passes for review. Location excludes
only when all readable sites are outside the selected US states or DC.
US-wide remote sites pass. State-specific remote sites count in that state only.
Physical local sites can vouch for unreadable sites from the same employer.

**Filter Decision**
The recorded verdict on a Posting, with the rule and reason. A filter change adds
new decisions. Old rows remain in the private append-only store.

**Match**
A Posting that passed Hard Filters. Passing is not proof of suitability.

**Dedup**
Recognising the same job across sources or observations.

**Score**
A keep probability from a privately configured model. It binds to a compact
Posting input and model identity. No model ships with Venator.
For you starts at 0.5. Explore is 0.1 to below 0.5. Lower scores are Excluded.
For you and Explore sort by descending probability. View may carry a same-model
score while current inputs wait, labelled Last month's score. A carried score
does not make an input current for Score selection.

**Refresh and Score**
Refresh fetches, filters and rebuilds View without inference. Unscored passes
wait in Awaiting Score. Stale passes wait in Awaiting Hard Filters.
Score runs separately or in the daily loop. The sidebar's location picker only
narrows the display. The Profile's location filter changes decisions.
Both use the offline [GeoNames lookup](docs/geonames-attribution.md).

## Applying

**Application**
Versioned materials for one Posting: a résumé, an optional letter and answers.
It is not the act of submitting those materials.

**Tailoring**
Reordering and emphasising confirmed facts. Drafted passages are checked against
the facts they cite. Review the result before using it.

**Review Queue**
Postings waiting for a Save or Dismiss action. Approval sends nothing to an employer.

**Submission**
A person sending materials through an employer's form. Venator can record a
manual report or detect confirmation during a reviewed handoff. It never presses Submit.

**Apply**
A press that checks a Posting, prepares or reuses documents and opens a visible
form. Daily pre-drafting prepares documents without opening a form or recording
an application action.

**Contact**
A privately imported person linked to an employer or board. Reached out records
outreach separately from application state and keep/skip labels.

## Filling forms

**FormSpec**
Observed form fields: labels, types, required flags, selectors and exact options.
It describes questions, not answers.

**FillPlan**
Proposed answers tied to confirmed Profile facts. Missing facts remain `unmapped`.
Dry Run plans require the literal `never_submit: true`.

**Dry Run**
The `venator.browser.fill` tool aborts state-changing requests, cancels submit
events and skips file uploads. The reviewed visible handoff is a separate tool.
It can fill trusted forms and upload reviewed PDFs, but submission stays manual.
Lever remains manual. See [docs/RUNNING.md](docs/RUNNING.md) for supported handoffs.

## Tracking

**TrackEvent**
An appended action: `approve`, `reject`, `prepare`, `fill`, `submit`, `restore`,
`outcome`, `withdraw`, `outreach` or `outreach_undo`. Each has a checked actor
and time. Store claims bind records to a Profile. `approve` means Save and
`reject` means Dismiss in the app.
`submit` records a manual application, never an automated pipeline submission.
Outreach events change neither application state nor keep/skip labels.

**Application State**
Explicit Track events fold to `queued`, `approved`, `prepared`, `rejected`,
`filled`, `submitted`, `concluded` or `withdrawn`. The latest valid state-changing
event wins. `restore` returns a Posting to `queued`.
Historical queued `llm_score` decisions can also create a queued state when no
Track action overrides them. Current keep probabilities do not create Track state.
