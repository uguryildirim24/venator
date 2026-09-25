/**
 * The onboarding write surface — the only place in this repository that writes anything.
 *
 * **Why it exists.** Onboarding has to end with a Profile on disk, and a person setting up
 * the product has no checkout, no editor open on a YAML file, and no reason to have one.
 * Three steps produce that Profile: connect a runtime, import a resume, choose what to apply
 * to. Steps one and two write nothing at all; step three writes three files into this
 * Install's own application data directory.
 *
 * **And one route for what happens after them.** Setup finishes whether or not an employer was
 * registered, and it must — resolving a board needs the network, so a hard blocker there would
 * refuse a Profile to somebody whose connection cannot reach a careers page. That leaves a
 * state somebody can be stranded in: a Profile, no employers, and a run control correctly off
 * because Discover has nothing to poll. `/employers` is the way back out of it, and it edits
 * the Profile that is already there rather than writing a new one — one file of it, appended
 * to under `sources` and nowhere else.
 *
 * **What it may never do.** This is a separate surface from the dashboard API on purpose, and
 * the separation is the point rather than the tidiness:
 *
 * - The dashboard API stays `GET`-only and read-only. Nothing here is mounted on it, nothing
 *   here writes through it, and the view database handle stays `readOnly: true`.
 * - Nothing here performs a state-changing request against anything. The never-submit
 *   invariant is not weakened by a byte: no Apply, no Submit, no account creation, no
 *   CAPTCHA, no approve/reject, and no `POST`, `PUT`, `PATCH` or `DELETE` leaves this
 *   process. One route still reads: `/boards/resolve` runs `venator.discover.register`, which
 *   `GET`s the employer page a person pasted and the ATS board it names, and follows no
 *   redirect. Reading a public careers page is how a board becomes registrable at all — the
 *   pipeline's own CLI does exactly this — and it is as far as it goes.
 * - All five routes run the pipeline as a subprocess, and every one of them reads.
 *   `/runtime/probe` runs `venator.llm.probe`; `/boards/resolve` runs
 *   `venator.discover.register`; `/profile` and `/employers` each run
 *   `onboarding/verify_profile.py`, which loads the Profile they have staged and have not yet
 *   moved into place; and `/resume/import` runs `onboarding/parse_resume.py`, which reads an
 *   uploaded PDF. The verifier writes nothing, starts nothing and makes no request — it answers
 *   whether `src/venator/profile/` reads a directory back — and a candidate it refuses refuses
 *   the write, leaving what is on disk byte-identical. The reader writes nothing either, and it
 *   is the one place on this surface besides the probe that can spend the Owner's own
 *   subscription: only on the path a person selected by name, only for one completion, and
 *   never by default, by fallback or by retry. See the route itself.
 * - Nothing here writes outside `<application data directory>/profiles/`. Not the checkout,
 *   not `data/`, not `build/`, not one file anywhere else. Two routes write at all —
 *   `/profile`, which creates a Profile, and `/employers`, which appends to one file of a
 *   Profile that exists in the Install. A same-named checkout Profile cannot supersede it.
 * - Nothing here fabricates a Profile fact. A screening answer with no value stays null, an
 *   EEO field is never defaulted to a decline, and a sponsorship answer is refused unless the
 *   Profile states outright that sponsorship is required.
 * - Nothing here chooses a runtime on someone's behalf or falls back from one to another.
 *
 * **Why it is safe on loopback.** The server binds 127.0.0.1 and that is the boundary
 * (ADR-0002). CORS is a browser-side courtesy on top, so it is kept to exactly what
 * onboarding needs: the same origin allowlist as the dashboard, plus `POST`. A page on some
 * other origin can still post a form or a plain-text body to a loopback port without the
 * browser asking anyone's permission, so every write additionally requires a header no
 * cross-origin page can set without a preflight the allowlist then refuses.
 */

import { Hono } from "hono";
import { cors } from "hono/cors";

