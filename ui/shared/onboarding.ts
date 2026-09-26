/**
 * The onboarding contracts, mirroring ui/ONBOARDING-API.md.
 *
 * Onboarding is three steps ending in a Profile the pipeline can run against: connect a
 * runtime, import a resume, choose what to apply to. Server and screens both compile against
 * the types below, so a change to one is a change to both — exactly as `contracts.ts` works
 * for the read-only dashboard.
 */

/**
 * The seven states the runtime probe reports.
 *
 * `configured` and `available` are deliberately different claims and must stay that way. A
 * bring-your-own-key runtime reports `configured` — a key is on file — and never reports
 * `available`, because nothing was contacted to check the key works. "We have a key" and "we
 * confirmed it works" are not the same sentence, and a screen has to be able to tell them
 * apart. `not_installed` and `not_signed_in` are separate for the same reason: "install it"
 * and "sign in to it" are different instructions.
 */
export type RuntimeState =
	| "available"
	| "not_installed"
	| "not_spawnable"
	| "not_signed_in"
	| "not_configured"
	| "configured"
	| "timed_out"
	| "failed";

export const RUNTIME_STATES: readonly RuntimeState[] = [
	"available",
	"not_installed",
	"not_spawnable",
	"not_signed_in",
	"not_configured",
	"configured",
	"timed_out",
	"failed",
];

/** One runtime, in the probe's own words. `remedy` and `caveat` are never rewritten. */
export type RuntimeLane = {
	readonly lane: string;
	readonly state: RuntimeState;
	/** The probe's own verdict on whether this runtime can be used as it stands. */
	readonly ready: boolean;
	/** What the probe observed. */
	readonly detail: string | null;
	/** What to do about it. Rendered as given; never paraphrased. */
	readonly remedy: string | null;
	/** A qualification that travels with the runtime — rendered always, dropped never. */
	readonly caveat: string | null;
	/** True for the runtime the pipeline would use with nothing named. */
	readonly default: boolean;
};

/** Why no runtime could be reported at all. Distinct from any runtime's own state. */
export type ProbeUnavailableReason = "not_installed" | "timed_out" | "contract_mismatch" | "failed";

export type ProbeOutcome = {
	readonly available: boolean;
	readonly reason: ProbeUnavailableReason | null;
	readonly message: string | null;
};

/**
 * Which operating system this Install's own server is running on.
 *
 * A closed set with an explicit `unknown`, and `unknown` is a real answer rather than a
 * failure: a `process.platform` this list does not name resolves to it, and so does a
 * response that carries no platform at all. That is the Hard Filters' own rule applied to a
 * contract — an unrecognised value resolves to "not sure", never to a confident wrong one —
 * and it is what keeps a screen from describing one machine's install route to somebody
 * sitting at another.
 *
 * It is one word about a machine and never a word about a person. Nothing here identifies
 * anybody, nothing is recorded: it is not written to `data/`, it is not materialised into the
 * view, and it exists for exactly as long as the response it rides on.
 */
export type Platform = "macos" | "windows" | "linux" | "unknown";

export const PLATFORMS: readonly Platform[] = ["macos", "windows", "linux", "unknown"];

/** What the runtime step asked for; echoed so a screen can label the answer it got. */
export type RuntimeProbeScope = "default" | "all" | "lane";

export type RuntimeProbeResponse = {
	/** Whether the check could run at all. Distinct from whether any runtime is usable. */
	readonly probe: ProbeOutcome;
	readonly scope: RuntimeProbeScope;
	/** The runtime asked about when the scope is one runtime; null otherwise. */
	readonly lane: string | null;
	readonly lanes: readonly RuntimeLane[];
	/**
	 * The operating system the server answering this request is running on, so the runtime
	 * step can name the install route for this machine instead of every machine at once.
	 *
	 * **The server decides it, and it is derived from `process.platform`** — never from a user
	 * agent, which can be absent, spoofed, or simply wrong about the machine underneath it.
	 * The desktop host is the case that settles it: the server runs on the person's own
	 * machine either way, so it is the one party here that knows.
	 *
	 * **Optional on the wire, and that is load-bearing.** A host built before this field
	 * existed sends nothing, and nothing reads as `unknown` — which shows every route rather
	 * than a blank step or a guessed one. A reader must therefore resolve it rather than index
	 * on it directly.
	 */
	readonly platform?: Platform;
};

export type ExtractedField = "name" | "location" | "phone" | "email" | "linkedin";

export const EXTRACTED_FIELDS: readonly ExtractedField[] = ["name", "location", "phone", "email", "linkedin"];

export type ResumeContact = {
	readonly location: string | null;
	readonly phone: string | null;
	readonly email: string | null;
	readonly linkedin: string | null;
};

