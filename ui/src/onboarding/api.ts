/**
 * The app's side of the onboarding surface.
 *
 * `src/api.ts` reads the dashboard's `GET`-only API; this reads the one `POST` surface, and
 * it is deliberately a separate module for the same reason the server mounts them apart: the
 * dashboard never writes, and keeping the writing calls out of the read client is what makes
 * that visible rather than merely true.
 *
 * Two rules live here rather than in a screen:
 *
 * - **Only read-only calls run on mount.** The existing Profile and Install settings are
 *   safe to read on entry; probes, writes and paid PDF readings are only called from presses.
 *   `GET /api/onboarding/state`, which lists directories, stays in `api.ts`.
 * - **A failure arrives as a value, not as a string.** The surface answers every failure with
 *   the same body: a stable `code`, a `message` written for the person reading it, a `remedy`
 *   when there is one, and the `field` at fault. `OnboardingRequestError` carries all four so
 *   a form can mark the input the server named instead of guessing from prose.
 */

import {
	ONBOARDING_REQUEST_HEADER,
	ONBOARDING_REQUEST_HEADER_VALUE,
	type BoardResolveResponse,
	type EmployerRegisterResponse,
	type EmployerRegistration,
	type OnboardingErrorBody,
	type OnboardingErrorCode,
	type ProfileWriteResponse,
	type ResumeImportResponse,
	type ResumeParseMode,
	type ResumeSuggestion,
	type RuntimeProbeResponse,
} from "../../shared/onboarding.ts";
import type { ExistingProfileResponse, ProfileDocuments, ProfileForm } from "../../shared/profile-form.ts";
import { API_BASE_URL } from "../api.ts";
import type { ProfileProposalBody } from "./draft.ts";

/** A refusal from the onboarding surface, with everything the screen needs to render it. */
export class OnboardingRequestError extends Error {
	readonly code: OnboardingErrorCode | null;
	readonly remedy: string | null;
	readonly field: string | null;

	constructor(message: string, code: OnboardingErrorCode | null, remedy: string | null, field: string | null) {
		super(message);
		this.name = "OnboardingRequestError";
		this.code = code;
		this.remedy = remedy;
		this.field = field;
	}
}

/**
 * The failure a browser reports when the loopback server is not answering at all.
 *
 * Written here rather than let through as the platform's own wording, which is a different
 * sentence in every browser and says nothing a person can act on.
 */
const UNREACHABLE = new OnboardingRequestError(
	"The dashboard could not reach its own local server, so nothing was sent.",
	null,
	"Check that the Venator dashboard is still running, then try again.",
	null,
);

function failureFrom(status: number, body: OnboardingErrorBody | null): OnboardingRequestError {
	const error = body?.error;
	if (error === undefined) {
		return new OnboardingRequestError(
			`That request was refused (${String(status)}), and no reason came back with it.`,
			null,
			null,
			null,
		);
	}
	return new OnboardingRequestError(error.message, error.code, error.remedy, error.field);
}

async function post<Value>(path: string, request: RequestInit): Promise<Value> {
	const headers = new Headers(request.headers);
	headers.set(ONBOARDING_REQUEST_HEADER, ONBOARDING_REQUEST_HEADER_VALUE);
	headers.set("accept", "application/json");
	let response: Response;
	try {
		response = await fetch(`${API_BASE_URL}/onboarding${path}`, { ...request, method: "POST", headers });
	} catch {
		throw UNREACHABLE;
	}
	if (!response.ok) {
		const body: OnboardingErrorBody | null = await response.json().catch(() => null);
		throw failureFrom(response.status, body);
	}
	// This surface shares shared/onboarding.ts with the server, so a successful body is
	// already the contract's own type rather than foreign input.
	return await response.json();
}

/**
 * Everything this surface is ever sent, named rather than left open.
 *
 * The list is short on purpose: setup questions, one new Profile, an existing Profile edit,
 * and a handful of employers. Naming them keeps the write surface's input
 * visible in one place — nothing reaches it that is not one of these.
 */