import {
	MAXIMUM_PDF_BYTES,
	ONBOARDING_REQUEST_HEADER,
	ONBOARDING_REQUEST_HEADER_VALUE,
	type BoardResolveResponse,
	type EmployerRegisterResponse,
	type EmployerRegistration,
	type ExtractedField,
	type ParsedResumeResponse,
	type PdfUnreadableReason,
	type ProfileWriteResponse,
	type ResumeImportResponse,
	type ResumeParseMode,
	type ResumeSuggestion,
	type RuntimeProbeResponse,
	type RuntimeProbeScope,
} from "../../shared/onboarding.ts";
import { checkLocalAction, LOCAL_ACTION_ORIGINS } from "../http/local-action-guard.ts";
import { hostPlatform } from "../platform.ts";
import { documentRuntime, jevKey, saveDocumentRuntime, saveJevKey } from "../install-settings.ts";
import { resolveBoards } from "./boards.ts";
import { MAXIMUM_EMPLOYERS_PER_REQUEST, registerEmployers } from "./employers.ts";
import { OnboardingError } from "./errors.ts";
import { asBoolean, asList, asMapping, asText, at, isPresent, JsonParseError, parseJson } from "./json.ts";
import type { JsonMapping } from "./json.ts";
import { readProfileUpload } from "./profile-upload.ts";
import { validateProfileName, writeProfile, type ProfileProposal } from "./profile.ts";
import { readResumePdf, ResumeReaderBusyError, type ResumePdfOutcome } from "./resume-pdf.ts";
import {
	extractResume,
	fieldStanding,
	MAXIMUM_RESUME_BYTES,
	readResumeText,
	ResumeReadError,
	type FieldStanding,
} from "./resume.ts";
import {
	probeRuntimes,
	RuntimeLaneNameError,
	RuntimeProbeBusyError,
	validateLaneName,
	type ProbeRequest,
} from "./runtime.ts";

/** A whole Profile as JSON. Nothing here is a large file. */
const MAXIMUM_JSON_BYTES = 512 * 1024;

/**
 * The header-and-origin gate, now shared with the run surface (`server/http/`).
 *
 * The check is the same on both and the reasoning is the same on both, so it lives once. What
 * stays here is this surface's own vocabulary: the guard says whether a request may act, and
 * an `OnboardingError` is what onboarding answers with.
 */
function requireOnboardingHeader(header: string | undefined, origin: string | undefined): void {
	const refusal = checkLocalAction(ONBOARDING_REQUEST_HEADER, ONBOARDING_REQUEST_HEADER_VALUE, header, origin);
	if (refusal !== null) throw new OnboardingError(refusal.reason, refusal.message, refusal.remedy);
}

/**
 * Refuses an oversized body before it is read into memory.
 *
 * A declared length is only a claim, so the actual text is measured again after it arrives;
 * this check is what stops the claim being buffered in the first place.
 */
function refuseOversizedBody(declared: string | undefined, limit: number, remedy: string | null): void {
	const length = Number.parseInt(declared ?? "", 10);
	if (Number.isInteger(length) && length > limit) {
		throw new OnboardingError("body_too_large", "That request is larger than this step accepts.", remedy);
	}
}

function readJsonBody(text: string): JsonMapping {
	if (text.length > MAXIMUM_JSON_BYTES) {
		throw new OnboardingError("body_too_large", "That request is larger than this step accepts.");
	}
	if (text.trim() === "") return {};
	try {
		const mapping = asMapping(parseJson(text));
		if (mapping === null) throw new OnboardingError("bad_request", "The request body must be a JSON object.");
		return mapping;
	} catch (cause) {
		if (cause instanceof JsonParseError) throw new OnboardingError("bad_request", "The request body is not valid JSON.");
		throw cause;
	}
}

/** `{}` and `{"scope":"default"}` mean the same thing: probe the default runtime only. */
function probeRequestFrom(body: JsonMapping): ProbeRequest {
	const lane = at(body, "lane");
	const all = asBoolean(at(body, "all")) ?? false;
	if (isPresent(lane) && all) {
		throw new OnboardingError("bad_request", "Ask about one runtime or about all of them, not both.", null, "lane");
	}
	if (isPresent(lane)) {
		const name = asText(lane);
		if (name === null || name.trim() === "") {
			throw new OnboardingError("bad_request", "The runtime to check must be named as text.", null, "lane");
		}
		return { lane: name.trim() };
	}
	return all ? "all" : "default";
}

function scopeOf(request: ProbeRequest): RuntimeProbeScope {
	if (request === "default") return "default";
	if (request === "all") return "all";
	return "lane";
}