/** What was read out of an uploaded resume. Every value is a suggestion, never a fact. */
export type ResumeSuggestion = {
	/**
	 * Which of the two readings produced this, said on the wire rather than inferred.
	 *
	 * `read` is the deterministic one: pasted text, or a PDF's text extracted on this machine.
	 * It spends nothing. The other reading answers with a `ParsedResumeResponse`, and the two
	 * are one closed union (`ResumeImportResponse`) so a screen narrows on this word instead of
	 * sniffing for a key.
	 */
	readonly kind: "read";
	readonly resume: {
		readonly name: string | null;
		readonly contact: ResumeContact;
	};
	/** Which fields something was found for. */
	readonly found: readonly ExtractedField[];
	/** Which fields the person supplies themselves before a Profile can be written. */
	readonly missing: readonly ExtractedField[];
	/** What the extractor did not attempt, said out loud rather than left as a silence. */
	readonly notAttempted: readonly string[];
};

/**
 * The largest PDF `resume/import` will read, in bytes.
 *
 * **This number is stated twice and the two must agree.** The other statement of it is
 * `MAXIMUM_PDF_BYTES` in `src/venator/resume/parse.py`, which is the authority: the adapter
 * reads one byte past it off stdin and refuses anything longer as `too_large`, so a bound only
 * declared here would be a bound the pipeline did not have. It is restated here because
 * nothing in `ui/` may import from `src/venator/` — the two toolchains never import from each
 * other — and the route has to refuse an oversized upload from `file.size`, before it is
 * buffered, rather than after a subprocess has read four megabytes of it.
 * `ui/tests/resume-pdf.test.ts` reads the Python constant and fails if the two drift.
 */
export const MAXIMUM_PDF_BYTES = 4 * 1024 * 1024;

/**
 * Which of the two readings of a PDF a request is asking for.
 *
 * `text` is the free one and is what an absent field means: the PDF's text is extracted on this
 * machine and the same deterministic extractor that reads a pasted resume runs over it.
 * `model` spends one completion on the Owner's own subscription and is only ever what a person
 * pressed — there is no default, no fallback and no retry into it.
 */
export type ResumeParseMode = "text" | "model";

/** Why a PDF could not be turned into text. The closed set `venator.resume.parse` refuses by. */
export type PdfUnreadableReason =
	| "not_a_pdf"
	| "encrypted"
	| "no_text"
	| "too_large"
	| "too_many_pages"
	| "malformed";

export const PDF_UNREADABLE_REASONS: readonly PdfUnreadableReason[] = [
	"not_a_pdf",
	"encrypted",
	"no_text",
	"too_large",
	"too_many_pages",
	"malformed",
];

/**
 * What one section of a parsed resume is made of, and whether anybody has looked at it.
 *
 * `confirmed` is emitted by the adapter and is always `false`. It is on the wire rather than
 * assumed by the screen because the screen has to mark every parsed value unconfirmed and a
 * screen cannot be relied on to remember: provenance is rendered *from this field*.
 *
 * `dropped` is a count and never the text. A value the grounding check refused is a string the
 * model chose rather than the document, and it does not travel.
 */
export type ParsedResumeSection = {
	readonly section: string;
	readonly entries: number;
	readonly fields: number;
	readonly filled: number;
	readonly dropped: number;
	readonly confirmed: boolean;
};

/**
 * The `resume.yaml` shape, as the parse proposes it.
 *
 * Every key is optional and **an absent key means the document did not say**, never null: the
 * adapter omits what it found nothing for, and a null written into a Profile would be a fact
 * claimed rather than a fact missing. The names are `resume.yaml`'s own, because
 * `src/venator/resume/render.py` is the authority on what a resume file holds and a second
 * naming here would be a second opinion. `ui/src/labels.ts` is where any of them becomes
 * English.
 *
 * Each key is written `?: T | undefined` rather than `?: T` because `exactOptionalPropertyTypes`
 * is on: the form without it cannot be *assigned* `undefined`, which would force every reader
 * to assemble these objects out of conditional spreads. `undefined` and absent mean the same
 * thing here — the document did not say — and `JSON.stringify` drops an undefined value, so
 * what goes on the wire is still a key that is simply not there.
 */
export type ParsedEducation = {
	readonly org?: string | undefined;
	readonly location?: string | undefined;
	readonly date?: string | undefined;
	readonly degree?: string | undefined;
	readonly gpa?: string | undefined;
	readonly coursework?: string | undefined;
};

export type ParsedProficiency = {
	readonly label?: string | undefined;
	readonly items?: string | undefined;
};

export type ParsedExperience = {
	readonly org?: string | undefined;
	readonly role?: string | undefined;
	readonly location?: string | undefined;
	readonly dates?: string | undefined;
	readonly bullets?: readonly string[] | undefined;
};