type ProbeBody =
	/** The runtime the pipeline would use: the cheapest question, and the default. */
	| Record<string, never>
	| { readonly lane: string }
	| { readonly all: true };

/** The body of `POST /api/onboarding/employers`: one named Profile, and what to put in it. */
export type EmployerRegistrationBody = {
	readonly profile: string;
	readonly boards: readonly EmployerRegistration[];
};

type OnboardingRequestBody =
	| { readonly runtime: "claude" | "codex" }
	| { readonly jevKey: string }
	| ProbeBody
	| { readonly text: string }
	| { readonly url: string }
	| ProfileProposalBody
	| EmployerRegistrationBody
	| ProfileFormBody;

/** The body of `POST /api/onboarding/existing-profile`: the form, and the files as they were opened. */
export type ProfileFormBody = {
	readonly name: string;
	readonly form: ProfileForm;
	readonly original: ProfileDocuments;
};

function postJson<Value>(path: string, body: OnboardingRequestBody): Promise<Value> {
	return post<Value>(path, { headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
}

/**
 * The one request on this surface that is not JSON: an uploaded file.
 *
 * A resume PDF is bytes, and bytes in a JSON string would be a base64 encoding of somebody's
 * document inflated by a third for no reason. So this is `multipart/form-data`, and the
 * content type is deliberately *not* set — the browser writes it, with the boundary it chose,
 * and setting it by hand is how a multipart body arrives unparseable.
 *
 * The closed union above still governs every JSON body. What may be in a form is narrower
 * still and is named where it is built: the file, and the two fields `buildResumeForm` writes.
 */
function postForm<Value>(path: string, body: FormData): Promise<Value> {
	return post<Value>(path, { body });
}

/** What a runtime probe was asked. One runtime is the ordinary question; every one is not. */
export type ProbeScope = { readonly kind: "default" } | { readonly kind: "lane"; readonly lane: string } | { readonly kind: "all" };

/** `{}` asks about the runtime the pipeline would use; the other two are explicit. */
function probeBody(scope: ProbeScope): ProbeBody {
	if (scope.kind === "lane") return { lane: scope.lane };
	if (scope.kind === "all") return { all: true };
	return {};
}

/**
 * Checks runtimes on this machine.
 *
 * **Only ever from something the Owner pressed.** Each runtime checked is a process launch
 * and a small completion against their own subscription, and `all` is one of each per
 * runtime. Never call this on mount, on a route change, on a timer, or to refresh a result
 * that is already on screen.
 */
export function probeRuntimes(scope: ProbeScope): Promise<RuntimeProbeResponse> {
	return postJson<RuntimeProbeResponse>("/runtime/probe", probeBody(scope));
}

export type InstallSettings = { readonly runtime: "claude" | "codex"; readonly jevKeyPresent: boolean };

/** Reads the Install's Profile as the Profile screen shows it, with the files it was read from. */
export async function readProfileForm(name: string): Promise<ExistingProfileResponse> {
	const response = await fetch(`${API_BASE_URL}/onboarding/existing-profile?name=${encodeURIComponent(name)}`, {
		headers: { [ONBOARDING_REQUEST_HEADER]: ONBOARDING_REQUEST_HEADER_VALUE },
	});
	if (!response.ok) {
		const body: OnboardingErrorBody | null = await response.json().catch(() => null);
		throw failureFrom(response.status, body);
	}
	// SAFETY: this local route answers with the shared ExistingProfileResponse shape.
	return await response.json() as ExistingProfileResponse;
}

/**
 * Saves the Profile screen's form.
 *
 * `original` is the three files as they were opened; the server refuses to write over a
 * Profile that changed since, and writes only the form's differences into them.
 */
export function saveProfileForm(name: string, form: ProfileForm, original: ProfileDocuments): Promise<{ saved: boolean }> {
	return postJson<{ saved: boolean }>("/existing-profile", { name, form, original });
}

export async function readInstallSettings(): Promise<InstallSettings> {
	const response = await fetch(`${API_BASE_URL}/onboarding/settings`, {
		headers: { [ONBOARDING_REQUEST_HEADER]: ONBOARDING_REQUEST_HEADER_VALUE },
	});
	if (!response.ok) throw new Error("Install settings could not be read.");
	// SAFETY: The local server's /settings response has the shared InstallSettings shape.
	return await response.json() as InstallSettings;
}

export function chooseDocumentRuntime(runtime: "claude" | "codex"): Promise<InstallSettings> {
	return postJson<InstallSettings>("/settings", { runtime });
}

export function storeJevKey(jevKey: string): Promise<InstallSettings> {
	return postJson<InstallSettings>("/settings", { jevKey });
}

/** Reads five contact facts out of resume text. Writes nothing; spends nothing. */
export function importResumeText(text: string): Promise<ResumeSuggestion> {
	return postJson<ResumeSuggestion>("/resume/import", { text });
}

/**
 * What to do with an uploaded PDF: read it here, or have an assistant read it.
 *
 * `lane` names the runtime to run the completion on and is null when nothing is named, which
 * leaves the choice where the pipeline already makes it. Nothing here falls back from one
 * runtime to another.
 */
export type ResumePdfImport = {
	readonly parse: ResumeParseMode;
	readonly lane: string | null;
};

function buildResumeForm(file: File, request: ResumePdfImport): FormData {
	const form = new FormData();
	form.append("file", file);
	// Written always, including for the free reading, so the request says which of the two it
	// is rather than leaving it to be inferred from a missing field. The server reads it the
	// other way round — anything but `model` is free — so the two disagree only in the safe
	// direction.
	form.append("parse", request.parse);
	if (request.lane !== null) form.append("lane", request.lane);
	return form;
}

/**
 * Reads an uploaded resume PDF.
 *
 * **`parse: "model"` spends one request on the Owner's own subscription, and nothing else
 * here does.** Like the runtime probe, it is only ever called from a handler for something a
 * person pressed after reading what pressing it costs — never on mount, never on a route
 * change, never on a timer, and never as a retry of the free reading. `parse: "text"` extracts
 * the PDF's text on this machine and spends nothing; it is the reading somebody who connected
 * no assistant gets, and it is a first-class answer rather than a degraded one.
 *
 * Neither writes anything. What comes back is a suggestion or a proposal, and the Profile is
 * written at the end of the flow by `writeProfile` and nowhere else.
 */
export function importResumePdf(file: File, request: ResumePdfImport): Promise<ResumeImportResponse> {
	return postForm<ResumeImportResponse>("/resume/import", buildResumeForm(file, request));
}

/** Turns one pasted employer address into the boards a Profile can poll. Reads only. */
export function resolveBoards(url: string): Promise<BoardResolveResponse> {
	return postJson<BoardResolveResponse>("/boards/resolve", { url });
}

/** Writes the Profile. The one write that makes a Profile where there was none. */
export function writeProfile(proposal: ProfileProposalBody, referencePdf: File | null = null): Promise<ProfileWriteResponse> {
	if (referencePdf !== null) {
		const form = new FormData();
		form.append("proposal", JSON.stringify(proposal));
		form.append("file", referencePdf);
		return postForm<ProfileWriteResponse>("/profile", form);
	}
	return postJson<ProfileWriteResponse>("/profile", proposal);
}

/**
 * Registers employers into a Profile that already exists.
 *
 * The one call here that edits something somebody already has, and it edits one part of it:
 * the employers `targeting.yaml` registers. Nothing else in that file is read, written or
 * moved (`server/onboarding/targeting-edit.ts`).
 */
export function registerEmployers(
	profile: string,
	boards: readonly EmployerRegistration[],
): Promise<EmployerRegisterResponse> {
	return postJson<EmployerRegisterResponse>("/employers", { profile, boards });
}