function proposalFrom(body: JsonMapping): ProfileProposal {
	const rawName = asText(at(body, "name"));
	if (rawName === null) {
		throw new OnboardingError("invalid_profile_name", "A Profile needs a name.", "Name this search.", "name");
	}
	const name = validateProfileName(rawName);
	const overwrite = asBoolean(at(body, "overwrite")) ?? false;
	const section = (key: string): JsonMapping | null => {
		const value = at(body, key);
		if (!isPresent(value)) return null;
		const mapping = asMapping(value);
		if (mapping === null) {
			throw new OnboardingError("invalid_profile", `${key} must be a mapping.`, null, key);
		}
		return mapping;
	};
	return {
		name,
		overwrite,
		resume: section("resume"),
		constraints: section("constraints"),
		targeting: section("targeting"),
	};
}

/** One registration request: the Profile it is for, and the employers to put in it. */
type EmployerRequest = { readonly profile: string; readonly boards: readonly EmployerRegistration[] };

/**
 * The employers a registration request names, read out of the body and nothing more.
 *
 * Every value is checked again by `validateEmployer` before it reaches a file; what happens
 * here is only the reading of the JSON, so a body that is not the right shape is a
 * `bad_request` naming the field rather than a refusal from deep inside the writer.
 */
function employersFrom(body: JsonMapping): EmployerRequest {
	const profile = asText(at(body, "profile"));
	if (profile === null || profile.trim() === "") {
		throw new OnboardingError(
			"bad_request",
			"No Profile was named to register the employer in.",
			"A run is always against one named Profile, and so is this.",
			"profile",
		);
	}
	const list = asList(at(body, "boards"));
	if (list === null) {
		throw new OnboardingError("bad_request", "The employers to register must arrive as a list.", null, "boards");
	}
	if (list.length === 0) {
		throw new OnboardingError("bad_request", "No employer was named to register.", null, "boards");
	}
	if (list.length > MAXIMUM_EMPLOYERS_PER_REQUEST) {
		throw new OnboardingError(
			"bad_request",
			`At most ${MAXIMUM_EMPLOYERS_PER_REQUEST} employers can be registered in one request.`,
			"Register them in smaller batches.",
			"boards",
		);
	}
	return {
		profile: profile.trim(),
		boards: list.map((item, index) => {
			const mapping = asMapping(item);
			const source = mapping === null ? null : asText(at(mapping, "source"));
			const board = mapping === null ? null : asText(at(mapping, "board"));
			const name = mapping === null ? null : asText(at(mapping, "name"));
			if (source === null || board === null || name === null) {
				throw new OnboardingError(
					"bad_request",
					"Each employer is named by its job board, the board on it, and the employer's own name.",
					null,
					`boards.${String(index)}`,
				);
			}
			return { source, board, name };
		}),
	};
}

/** The bytes `%PDF-` is spelled in. Content decides what a file is here; a filename never does. */
const PDF_MAGIC: readonly number[] = [0x25, 0x50, 0x44, 0x46, 0x2d];

function looksLikePdf(bytes: Uint8Array): boolean {
	return PDF_MAGIC.every((byte, index) => bytes[index] === byte);
}

/**
 * One resume upload, as the request carried it.
 *
 * `parse` is the only field on this surface that decides whether money is spent, so it is read
 * once, here, and it is read to fail towards free.
 */
type ResumeUpload = {
	readonly bytes: Uint8Array;
	/**
	 * True only for a PDF that arrived as an attachment.
	 *
	 * Two conditions, both required. The bytes must begin as a PDF does — decided by looking,
	 * because a `.txt` holding a PDF is still a PDF — *and* they must have arrived on the
	 * multipart route, because a PDF pasted into a JSON `text` key or posted as a `text/plain`
	 * body is a person doing the wrong thing with the right file, and the refusal that tells
	 * them so is more useful than four megabytes of binary quietly working.
	 */
	readonly pdf: boolean;
	/** Which reading was asked for. `text` unless an attached PDF was asked to be parsed. */
	readonly parse: ResumeParseMode;
	readonly lane: string | null;
};

/**
 * Which reading a form asked for.
 *
 * **Spending is never a default.** Exactly one value selects the completion — the word `model`
 * — and everything else is the free local reading: the field absent, the field empty, the
 * field holding another word, the field holding a file. There is no fallback into the paid
 * reading when the free one finds little, and no retry that escalates into it. A person
 * pressed the button that says what it costs, or nothing costs anything.
 */
