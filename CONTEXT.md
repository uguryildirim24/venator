# Venator

An automated job-application pipeline: it discovers postings, filters them against the owner's profile, prepares tailored applications, and submits each one only after the owner approves it.

## Language

### People & facts

**Owner**:
The person a Profile applies on behalf of. Exactly one Owner per Profile.
_Avoid_: user, candidate. Also avoid "applicant" — except in the questions sent to Jev, which name the Owner as an employer would see them.

A new Profile starts with a conservative Jev policy in shadow. **Jev it** checks Hard Filter passes when pressed; Refresh does not score them. Its look first / review / skip labels are **An estimate, not a verdict**. Jev results bind to that Profile's compiled facts and policy; another Profile's activation does not carry over. The current release's domain questions are life-sciences-specific; a different field may yield review instead of useful look-first ordering until a release for that field exists.

**Profile**:
An Owner's canonical facts: resume data, work-authorization status, locations, role preferences, and what to search for. The only source applications may draw from. Profiles are countable: an Install holds one or more, and every stage CLI takes `--profile`.
_Avoid_: config, settings

**Install**:
One person's Venator, with the Profiles and data in it. The boundary between people: one Install per person, never shared. The boundary is the Install's application data directory (`$VENATOR_HOME`, else the platform's usual place) — not a Git checkout, because the product ships to people who have no clone at all; the Install's Profiles and append-only stores live outside the checkout and outside Git (`docs/adr/0002-checkout-is-the-tenant-boundary.md`).
_Avoid_: tenant, account, workspace, instance

### Discovery & filtering

**Source**:
A place postings are discovered from (a job platform, an ATS board, an aggregator API).
_Avoid_: provider, feed, scraper

**Posting**:
One job advertisement as discovered from a Source. Every discovered Posting is kept, whether or not it survives filtering.
_Avoid_: job, listing, opportunity

**Hard Filter**:
A deterministic rule that kills a Posting outright (location, work authorization, role type, education level).
_Avoid_: rule, blacklist

**Filter Decision**:
The recorded verdict on one Posting — which filter kept or killed it, and why. Every Posting gets one; decisions are replayable when filters change. There is no silent drop.
_Avoid_: rejection, drop

**Match**:
A Posting that survived Hard Filters and is worth applying to.

**Dedup**:
Recognizing that two Postings are the same underlying job (across Sources or time) so the Owner never applies to it twice.

**Refresh and Jev it**:
Refresh discovers Postings, records Hard Filter Decisions, and rebuilds View; passes without a current Jev result sit in Awaiting Jev. Only pressing Jev it sorts those passes with Jev. If Jev pauses, the remaining Postings wait for the next Jev it press, not the next Refresh.

### Applying

**Application**:
The tailored bundle prepared for one Match: resume, cover letter, and screening answers.
_Avoid_: submission (that's the act of sending), draft

**Tailoring**:
Reordering and emphasizing real Profile facts for one Match. Tailoring never invents facts.
_Avoid_: generation, rewriting

**Notes**:
The fact-and-emphasis outline drafted for a cover letter; a separate voice model renders Notes into prose.
_Avoid_: prompt, outline

**Review Queue**:
Applications awaiting the Owner's approve/reject. Nothing is submitted without an explicit approval.
_Avoid_: inbox, pending list

**Submission**:
The act and record of delivering an approved Application through the employer's own apply form.
_Avoid_: application (that's the bundle), send

### Filling

**FormSpec**:
The inventory of one employer's application form as observed live: every field's label, kind, whether it is required, its selector, and its exact options. Captured by reconnaissance; describes the form, never the answers.
_Avoid_: schema, form model

**FillPlan**:
The answers proposed for one FormSpec — each a value plus the Profile fact it came from — together with the fields left `unmapped` and why. A field with no honest mappable answer is unmapped, never guessed; a plan carries `never_submit: true` and the driver refuses to execute one that does not.
_Avoid_: form data, answers, payload

**Dry Run**:
Filling a form with no way to send it: state-changing requests aborted at the network layer, submit events cancelled and counted, file fields skipped, no code path that clicks Apply. This describes `venator.browser.fill`. The separate visible application handoff supports assisted fields and uploads for user review and submission. A dry run remains evidence, not a Submission.
_Avoid_: test run, simulation, preview

### Tracking

**TrackEvent**:
One recorded fact about an Application's life: `approve`, `reject`, `prepare`, `fill`, `submit`, `restore`, `outcome`, or `withdraw`. Append-only, with a validated actor, details, time and Profile ownership. Personal actions can begin without an earlier score or approval. `submit` records the owner's manual applied report; it does not itself send an employer request.
_Avoid_: status update, log entry

**Application State**:
The effective state of one Posting, folded from its TrackEvents in append order — `queued`, `approved`, `prepared`, `rejected`, `filled`, `submitted`, `concluded`, `withdrawn`. The latest valid event determines the state; `restore` returns a job to `queued`. Legacy score-only queue entries can also be derived when no TrackEvent exists.
_Avoid_: status, stage
