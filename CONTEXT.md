# Venator vocabulary

Venator finds job postings, filters them against one person's Profile, helps prepare
applications, and tracks them. The person always submits. Venator never does.

These are the words the code, the commits and the app use. Each entry says what to
avoid saying instead.

## People and facts

**Owner**
The person a Profile applies for. One Owner per Profile. Refer to the Owner as
"they"; never guess pronouns.
_Avoid_: user, candidate, applicant.

**Profile**
The Owner's facts: résumé, work authorization, locations, the roles they want, and
what to search for. Applications may only use facts from here. An Install can hold
more than one Profile, one per search, and every stage takes `--profile`.
_Avoid_: config, settings

**Install**
One person's Venator, with its Profiles and data. One Install per person, never
shared. Its boundary is the application data directory (`$VENATOR_HOME`, or the
platform's usual place), not a Git checkout, because most people using it will never
have a checkout. Profiles and the append-only stores live there, outside Git
(`docs/adr/0002-checkout-is-the-tenant-boundary.md`).
_Avoid_: tenant, account, workspace, instance

## Discovery and filtering

**Source**
Where Postings come from: an ATS board such as Greenhouse, Lever, Ashby,
SmartRecruiters or Workday.
_Avoid_: provider, feed, scraper

**Posting**
One job ad as a Source published it. Every Posting is kept, whether or not it
passes the filters.
_Avoid_: job, listing, opportunity

**Hard Filter**
A fixed rule that kills a Posting outright. The rules are `work_authorization`,
`education_fit`, `role_target`, `eligibility` and `location`, with wording from
the Profile. Location kills only when all readable sites are outside the
Profile's selected US states (including DC). A remote site tied to a state counts only in
that state. Only physical local sites vouch for unreadable sites from their
employer. When the wording is unclear, the Posting passes. Killing a good Posting by mistake
is the failure to avoid.
_Avoid_: rule, blacklist

**Filter Decision**
The recorded verdict on one Posting: which filter passed or killed it, and why.
Every Posting gets one. When the filters change, the decisions are replayed and new
rows are added. Nothing is dropped silently.
_Avoid_: rejection, drop

**Match**
A Posting that passed the Hard Filters and is worth applying to.

**Dedup**
Recognising that two Postings are the same job, across Sources or over time, so the
Owner never applies twice.

**Score**
A keep probability from a person's own model configured in their Install. A score
binds to the compact Posting input and model identity. No model ships with Venator.
For you starts at 0.5; below 0.1 is Excluded; Explore is between those cuts.
For you and Explore show the highest keep probability first. View may carry the
latest same-model score while current inputs wait for scoring, marked Last month's
score. Carry does not make an input current for Score selection.

**Refresh and Score**
Refresh fetches Postings, records Filter Decisions and rebuilds View without
inference. Hard Filter passes without a current or carried same-model score wait
in Awaiting Score. Stale passes wait in Awaiting Hard Filters instead. Score runs
on a press or in the daily loop; a pause keeps earlier scores for the next run.
The sidebar's location choice narrows what is displayed; unlike the Profile's
location Hard Filter, it never changes a Filter Decision. Both use the same
offline GeoNames place lookup ([attribution](docs/geonames-attribution.md)).

## Applying

**Application**
The bundle prepared for one Match: a résumé, an optional cover letter, and screening
answers.
_Avoid_: submission (that's the act of sending it), draft

**Tailoring**
Reordering and emphasising real Profile facts for one Match. Tailoring never invents
a fact. A second check compares every drafted passage with the facts it cites.
_Avoid_: generation, rewriting

**Review Queue**
Postings waiting for the Owner to save or dismiss them. Nothing is sent to an
employer by Venator, approved or not.
_Avoid_: inbox, pending list

**Submission**
The Owner sending an Application through the employer's own form. Venator records it
only when the Owner reports it.
_Avoid_: application (that's the bundle), send

**Apply**
One press checks the Posting, prepares or reuses current documents and opens the
visible filled form. The Owner still submits. Daily pre-drafting prepares documents
without opening a browser or recording an Application.

**Contact**
A privately imported person linked to an employer or board, shown in the Posting
margin. Reached out records outreach separately from Application state and labels.

## Filling forms

**FormSpec**
What one employer's application form looks like, as seen live: each field's label,
kind, whether it's required, its selector and its exact options. Recon captures it.
It describes the form, never the answers.
_Avoid_: schema, form model

**FillPlan**
The proposed answers for one FormSpec. Each answer is a value plus the Profile fact it
came from. Fields no Profile fact can answer are listed as `unmapped`, with the reason,
never guessed. A plan carries `never_submit: true`, and the driver refuses one that
doesn't.
_Avoid_: form data, answers, payload

**Dry Run**
Filling a form with no way to send it. State-changing requests are aborted, submit
events are cancelled and counted, file fields are skipped, and no code clicks Apply.
This is `venator.browser.fill`. The visible handoff is a separate tool: on trusted Greenhouse and Ashby
forms bound to the Posting, it fills confirmed fields and uploads reviewed
files for the Owner to check and submit. Lever remains manual. A Dry Run is
evidence, not a Submission.
_Avoid_: test run, simulation, preview

## Tracking

**TrackEvent**
One recorded fact about an Application: `approve`, `reject`, `prepare`, `fill`,
`submit`, `restore`, `outcome`, `withdraw`, `outreach` or `outreach_undo`. Events are appended, never edited, and
each has a checked actor, details, a time and the Profile it belongs to. The Owner
can act on a Posting before it has a score. `submit` records the
Owner's own report that they applied; it sends nothing to an employer. In the app,
`approve` is Save and `reject` is Dismiss. Outreach and its undo never change
Application state or keep/skip labels.
_Avoid_: status update, log entry

**Application State**
Where one Posting stands, worked out from its TrackEvents in order: `queued`,
`approved`, `prepared`, `rejected`, `filled`, `submitted`, `concluded` or
`withdrawn`. The latest valid event wins, and `restore` puts a Posting back to
`queued`. An old score-only queue entry can also produce `queued` when there are no
TrackEvents.
_Avoid_: status, stage