function parseModeFrom(form: FormData): ResumeParseMode {
	return form.get("parse") === "model" ? "model" : "text";
}

/**
 * The runtime a parse should run on, when one was named.
 *
 * Validated the way `probeRequestFrom` validates the same value — text, non-empty, then
 * `validateLaneName`'s own rule — rather than by a second rule written here. Null is not
 * "choose one": it means nothing is named, and the choice stays where
 * `venator.llm.adapter` already makes it.
 */
function laneFrom(form: FormData): string | null {
	const named = form.get("lane");
	if (named === null) return null;
	if (named instanceof File) {
		throw new OnboardingError("bad_request", "The runtime to use must be named as text.", null, "lane");
	}
	const lane = named.trim();
	if (lane === "") return null;
	return validateLaneName(lane);
}

async function multipartUpload(request: Request): Promise<ResumeUpload> {
	const form = await request.formData();
	const file = form.get("file") ?? form.get("resume");
	if (file === null) {
		throw new OnboardingError("bad_request", "No resume was attached.", "Choose a file, or paste the text instead.");
	}
	if (file instanceof File) {
		// `request.formData()` has already parsed and buffered the multipart body. The route's
		// `Content-Length` check is the pre-buffer guard. This size check is a backstop when that
		// header is missing or false; such a body can reach the multipart parser's own limit
		// before this check refuses it. The PDF bound applies here because an attachment may be a
		// PDF. A file that is not a PDF is held to the text bound below after its bytes are read.
		if (file.size > MAXIMUM_PDF_BYTES) {
			throw new OnboardingError(
				"body_too_large",
				"That file is larger than this step accepts.",
				"Attach a smaller PDF, or paste the resume text instead.",
			);
		}
	}
	const bytes =
		file instanceof File ? new Uint8Array(await file.arrayBuffer()) : new TextEncoder().encode(file);
	const pdf = looksLikePdf(bytes);
	if (!pdf && bytes.length > MAXIMUM_RESUME_BYTES) {
		throw new OnboardingError(
			"body_too_large",
			"That file is larger than this step accepts.",
			"Paste the resume text instead, or save a plain-text copy of it.",
		);
	}
	return { bytes, pdf, parse: pdf ? parseModeFrom(form) : "text", lane: laneFrom(form) };
}

/**
 * The resume a request carried, and what it asked to have done with it.
 *
 * Three shapes arrive and only one of them may be a PDF. The other two are unchanged from the
 * day this route was written: a JSON body with a `text` key, and a `text/*` body. Both are
 * deterministic, spend nothing, and spawn nothing.
 */
async function resumeUploadFrom(request: Request): Promise<ResumeUpload> {
	const contentType = request.headers.get("content-type") ?? "";
	if (contentType.startsWith("multipart/form-data")) return await multipartUpload(request);
	if (contentType.startsWith("application/json")) {
		const body = readJsonBody(await request.text());
		const text = asText(at(body, "text"));
		if (text === null) {
			throw new OnboardingError(
				"bad_request",
				"Send the resume as text under a `text` key, or attach a file.",
				null,
				"text",
			);
		}
		return { bytes: new TextEncoder().encode(text), pdf: false, parse: "text", lane: null };
	}
	if (contentType.startsWith("text/")) {
		return { bytes: new Uint8Array(await request.arrayBuffer()), pdf: false, parse: "text", lane: null };
	}
	throw new OnboardingError(
		"unsupported_media_type",
		"This step reads plain text, or an attached PDF.",
		"Paste the resume text, or attach the resume as a PDF.",
	);
}

/**
 * What to say about a PDF that could not be turned into text.
 *
 * One fixed sentence and one fixed remedy per reason, written here. Nothing `pypdf` said
 * travels — its messages carry byte offsets, object numbers and fragments of the document — so
 * what crosses the seam is a word from a closed set and this table turns it into English.
 */
