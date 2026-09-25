/**
 * The one error shape the onboarding surface answers with.
 *
 * Every message here is written by hand. A caught exception's own text is logged to stderr
 * and never returned, because the things that throw in this module are filesystem calls and
 * subprocess launches, and their messages carry absolute paths, home directory names and
 * occasionally environment contents. The person on the other end of a failed write needs to
 * know what to do about it, not where this Install keeps its files.
 */

import type { OnboardingErrorBody, OnboardingErrorCode } from "../../shared/onboarding.ts";

/** The literal statuses this surface answers with; Hono types a response on the literal. */
export type OnboardingStatus = 400 | 403 | 409 | 413 | 415 | 422 | 500 | 503;

const STATUS: ReadonlyMap<OnboardingErrorCode, OnboardingStatus> = new Map([
	["bad_request", 400],
	["body_too_large", 413],
	["unsupported_media_type", 415],
	["invalid_profile_name", 422],
	["invalid_profile", 422],
	["profile_exists", 409],
	// The Profile the request named is not one this Install can edit — it does not exist, or
	// it lives in a checkout, which is somebody's working tree and not this surface's to write.
	["profile_absent", 409],
	// The Profile is there and is written in a shape this dashboard will not edit. A 422: the
	// request was fine, the file it names is one only a person should change.
	["targeting_unreadable", 422],
	["probe_busy", 409],
	["write_failed", 500],
	["extraction_failed", 422],
	// The reading happened and produced nothing usable. A 422 like every other unusable
	// document: the request was well formed and the file it carried is the person's to change.
	["parse_failed", 422],
	// Nothing about the request was wrong — this machine simply has no runtime connected, so
	// the reading that needs one could not run. A 503 like the resolver's, and the remedy names
	// the free reading of the same file rather than leaving somebody stuck.
	["runtime_unavailable", 503],
	// The reader could not be run at all, or answered in a shape this dashboard does not know.
	// Also a 503: the upload may have been perfect and this Install cannot read a PDF.
	["reader_unavailable", 503],
	// A second reading arriving while one is in flight. A 409 like `probe_busy`, and for the
	// same reason: nothing about the request was wrong, and the state it conflicts with is one
	// that clears itself.
	["reader_busy", 409],
	// A pasted address that named no board is the person's to fix, so it is a 422 like every
	// other unusable value. A resolver that could not run at all is not: the paste may have
	// been perfect and this Install simply cannot read a careers page, which is what 503 says.
	["unresolvable_url", 422],
	["resolver_unavailable", 503],
	// The staged Profile was read back by the pipeline's own loader and refused. A 422: the
	// request produced a document the arbiter of what a Profile means will not accept, and the
	// values in it are the person's to correct.
	["unloadable_profile", 422],
	// The read-back could not be run at all — no pipeline in this Install, or a check that did
	// not finish. Nothing about the request was wrong, so it is a 503 like the resolver's.
	["verification_unavailable", 503],
	["forbidden_origin", 403],
]);

export class OnboardingError extends Error {
	readonly code: OnboardingErrorCode;
	readonly remedy: string | null;
	readonly field: string | null;

	constructor(code: OnboardingErrorCode, message: string, remedy: string | null = null, field: string | null = null) {
		super(message);
		this.name = "OnboardingError";
		this.code = code;
		this.remedy = remedy;
		this.field = field;
	}

	get status(): OnboardingStatus {
		return STATUS.get(this.code) ?? 500;
	}

	get body(): OnboardingErrorBody {
		return { error: { code: this.code, message: this.message, remedy: this.remedy, field: this.field } };
	}
}

/** A rejection naming the field that caused it, which is what a form needs to mark. */
export function invalidProfile(field: string, message: string, remedy: string | null = null): OnboardingError {
	return new OnboardingError("invalid_profile", message, remedy, field);
}