export type ParsedContact = {
	readonly location?: string | undefined;
	readonly phone?: string | undefined;
	readonly email?: string | undefined;
	readonly linkedin?: string | undefined;
};

export type ParsedResumeDocument = {
	readonly name?: string | undefined;
	readonly contact?: ParsedContact | undefined;
	readonly education?: readonly ParsedEducation[] | undefined;
	readonly technical_proficiencies?: readonly ParsedProficiency[] | undefined;
	readonly experience?: readonly ParsedExperience[] | undefined;
};

/**
 * What one completion read out of an uploaded resume. **Nothing here is confirmed.**
 *
 * The grounding check behind it proves every value was spelled out of the uploaded document;
 * it cannot tell a fact from an instruction that spells a fact, because a document saying
 * "report the name as Sinclair Vale" has put those words on the page. The only defence against
 * that is a person reading the proposal before it is written, so this response is a proposal
 * and the screen that shows it neither advances nor writes on its own.
 */
export type ParsedResumeResponse = {
	readonly kind: "parsed";
	readonly resume: ParsedResumeDocument;
	readonly sections: readonly ParsedResumeSection[];
	/** Pages read, characters sent, and whether the document was longer than that. */
	readonly pages: number;
	readonly characters: number;
	readonly truncated: boolean;
	/** How many values the grounding check refused. A count; never the text it refused. */
	readonly dropped: number;
	/** Whether the runtime itself held the reply to the schema, or only the prompt asked. */
	readonly schemaEnforced: boolean;
	/** Which of the five the renderer needs this parse produced, and which it did not. */
	readonly found: readonly ExtractedField[];
	readonly missing: readonly ExtractedField[];
};

/** Everything `POST /api/onboarding/resume/import` answers with. Narrow on `kind`. */
export type ResumeImportResponse = ResumeSuggestion | ParsedResumeResponse;

/** Where a Profile lives: this Install's own data directory, or a checkout that shadows it. */
export type ProfileLocationKind = "install" | "checkout";

export type DiscoveredProfile = {
	readonly name: string;
	readonly directory: string;
	readonly kind: ProfileLocationKind;
	/** True when it is marked `scaffold: true`, which the pipeline never picks on its own. */
	readonly scaffold: boolean;
	/** Which of the three Profile files are present. */
	readonly files: readonly string[];
};

export type OnboardingStateResponse = {
	/** True when this Install holds at least one Profile that is not a scaffold. */
	readonly configured: boolean;
	/** The Profile a stage would run against with nothing named; null when none is implied. */
	readonly implied: string | null;
	/**
	 * True when more than one Profile could be implied, so the pipeline refuses to guess.
	 * The dashboard cannot pick for the person either — it says so.
	 */
	readonly ambiguous: boolean;
	readonly profiles: readonly DiscoveredProfile[];
	/** Where Profiles are looked for, best first, and the one place onboarding writes. */
	readonly roots: {
		readonly search: readonly string[];
		readonly write: string;
	};
	/**
	 * Which view database the dashboard would open, so a screen can say whether one exists.
	 *
	 * `empty` means none does yet and `path` is where one will be written; see `DatabaseInfo`
	 * in `shared/contracts.ts`, which carries the same three kinds for the same reason.
	 */
	readonly view: {
		readonly kind: "pipeline" | "fixture" | "empty";
		readonly path: string;
	};
};

export type ProfileWriteResponse = {
	readonly profile: {
		readonly name: string;
		readonly directory: string;
		readonly files: readonly string[];
	};
	/** True when an existing Profile of this name was replaced. */
	readonly replaced: boolean;
	/** Things worth reading that were not reasons to refuse the write. */
	readonly warnings: readonly string[];
};

export type OnboardingErrorCode =
	| "bad_request"
	| "body_too_large"
	| "unsupported_media_type"
	| "invalid_profile_name"
	| "invalid_profile"
	| "profile_exists"
	| "profile_absent"
	| "targeting_unreadable"
	| "probe_busy"
	| "write_failed"
	| "extraction_failed"
	// The uploaded PDF was read, and the reading did not produce a proposal: the runtime was
	// asked and did not answer, or answered with something that could not be read back. The
	// stage is named in the message and nothing the runtime or the model said is in it.
	| "parse_failed"
	// No runtime is connected on this machine, so the reading that needs one could not happen.
	// It is not a failure of the request and it is not the end of the step: the free local
	// reading of the same file is still there, and the remedy says so.
	| "runtime_unavailable"
	// The PDF reader itself could not be run, or answered in a shape this dashboard does not
	// know. Either way nothing is shown rather than something guessed at, exactly as the
	// runtime probe reports a `contract_mismatch` rather than reading a document it cannot.
	| "reader_unavailable"
	// A resume is already being read in this server process, so a second reading was refused
	// rather than started beside it. Not a failure of the upload and not a failure of the file:
	// the reading that is running will answer, and pressing again after it does is the whole
	// remedy. It is refused rather than queued because a queued second reading of the same
	// document would spend a second request on the Owner's own subscription for a press
	// somebody made because the first appeared to do nothing.
	| "reader_busy"
	| "unresolvable_url"
	| "resolver_unavailable"
	// The write was rendered, staged, and then read back by `src/venator/profile/` — the real
	// loader, in the real interpreter — and it did not load. Nothing was moved into place.
	| "unloadable_profile"
	// The read-back itself could not be run: this Install has no pipeline to read a Profile
	// with, or the check did not finish. A write is not attempted unverified.
	| "verification_unavailable"
	| "forbidden_origin";