const UNREADABLE_PDF: ReadonlyMap<PdfUnreadableReason, { readonly message: string; readonly remedy: string }> =
	new Map([
		[
			"not_a_pdf",
			{
				message: "That file does not begin as a PDF does.",
				remedy: "Attach the resume as a PDF, or paste its text instead.",
			},
		],
		[
			"encrypted",
			{
				message: "That PDF is locked, and this step never asks for a password.",
				remedy: "Open it, save an unlocked copy, and attach that — or paste the text instead.",
			},
		],
		[
			"no_text",
			{
				message: "That PDF carries no text, which is what a scan of a printed page looks like.",
				remedy: "Attach a PDF saved from the document itself rather than scanned from paper, or paste the text.",
			},
		],
		[
			"too_large",
			{
				message: "That file is larger than this step accepts.",
				remedy: "Attach a smaller PDF, or paste the resume text instead.",
			},
		],
		[
			"too_many_pages",
			{
				message: "That PDF has more pages than this step reads.",
				remedy: "Attach the resume on its own, or paste its text instead.",
			},
		],
		[
			"malformed",
			{
				message: "That file begins as a PDF and could not be read as one.",
				remedy: "Open it and save a fresh copy, or paste the resume text instead.",
			},
		],
	]);

/** What a reading that gave up at each stage means, in a sentence with nothing quoted in it. */
const PARSE_FAILURE: ReadonlyMap<string, string> = new Map([
	["extract", "That PDF could not be turned into text on this machine."],
	["complete", "The runtime was asked to read the resume and did not answer."],
	["decode", "The runtime answered with something this step could not read back."],
]);

/** The five the renderer needs, out of a parsed proposal. Absent is absent, never null. */
function parsedFieldStanding(parsed: Extract<ResumePdfOutcome, { readonly outcome: "parsed" }>): FieldStanding {
	const contact = parsed.resume.contact;
	const values: ReadonlyMap<ExtractedField, string | null> = new Map([
		["name", parsed.resume.name ?? null],
		["location", contact?.location ?? null],
		["phone", contact?.phone ?? null],
		["email", contact?.email ?? null],
		["linkedin", contact?.linkedin ?? null],
	]);
	return fieldStanding(values);
}

/**
 * The refusal an outcome that produced no proposal is, worded for the person who pressed.
 *
 * Nothing the adapter, the runtime, the model or the PDF library said appears anywhere in it.
 * Every sentence is constructed from the tables above and from the outcome's own word, which
 * is the containment rule `venator.llm`'s key lane keeps: a constructed message cannot leak by
 * construction, while a scrubbed one has to be got right forever.
 */
function resumeRefusal(outcome: ResumePdfOutcome): OnboardingError {
	if (outcome.outcome === "unreadable") {
		const said = UNREADABLE_PDF.get(outcome.reason);
		return new OnboardingError(
			"extraction_failed",
			said?.message ?? "That PDF could not be read.",
			said?.remedy ?? "Paste the resume text instead.",
		);
	}
	if (outcome.outcome === "no_runtime") {
		return new OnboardingError(
			"runtime_unavailable",
			"No assistant is connected on this machine, so nothing could be asked to read the resume.",
			"Connect one in the first step — or read the PDF on this machine instead, which needs no assistant and reads the five contact facts.",
		);
	}
	if (outcome.outcome === "failed") {
		return new OnboardingError(
			"parse_failed",
			PARSE_FAILURE.get(outcome.stage) ?? "The resume could not be read.",
			"Nothing was written. Read the PDF on this machine instead, or paste the resume text.",
		);
	}
	if (outcome.outcome === "mismatch") {
		return new OnboardingError(
			"reader_unavailable",
			"The resume reader answered in a shape this dashboard does not know, so nothing is being shown rather than something guessed at.",
			"Paste the resume text instead, or type the five fields yourself.",
		);
	}
	if (outcome.outcome === "timed-out") {
		return new OnboardingError(
			"reader_unavailable",
			"Reading the resume did not finish in time.",
			"Nothing was written. Try again, or paste the resume text instead.",
		);
	}
	return new OnboardingError(
		"reader_unavailable",
		"This Install cannot read a PDF: the pipeline that reads one is not part of it, or its interpreter would not start.",
		"Paste the resume text instead, or type the five fields yourself.",
	);
}

/**
 * One uploaded PDF, read the way the request asked, as the answer the screen renders.
 *
 * The two readings meet here and nowhere else. `text` comes back as the same
 * `ResumeSuggestion` a pasted resume produces — one extractor, whichever way the document
 * arrived — and `model` comes back as a proposal that is explicitly nobody's confirmed fact.
 * A document whose outcome does not match the mode that was asked for is drift, and is refused
 * as a mismatch rather than rendered.
 */
