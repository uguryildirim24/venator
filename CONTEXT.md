# Venator vocabulary

Venator finds job postings, filters them against one person's Profile, helps prepare
applications, and tracks them. The person always submits. Venator never does.

These are the words the code, the commits and the app use. Each entry says what to
avoid saying instead.

## People and facts

**Owner**
The person a Profile applies for. One Owner per Profile. Refer to the Owner as
"they"; never guess pronouns.
_Avoid_: user, candidate. Also avoid "applicant", except in the questions sent to
Jev, which describe the Owner the way an employer would see them.

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
`education_fit`, `role_target` and `eligibility`, with wording from the Profile.
When the wording is unclear, the Posting passes. Killing a good Posting by mistake
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

**Jev**
A TypeSafe classifier that sorts Hard Filter passes into look first, review and
skip. A new Profile starts with a cautious Jev policy in shadow mode. Jev's labels
are an estimate, not a verdict. Results are tied to that Profile's facts and policy;
another Profile's results never carry over. The current release's questions are
written for life-sciences jobs, so other fields get more review and less useful
look-first ordering until a release for that field exists.

**Refresh and Jev it**
Refresh fetches Postings, records Filter Decisions and rebuilds the view. Passes
without a current Jev result wait in Awaiting Jev. Only pressing Jev it sends them to
Jev. If Jev pauses, the rest wait for the next Jev it, not the next Refresh.
The sidebar's location choice narrows what is displayed; it is not a Hard Filter
and never changes a Filter Decision.

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
This is `venator.browser.fill`. The visible handoff is a separate tool: it fills
confirmed fields and uploads files for the Owner to review and submit. A Dry Run is
evidence, not a Submission.
_Avoid_: test run, simulation, preview

## Tracking

**TrackEvent**
One recorded fact about an Application: `approve`, `reject`, `prepare`, `fill`,
`submit`, `restore`, `outcome` or `withdraw`. Events are appended, never edited, and
each has a checked actor, details, a time and the Profile it belongs to. The Owner
can act on a Posting before Jev or any score has seen it. `submit` records the
Owner's own report that they applied; it sends nothing to an employer. In the app,
`approve` is Save and `reject` is Dismiss.
_Avoid_: status update, log entry

**Application State**
Where one Posting stands, worked out from its TrackEvents in order: `queued`,
`approved`, `prepared`, `rejected`, `filled`, `submitted`, `concluded` or
`withdrawn`. The latest valid event wins, and `restore` puts a Posting back to
`queued`. An old score-only queue entry can also produce `queued` when there are no
TrackEvents.
_Avoid_: status, stage