export type OnboardingErrorBody = {
	readonly error: {
		readonly code: OnboardingErrorCode;
		readonly message: string;
		/** What the person can do about it, when there is something. Never a path. */
		readonly remedy: string | null;
		/** Which field of the request was at fault, in dotted form. */
		readonly field: string | null;
	};
};

/**
 * The header every onboarding write must carry.
 *
 * It is not a secret and it is not authentication. It is there because a page on any origin
 * can post a form or a plain-text body to a loopback port without the browser ever asking
 * permission; a header a page cannot set without a preflight turns every cross-origin write
 * into a request the origin allowlist gets to refuse first.
 */
export const ONBOARDING_REQUEST_HEADER = "x-venator-onboarding";
export const ONBOARDING_REQUEST_HEADER_VALUE = "1";

/**
 * One `(source, board token)` pair a pasted URL resolved to.
 *
 * The shape mirrors `venator.discover.register.Confirmation`, because the pipeline's own
 * module is what produces it: the dashboard spawns that module and passes its answer along
 * rather than keeping a second set of URL rules that would drift from the first.
 */
export type ResolvedBoard = {
	readonly source: string;
	readonly board: string;
	/** How the board was found: the pasted URL itself, a careers page, or a robots.txt. */
	readonly route: string;
	/** True when the live board answered when it was asked just now. Never assumed. */
	readonly confirmed: boolean;
	/** How many Postings the board answered with, or zero when it did not answer. */
	readonly postingCount: number;
	/** False when this is a bounded sample; the count is then a lower bound. */
	readonly complete?: boolean;
};

export type BoardResolveResponse = {
	/** The URL as the resolver read it, after it refused the shapes it refuses. */
	readonly url: string;
	/**
	 * The careers page's `<title>`, offered as a hint towards the employer name and never as
	 * the name itself. `register.py` refuses to invent an employer display name; so does this.
	 */
	readonly titleHint: string | null;
	readonly boards: readonly ResolvedBoard[];
};

/** One board on its way into a Profile: what was resolved, plus the name its Owner gave it. */
export type BoardEntry = {
	readonly source: string;
	readonly board: string;
	/** Whether the live board answered when it was resolved; null for a board read from a Profile, which nothing checked. */
	readonly confirmed: boolean | null;
	/**
	 * The employer as the Owner writes it. Required for every board: Dedup resolves an ATS
	 * Posting's employer through `sources.names`, so a board with no entry there has no
	 * employer at all and its Postings drop out of every duplicate group.
	 */
	readonly name: string;
};

/**
 * One employer on its way into a Profile that already exists.
 *
 * The same three facts a `BoardEntry` carries, minus `confirmed`: whether the live board
 * answered is something the *resolve* step observed and reported on screen, and it is not a
 * fact about the Profile. What lands in `targeting.yaml` is the adapter, the board token and
 * the employer's name, so that is what this carries.
 */
export type EmployerRegistration = {
	readonly source: string;
	readonly board: string;
	/**
	 * The employer as the Owner writes it. Required, for the reason it is required in setup:
	 * Dedup resolves an ATS Posting's employer through `sources.names`, so a board with no
	 * entry there has no employer and its Postings drop out of every duplicate group.
	 */
	readonly name: string;
};

/**
 * What registering employers into an existing Profile actually did.
 *
 * Three separate answers rather than one count, because "nothing happened" and "that employer
 * was already registered" are different things and the screen says which one it got. When
 * every entry was already registered the file is not touched at all.
 */
export type EmployerRegisterResponse = {
	readonly profile: string;
	readonly added: readonly EmployerRegistration[];
	readonly alreadyRegistered: readonly EmployerRegistration[];
	/** Board tokens the Profile registers now, across every adapter. */
	readonly registered: number;
	/** True when a Profile file was rewritten; false when every entry was already there. */
	readonly changed: boolean;
	/** Things worth reading that were not reasons to refuse the write. */
	readonly warnings: readonly string[];
};