async function readUploadedPdf(upload: ResumeUpload): Promise<ResumeImportResponse> {
	const outcome = await readResumePdf(upload.bytes, { mode: upload.parse, lane: upload.lane });
	if (outcome.outcome === "read" && upload.parse === "text") {
		return extractResume(outcome.text);
	}
	if (outcome.outcome === "parsed" && upload.parse === "model") {
		const standing = parsedFieldStanding(outcome);
		const parsed: ParsedResumeResponse = {
			kind: "parsed",
			resume: outcome.resume,
			sections: outcome.sections,
			pages: outcome.pages,
			characters: outcome.characters,
			truncated: outcome.truncated,
			dropped: outcome.dropped,
			schemaEnforced: outcome.schemaEnforced,
			found: standing.found,
			missing: standing.missing,
		};
		return parsed;
	}
	if (outcome.outcome === "read" || outcome.outcome === "parsed") {
		throw resumeRefusal({ outcome: "mismatch" });
	}
	throw resumeRefusal(outcome);
}

export function createOnboardingRoutes(): Hono {
	const onboarding = new Hono();

	onboarding.use(
		"*",
		cors({
			origin: [...LOCAL_ACTION_ORIGINS],
			allowMethods: ["GET", "POST", "OPTIONS"],
			allowHeaders: ["Content-Type", ONBOARDING_REQUEST_HEADER],
		}),
	);

	onboarding.get("/settings", async (context) => {
		requireOnboardingHeader(context.req.header(ONBOARDING_REQUEST_HEADER), context.req.header("origin"));
		return context.json({ runtime: documentRuntime(), jevKeyPresent: (await jevKey()) !== null });
	});

	onboarding.post("/settings", async (context) => {
		requireOnboardingHeader(context.req.header(ONBOARDING_REQUEST_HEADER), context.req.header("origin"));
		refuseOversizedBody(context.req.header("content-length"), 8192, null);
		const body = readJsonBody(await context.req.text());
		const runtime = at(body, "runtime");
		const key = at(body, "jevKey");
		if (runtime !== undefined && runtime !== "claude" && runtime !== "codex") {
			throw new OnboardingError("bad_request", "Choose Claude or ChatGPT.", null, "runtime");
		}
		const keyText = asText(key);
		if (key !== undefined && (keyText === null || keyText.length > 4096 || !keyText.trim())) {
			throw new OnboardingError("bad_request", "Enter one TypeSafe key.", null, "jevKey");
		}
		if (runtime === undefined && key === undefined) throw new OnboardingError("bad_request", "Choose a setting to save.");
		if (keyText !== null) await saveJevKey(keyText);
		if (runtime === "claude" || runtime === "codex") await saveDocumentRuntime(runtime);
		return context.json({ runtime: documentRuntime(), jevKeyPresent: (await jevKey()) !== null });
	});

	/**
	 * Step one: what runtimes does this machine have?
	 *
	 * **Never called on render.** A probe launches a real process per runtime and spends the
	 * person's own subscription, so it runs from a deliberate action and from nothing else —
	 * no mount, no poll, no retry loop. `{}` checks the default runtime only; `all: true` is
	 * an explicit opt-in that costs one launch per runtime.
	 */
	onboarding.post("/runtime/probe", async (context) => {
		requireOnboardingHeader(
			context.req.header(ONBOARDING_REQUEST_HEADER),
			context.req.header("origin"),
		);
		refuseOversizedBody(context.req.header("content-length"), MAXIMUM_JSON_BYTES, null);
		const request = probeRequestFrom(readJsonBody(await context.req.text()));
		const result = await probeRuntimes(request);
		const response: RuntimeProbeResponse = {
			probe: result.probe,
			scope: scopeOf(request),
			lane: request === "default" || request === "all" ? null : request.lane,
			lanes: result.lanes,
			// Which machine this is, so the step can name the install route for it rather than
			// for all of them. It is read from `process.platform` here — on the person's own
			// machine, where the answer actually lives — and never sniffed from a user agent.
			// A platform this contract does not name arrives as `unknown`, which the screen
			// renders as every route rather than as a guess.
			platform: hostPlatform(),
		};
		return context.json(response);
	});

	/**
	 * Step two: read what can be read out of a resume, and write nothing.
	 *
	 * Extraction is a suggestion. The response is what the person confirms or corrects before
	 * any of it becomes a Profile fact, which is why nothing here touches the filesystem and
	 * why every value comes back exactly as it was written in the document.
	 *
	 * **Three readings, one route, and only one of them spends anything.**
	 *
	 * 1. A pasted body — JSON `{text}` or `text/*` — is read by the deterministic extractor in
	 *    `resume.ts`. Unchanged, spawns nothing, spends nothing. A PDF sent this way is still
	 *    refused; what changed is the remedy, which now says to attach the file.
	 * 2. An attached PDF with no `parse` field, or any `parse` but `model`, is extracted to
	 *    text by `venator.resume.parse` on this machine and then read by that same
	 *    deterministic extractor. It spawns a subprocess and spends nothing, and it is what
	 *    somebody who connected no runtime gets.
	 * 3. An attached PDF with `parse=model` spends **one completion** on the Owner's own
	 *    subscription, through `venator.llm.complete` and nothing else, and answers with a
	 *    proposal in which every value is grounded in the document and **no section is
	 *    confirmed**. Nothing is written by it, nothing advances because of it, and the screen
	 *    shows every value back for a person to read — which is the only defence there is
	 *    against a document that spells an instruction in the shape of a fact.
	 *
	 * The route that spends is selected by a field on the form and by nothing else: not by a
	 * default, not by a fallback from a disappointing free reading, and not by a retry. That
	 * separation is the same one the run surface keeps between describing a cost and incurring
	 * one, and it is why this stayed one route rather than becoming a sixth.
	 *
	 * **And one reading at a time.** A second request arriving while one is in flight is
	 * refused with `reader_busy` and spawns nothing — the same posture `/runtime/probe` keeps
	 * for the same reason, stated at length on `oneReadingAtATime` in `resume-pdf.ts`. Ten
	 * presses were ten interpreters and, on the paid path, ten completions billed for the one
	 * press somebody made. The screen disables its own buttons while a reading runs; this is
	 * the backstop for everything that is not the screen.
	 */
	onboarding.post("/resume/import", async (context) => {
		requireOnboardingHeader(
			context.req.header(ONBOARDING_REQUEST_HEADER),
			context.req.header("origin"),
		);
		// An attachment may be a PDF and a PDF is allowed to be larger than a text file, so the
		// declared length is held to whichever bound the route it arrived on actually has. The
		// text bound is untouched: it is still 512 KiB for everything that is not an attachment,
		// and for an attachment that turns out not to be a PDF.
		const attached = (context.req.header("content-type") ?? "").startsWith("multipart/form-data");
		refuseOversizedBody(
			context.req.header("content-length"),
			attached ? MAXIMUM_PDF_BYTES : MAXIMUM_RESUME_BYTES,
			attached
				? "Attach a smaller PDF, or paste the resume text instead."
				: "Paste the resume text instead, or save a plain-text copy of it.",
		);
		const upload = await resumeUploadFrom(context.req.raw);
		if (upload.pdf) return context.json(await readUploadedPdf(upload));
		const suggestion: ResumeSuggestion = extractResume(readResumeText(upload.bytes));
		return context.json(suggestion);
	});

	/**
	 * Step three, first half: what boards does this employer's address name?
	 *
	 * The cold start. A Profile written here begins with an empty board registry, and Discover
	 * only answers for an employer somebody registered, so without this a person finishes
	 * onboarding and their first run finds nothing. Every rule applied belongs to
	 * `venator.discover.register`, which this runs rather than reimplements.
	 *
	 * Read-only, and the only outbound request anything on this surface makes: `GET`s that
	 * follow no redirect, against the address that was pasted and the ATS board it names.
	 * Nothing is written — a resolved board reaches a Profile only when the write below sends
	 * it back with an employer name attached.
	 */
	onboarding.post("/boards/resolve", async (context) => {
		requireOnboardingHeader(
			context.req.header(ONBOARDING_REQUEST_HEADER),
			context.req.header("origin"),
		);
		refuseOversizedBody(context.req.header("content-length"), MAXIMUM_JSON_BYTES, null);
		const body = readJsonBody(await context.req.text());
		const url = asText(at(body, "url"));
		if (url === null || url.trim() === "") {
			throw new OnboardingError("bad_request", "No address was given to look at.", null, "url");
		}
		const resolved: BoardResolveResponse = await resolveBoards(url);
		return context.json(resolved);
	});

	/**
	 * Step three, second half: write the Profile.
	 *
	 * The only write in the repository. It validates first and writes second, it writes into
	 * the application data directory and nowhere else, and it moves a fully written directory
	 * into place in one rename so an interruption cannot leave a Profile that loads with half
	 * its policy missing. Between the two, the staged directory is read back by the pipeline's
	 * own loader, and the rename happens only if it loads.
	 */
	onboarding.post("/profile", async (context) => {
		requireOnboardingHeader(
			context.req.header(ONBOARDING_REQUEST_HEADER),
			context.req.header("origin"),
		);
		let proposal: ProfileProposal;
		if ((context.req.header("content-type") ?? "").startsWith("multipart/form-data")) {
			const upload = await readProfileUpload(context.req.raw);
			proposal = { ...proposalFrom(readJsonBody(upload.json)), referencePdf: upload.pdf };
		} else {
			refuseOversizedBody(context.req.header("content-length"), MAXIMUM_JSON_BYTES, null);
			proposal = proposalFrom(readJsonBody(await context.req.text()));
		}
		const written = await writeProfile(proposal);
		const response: ProfileWriteResponse = {
			profile: { name: written.name, directory: written.directory, files: written.files },
			replaced: written.replaced,
			warnings: written.warnings,
		};
		return context.json(response, written.replaced ? 200 : 201);
	});

	/**
	 * The way back, for somebody already past setup with no employer registered.
	 *
	 * This is the only route on this surface that touches a Profile that already exists, and it
	 * touches one file of it: `targeting.yaml`, appended to under `sources` and nowhere else
	 * (`targeting-edit.ts`). It writes into this Install's own application data directory and
	 * refuses a Profile that comes from a checkout, exactly as the Profile write does.
	 *
	 * It makes no request of anything. The board arriving here was resolved by
	 * `/boards/resolve` above, which is where the one outbound `GET` on this surface lives, and
	 * an employer name is never invented — a board with no name is refused rather than written
	 * nameless. It does run one subprocess, and that one reads: the edited document is staged
	 * beside the file and handed to `src/venator/profile/` before the rename, so an employer
	 * whose registration would produce a Profile the pipeline cannot read is refused with the
	 * file left byte-identical.
	 */
	onboarding.post("/employers", async (context) => {
		requireOnboardingHeader(
			context.req.header(ONBOARDING_REQUEST_HEADER),
			context.req.header("origin"),
		);
		refuseOversizedBody(context.req.header("content-length"), MAXIMUM_JSON_BYTES, null);
		const body = readJsonBody(await context.req.text());
		const request = employersFrom(body);
		const response: EmployerRegisterResponse = await registerEmployers(request.profile, request.boards);
		return context.json(response);
	});

	onboarding.onError((error, context) => {
		if (error instanceof OnboardingError) {
			return context.json(error.body, error.status);
		}
		if (error instanceof ResumeReadError) {
			const wrapped = new OnboardingError("extraction_failed", error.message, error.remedy);
			return context.json(wrapped.body, wrapped.status);
		}
		if (error instanceof RuntimeLaneNameError) {
			const wrapped = new OnboardingError("bad_request", error.message, null, "lane");
			return context.json(wrapped.body, wrapped.status);
		}
		if (error instanceof ResumeReaderBusyError) {
			const wrapped = new OnboardingError(
				"reader_busy",
				"A resume is already being read.",
				"Wait for it to finish — each reading launches a real process, and the one that asks the assistant spends a request on the account's own budget, so a second is not started alongside it.",
			);
			return context.json(wrapped.body, wrapped.status);
		}
		if (error instanceof RuntimeProbeBusyError) {
			const wrapped = new OnboardingError(
				"probe_busy",
				"A runtime check is already running.",
				"Wait for it to finish — each one launches a real process and spends the account's own budget, so a second is not started alongside it.",
			);
			return context.json(wrapped.body, wrapped.status);
		}
		// Nothing else is described to the caller: what throws here is filesystem and process
		// machinery, and its messages carry paths and environment contents.
		process.stderr.write(`onboarding: unhandled failure: ${error.message}\n`);
		const wrapped = new OnboardingError("write_failed", "Something went wrong handling that request.");
		return context.json(wrapped.body, wrapped.status);
	});

	return onboarding;
}
